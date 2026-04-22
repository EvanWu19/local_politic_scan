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

PM_SYSTEM = f"""You are the Product Manager for a daily local-politics podcast
aimed at a first-time voter in {_LOCALE}.

You receive the audience's free-text daily notes from the past several
days (the listener writes one short note per episode after listening).
Your job is to distill them into a tight, structured rollup that the
Editor and Author agents will use to plan future episodes.

Look for:
  - RECURRING THEMES — topics the listener mentions across multiple days
  - OPEN QUESTIONS — questions they've asked that future episodes should answer
  - UNDERSERVED TOPICS — areas they keep flagging interest in but the show
    hasn't covered enough

Be conservative: do not invent themes from a single passing comment.
If there is genuinely nothing in a category, leave it empty.

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

    user_prompt = _build_user_prompt(notes, window_start, window_end)

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
    )
    saved = get_latest_weekly_themes(db_path)
    log.info("PM agent: saved rollup for %s..%s (%d themes, %d open questions)",
             window_start, window_end,
             len(parsed["themes"]), len(parsed["open_questions"]))
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


def _build_user_prompt(notes: List[Dict], window_start: date, window_end: date) -> str:
    body = "\n\n".join(
        f"[{r['report_date']}] {r['content'].strip()}"
        for r in sorted(notes, key=lambda x: x["report_date"])
    )
    return (
        f"Audience notes for {window_start.isoformat()} through {window_end.isoformat()}:\n\n"
        f"{body}\n\n"
        "Produce the rollup now."
    )


_SECTION_RE = re.compile(
    r"^\s*(SUMMARY|THEMES|OPEN QUESTIONS|UNDERSERVED TOPICS)\s*:\s*(.*)$",
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
    return "\n".join(lines)
