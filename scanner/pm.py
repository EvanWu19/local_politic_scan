"""
PM agent — rolls up audience daily_notes into recurring themes,
open questions, and underserved topics.

Used by:
  • the Editor agent, which reads the most recent rollup so its revisions
    can speak to longer-term patterns the listener has expressed
  • a future Author/themes dashboard (not yet wired)

Run via `python main.py pm` or programmatically. Idempotent: a second run
for the same window UPSERTs the row.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import anthropic

from config import Config as _Cfg

log = logging.getLogger(__name__)

_LOCALE = ", ".join(p for p in [_Cfg.CITY, _Cfg.COUNTY, _Cfg.STATE] if p) or "the local area"

DEFAULT_WINDOW_DAYS = 7
MIN_NOTES = 2  # below this, the rollup has no useful signal — skip the LLM call
RECENT_SCRIPTS_DAYS = 5
MAX_SCRIPT_CHARS = 2500  # per-episode excerpt budget fed to PM

PM_SYSTEM = f"""You are the Product Manager for a daily local-politics podcast
aimed at a first-time voter in {_LOCALE}.

You receive:
  1. the audience's free-text daily notes from the past several days
     (the listener writes one short note per episode after listening)
  2. excerpts from the podcast scripts the show has shipped over the
     last 5 days — so you can detect repeated framings, taglines, or
     anecdotes the show has leaned on too hard

Your job is to distill all of this into a tight, structured rollup
that the Editor and Author agents will use to plan the NEXT episode.

Look for:
  - RECURRING THEMES — topics the listener mentions across multiple days
  - OPEN QUESTIONS — questions they've asked that future episodes should answer
  - UNDERSERVED TOPICS — areas they keep flagging interest in but the show
    hasn't covered enough
  - RECENT COVERAGE TO AVOID — specific framings, taglines, anecdotes,
    or phrasings that have already appeared in the last 5 days of scripts
    and should NOT be repeated in the next episode. Be concrete: name the
    exact tagline or story, not a vague category. Also include any
    framings the listener has flagged as repetitive in their notes.

Be conservative on themes (don't invent from one passing comment), but
be aggressive on the avoid list — repetition is the #1 quality
problem and the listener has called it out explicitly.

OUTPUT FORMAT — five sections, exactly in this order, nothing else:

SUMMARY: <one short paragraph (2–3 sentences) describing what this listener
seems to care about right now>

THEMES:
- <theme 1 short title> | <one-sentence why it matters to this listener>
- <theme 2 short title> | <one-sentence why it matters to this listener>
(0–5 lines; omit the section header line if there are none)

OPEN QUESTIONS:
- <verbatim or near-verbatim question the listener has raised>
- ...
(0–5 lines)

UNDERSERVED TOPICS:
- <short label of a topic the listener keeps asking about>
- ...
(0–5 lines)

RECENT COVERAGE TO AVOID:
- <concrete tagline, framing, or story the show has already used>
- ...
(0–10 lines; be specific enough that the Author would recognize the
item if they wrote it again)

Plain text only — no JSON, no markdown fences, no preamble."""


def generate_weekly_themes(
    db_path: Path,
    anthropic_key: str,
    window_end: Optional[date] = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    model: str = "claude-haiku-4-5-20251001",
) -> Optional[Dict]:
    """
    Roll up daily_notes in [window_end - window_days + 1 .. window_end].
    Returns the saved rollup dict, or None if not enough notes / no key.
    """
    if not anthropic_key:
        log.warning("PM agent: no anthropic_key, skipping")
        return None

    window_end = window_end or date.today()
    window_start = window_end - timedelta(days=window_days - 1)

    notes = _load_notes_in_range(db_path, window_start, window_end)
    if len(notes) < MIN_NOTES:
        log.info("PM agent: only %d notes in window %s..%s — skipping",
                 len(notes), window_start, window_end)
        return None

    recent_scripts = _load_recent_scripts(
        window_end=window_end, days=RECENT_SCRIPTS_DAYS
    )
    user_prompt = _build_user_prompt(
        notes, window_start, window_end, recent_scripts
    )

    try:
        client = anthropic.Anthropic(api_key=anthropic_key)
        resp = client.messages.create(
            model=model,
            max_tokens=2000,
            system=PM_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = resp.content[0].text.strip()
    except Exception as e:
        log.error("PM agent Claude call failed: %s", e)
        return None

    parsed = _parse_pm_output(raw)
    if parsed is None:
        log.warning("PM agent: output unparseable, not saving")
        return None

    from scanner.database import save_weekly_themes, get_latest_weekly_themes
    save_weekly_themes(
        db_path=db_path,
        week_start=window_start.isoformat(),
        week_end=window_end.isoformat(),
        themes=parsed["themes"],
        open_questions=parsed["open_questions"],
        underserved_topics=parsed["underserved_topics"],
        summary=parsed["summary"],
        note_count=len(notes),
        avoid_list=parsed["avoid_list"],
    )
    saved = get_latest_weekly_themes(db_path)
    log.info(
        "PM agent: saved rollup for %s..%s (%d themes, %d open questions, %d avoid)",
        window_start, window_end,
        len(parsed["themes"]), len(parsed["open_questions"]),
        len(parsed["avoid_list"]),
    )
    return saved


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _load_notes_in_range(db_path: Path, start: date, end: date) -> List[Dict]:
    """Inclusive [start, end] range of daily_notes with non-empty content."""
    from scanner.database import list_daily_notes
    rows = list_daily_notes(db_path, limit=200)
    start_iso, end_iso = start.isoformat(), end.isoformat()
    return [
        r for r in rows
        if start_iso <= r.get("report_date", "") <= end_iso
        and (r.get("content") or "").strip()
    ]


def _build_user_prompt(notes: List[Dict], window_start: date, window_end: date,
                        recent_scripts: List[Dict]) -> str:
    notes_body = "\n\n".join(
        f"[{r['report_date']}] {r['content'].strip()}"
        for r in sorted(notes, key=lambda x: x["report_date"])
    )
    parts = [
        f"Audience notes for {window_start.isoformat()} through {window_end.isoformat()}:",
        "",
        notes_body,
    ]
    if recent_scripts:
        parts += ["", "Recent shipped podcast scripts (most recent first):", ""]
        for s in recent_scripts:
            parts.append(f"--- {s['date']} {s['episode']} ---")
            parts.append(s["excerpt"])
            parts.append("")
    else:
        parts += ["", "(No prior shipped scripts available.)"]
    parts += ["", "Produce the rollup now."]
    return "\n".join(parts)


def _load_recent_scripts(window_end: date, days: int) -> List[Dict]:
    """
    Load the shipped (post-editor) episode scripts for the last `days`
    days up to (and including) window_end. Returns a list of
    {date, episode, excerpt} dicts, newest first.

    Uses the final `podcast_<date>_ep<N>.txt` artifacts written to
    Config.PODCASTS_DIR. Excerpts are truncated to MAX_SCRIPT_CHARS to
    keep the PM prompt bounded.
    """
    pdir: Path = _Cfg.PODCASTS_DIR
    if not pdir.exists():
        return []

    start = window_end - timedelta(days=days - 1)
    out: List[Dict] = []
    # newest first
    for delta in range(days):
        d = window_end - timedelta(days=delta)
        if d < start:
            break
        # ep1..ep4 in order (keep deterministic)
        for f in sorted(pdir.glob(f"podcast_{d.isoformat()}_ep*.txt")):
            # Skip draft/intermediate artifacts; shipped file is plain <date>_epN.txt
            name = f.name
            if ".draft." in name or ".editor." in name:
                continue
            if not name.endswith(".txt"):
                continue
            # files like podcast_<date>_ep1_federal.txt are segment-only
            # legacy files — prefer the consolidated _ep1.txt. Skip the
            # _epN_<slug>.txt variants.
            stem = f.stem  # podcast_<date>_ep1 or podcast_<date>_ep1_federal
            tail = stem.split(f"podcast_{d.isoformat()}_", 1)[-1]
            if "_" in tail:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                continue
            if not text:
                continue
            if len(text) > MAX_SCRIPT_CHARS:
                text = text[:MAX_SCRIPT_CHARS] + " …[truncated]"
            out.append({
                "date": d.isoformat(),
                "episode": tail,
                "excerpt": text,
            })
    return out


_SECTION_RE = re.compile(
    r"^\s*(SUMMARY|THEMES|OPEN QUESTIONS|UNDERSERVED TOPICS|RECENT COVERAGE TO AVOID)\s*:\s*(.*)$",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^\s*[-*•]\s+(.*?)\s*$")


def _parse_pm_output(raw: str) -> Optional[Dict]:
    """Parse the five-section text format. Returns None if SUMMARY missing."""
    if not raw:
        return None
    text = re.sub(r"^```[a-zA-Z]*\s*\n?", "", raw.strip())
    text = re.sub(r"\n?```\s*$", "", text).strip()

    sections: Dict[str, List[str]] = {
        "SUMMARY": [], "THEMES": [],
        "OPEN QUESTIONS": [], "UNDERSERVED TOPICS": [],
        "RECENT COVERAGE TO AVOID": [],
    }
    current = None
    for line in text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            current = m.group(1).upper()
            inline = m.group(2).strip()
            if inline:
                sections[current].append(inline)
            continue
        if current is None:
            continue
        b = _BULLET_RE.match(line)
        if b:
            sections[current].append(b.group(1).strip())
        elif line.strip() and current == "SUMMARY":
            # Allow multi-line summary text without bullet
            sections[current].append(line.strip())

    summary = " ".join(sections["SUMMARY"]).strip()
    if not summary:
        return None

    themes: List[Dict] = []
    for line in sections["THEMES"]:
        if "|" in line:
            title, why = line.split("|", 1)
            themes.append({"title": title.strip(), "why": why.strip()})
        else:
            themes.append({"title": line.strip(), "why": ""})

    return {
        "summary": summary,
        "themes": themes,
        "open_questions": [s for s in sections["OPEN QUESTIONS"] if s],
        "underserved_topics": [s for s in sections["UNDERSERVED TOPICS"] if s],
        "avoid_list": [s for s in sections["RECENT COVERAGE TO AVOID"] if s],
    }


def format_themes_for_prompt(rollup: Dict) -> str:
    """Render a saved rollup back into a compact prompt-friendly block."""
    if not rollup:
        return ""
    lines = [
        f"Listener rollup ({rollup.get('week_start')}..{rollup.get('week_end')}, "
        f"based on {rollup.get('note_count', 0)} notes):",
        f"Summary: {rollup.get('summary','')}",
    ]
    themes = rollup.get("themes") or []
    if themes:
        lines.append("Recurring themes:")
        for t in themes:
            why = f" — {t.get('why')}" if t.get("why") else ""
            lines.append(f"  - {t.get('title','')}{why}")
    qs = rollup.get("open_questions") or []
    if qs:
        lines.append("Open questions the listener keeps raising:")
        for q in qs:
            lines.append(f"  - {q}")
    under = rollup.get("underserved_topics") or []
    if under:
        lines.append("Topics they want more coverage of:")
        for u in under:
            lines.append(f"  - {u}")
    avoid = rollup.get("avoid_list") or []
    if avoid:
        lines.append(
            "RECENT COVERAGE TO AVOID — do NOT reuse these framings, "
            "taglines, or anecdotes (they already shipped in the last 5 days):"
        )
        for a in avoid:
            lines.append(f"  - {a}")
    return "\n".join(lines)
