"""
Editor agent — reviews a draft podcast episode script before TTS.

Reads:
  • the last 3 days of prior episode scripts (to catch verbatim repetition)
  • the last 7 days of audience daily notes (to surface open questions)

Returns {"final_script", "notes", "changed"}. If Claude returns anything we
can't parse, we fall back to the draft unchanged.
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
_DISTRICTS = _Cfg.districts_profile()
_DISTRICT_BLOCK = f"\n\nLISTENER'S VOTING DISTRICTS (keep the script tied to these offices):\n{_DISTRICTS}\n" if _DISTRICTS else ""

PRIOR_DAYS = 5
NOTES_DAYS = 7
_MAX_PRIOR_CHARS = 8000
_MAX_NOTES_CHARS = 2500
_MAX_THEMES_CHARS = 2000

EDITOR_SYSTEM = f"""You are the Editor for a local politics podcast aimed at a first-time voter in {_LOCALE}.{_DISTRICT_BLOCK}

An Author agent has written today's episode script as a two-host dialogue
(ALEX = presenter, JORDAN = curious co-host). Your job is a tight final pass
before text-to-speech synthesis — AND you have authority to ORDER a full
rewrite when a draft is beyond surgical repair.

CHECK FOR:
1. REPETITION — Compare today's script against the last 5 days of shipped
   scripts shown below. Repetition is the #1 quality problem the listener
   has called out. Flag not just verbatim matches but also paraphrased
   reuse: the same story framing ("imagine you're at the kitchen table…"),
   the same tagline, the same anecdote re-told, the same metaphor.
   • If 1–3 lines are repetitive: rewrite just those lines to acknowledge
     prior coverage and add a fresh angle.
   • If repetition is PERVASIVE (more than ~3 reused framings, or the
     entire through-line mirrors a prior episode): do NOT try to patch —
     issue an ORDER_REWRITE verdict with a concrete rewrite_reason and a
     list of framings/taglines/anecdotes the Author must avoid.
2. AUDIENCE QUESTIONS — The listener leaves daily notes AFTER each episode.
   The notes shown below are from earlier days (never today's). If any of
   them raise a question or topic today's script can speak to, add 1–2
   lines where Alex or Jordan briefly addresses it — phrased like a
   delayed callback ("a listener asked us last week why…"). Never invent
   facts not supported by the script.
3. RECURRING THEMES — A separate PM rollup distills the listener's recent
   notes into themes, open questions, and underserved topics, plus a
   RECENT COVERAGE TO AVOID list. If today's script ignores a flagged
   theme or reuses something on the avoid list, that counts toward
   repetition severity (rule 1). If the draft is clean, do NOT shoehorn
   themes in.
4. LISTENER POSITIONING — The tool is a personalized guide for this
   listener. The script must never tell them to "figure out who your
   candidate is", "look up your district", or "find out who's on your
   ballot" — the Collector and Analyst own that work. If you see such a
   phrase, rewrite it (or escalate to ORDER_REWRITE if pervasive).
5. FORMAT DISCIPLINE — Every line must begin `ALEX:` or `JORDAN:` with no
   stage directions, markdown, brackets, asterisks, or emoji. Remove any line
   that is not dialogue. Keep speakers alternating naturally.

HARD RULES:
- Preserve overall length and tone on surgical edits. Do not gut the script.
- Prefer the SMALLEST change that fixes a real problem; escalate to
  ORDER_REWRITE only when the draft has a structural problem that can't
  be patched line-by-line.
- NEVER invent facts the draft doesn't contain.

OUTPUT FORMAT — four sections, exactly in this order, nothing else:

NOTES: <one short sentence describing what you changed, or why you ordered a rewrite, or "No changes needed.">
VERDICT: approved | revised | order_rewrite
REWRITE_REASON: <only when VERDICT is order_rewrite — one sentence explaining the structural problem, followed by a semicolon-separated list of specific framings/taglines/anecdotes the Author must avoid in the rewrite. Leave blank otherwise.>
===SCRIPT===
<if VERDICT is "revised", put the full revised dialogue here, every line starting with ALEX: or JORDAN:; if VERDICT is "approved" or "order_rewrite", leave this section empty>

Do NOT wrap in JSON, markdown code fences, or any other envelope. Plain text only."""


def review_script(
    draft: str,
    ep_num: int,
    ep_title: str,
    episode_date: date,
    podcasts_dir: Path,
    db_path: Path,
    anthropic_key: str,
    model: str = "claude-sonnet-4-5-20250929",
) -> Dict:
    """
    Main entry point. Returns:
        {"final_script": str, "notes": str, "changed": bool}
    Falls through to the draft unchanged on any error.
    """
    if not anthropic_key:
        return {
            "final_script": draft,
            "notes": "Editor skipped: no API key",
            "changed": False,
            "verdict": "approved",
            "rewrite_reason": "",
        }

    prior_excerpts = _load_prior_script_excerpts(podcasts_dir, episode_date, days=PRIOR_DAYS)
    notes_block = _load_recent_notes(db_path, episode_date, days=NOTES_DAYS)
    themes_block = _load_latest_themes_block(db_path, episode_date)

    user_prompt = _build_user_prompt(
        ep_num=ep_num,
        ep_title=ep_title,
        episode_date=episode_date,
        draft=draft,
        prior_excerpts=prior_excerpts,
        notes_block=notes_block,
        themes_block=themes_block,
    )

    # Budget enough output tokens to echo back the full script in a JSON
    # "script" field. Rule of thumb: ~1.4 tokens per word, plus JSON overhead.
    draft_tokens_est = int(len(draft.split()) * 1.4) + 500
    max_out = max(8000, min(draft_tokens_est + 1500, 32000))

    try:
        client = anthropic.Anthropic(api_key=anthropic_key)
        resp = client.messages.create(
            model=model,
            max_tokens=max_out,
            system=EDITOR_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = resp.content[0].text.strip()
    except Exception as e:
        log.error("Editor call failed: %s", e)
        return {
            "final_script": draft,
            "notes": f"Editor error: {e}",
            "changed": False,
            "verdict": "approved",
            "rewrite_reason": "",
        }

    parsed = _parse_editor_response(raw)
    if parsed is None:
        log.warning("Editor output unparseable; keeping draft")
        return {
            "final_script": draft,
            "notes": "Editor output unparseable, kept draft",
            "changed": False,
            "verdict": "approved",
            "rewrite_reason": "",
        }

    notes = parsed["notes"] or "No notes."
    verdict = parsed["verdict"]

    if verdict == "order_rewrite":
        reason = parsed.get("rewrite_reason") or notes
        return {
            "final_script": draft,
            "notes": notes,
            "changed": False,
            "verdict": "order_rewrite",
            "rewrite_reason": reason,
        }

    if verdict == "approved":
        return {
            "final_script": draft,
            "notes": notes,
            "changed": False,
            "verdict": "approved",
            "rewrite_reason": "",
        }

    # verdict == "revised"
    final = _sanitize_dialogue(parsed["script"])
    if not final:
        return {
            "final_script": draft,
            "notes": "Editor said revised but returned empty script; kept draft",
            "changed": False,
            "verdict": "approved",
            "rewrite_reason": "",
        }

    return {
        "final_script": final,
        "notes": notes,
        "changed": final != draft.strip(),
        "verdict": "revised",
        "rewrite_reason": "",
    }


# ──────────────────────────────────────────────────────────────────────────────
# Context builders
# ──────────────────────────────────────────────────────────────────────────────

def _load_prior_script_excerpts(podcasts_dir: Path, episode_date: date,
                                 days: int) -> str:
    """Pull a compact excerpt (first ~30 lines) from each prior script."""
    if not podcasts_dir.exists():
        return ""
    dates = [(episode_date - timedelta(days=i)).isoformat() for i in range(1, days + 1)]
    chunks: List[str] = []
    remaining = _MAX_PRIOR_CHARS
    for d in dates:
        for path in sorted(podcasts_dir.glob(f"podcast_{d}_ep*.txt")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            lines = [ln for ln in text.splitlines() if ln.strip()]
            excerpt = "\n".join(lines[:30])
            header = f"--- {path.stem} ---"
            block = f"{header}\n{excerpt}"
            if len(block) > remaining:
                block = block[:remaining]
            chunks.append(block)
            remaining -= len(block)
            if remaining <= 0:
                return "\n\n".join(chunks)
    return "\n\n".join(chunks)


def _load_recent_notes(db_path: Path, episode_date: date, days: int) -> str:
    """
    Pull daily_notes rows from the `days` calendar days BEFORE episode_date.

    Same-day notes are deliberately excluded: the listener writes a note
    after listening to that day's episode, so it can only inform episodes
    on later days. Including the same day would surface answers in an
    episode the listener has already finished.
    """
    from scanner.database import list_daily_notes
    try:
        rows = list_daily_notes(db_path, limit=days * 2)
    except Exception as e:
        log.warning("Could not load daily_notes: %s", e)
        return ""

    cutoff = (episode_date - timedelta(days=days)).isoformat()
    today_iso = episode_date.isoformat()
    kept: List[str] = []
    total = 0
    for row in rows:
        d = row.get("report_date", "")
        if d < cutoff or d >= today_iso:
            continue
        content = (row.get("content") or "").strip()
        if not content:
            continue
        block = f"[{d}] {content}"
        if total + len(block) > _MAX_NOTES_CHARS:
            block = block[: _MAX_NOTES_CHARS - total]
            kept.append(block)
            break
        kept.append(block)
        total += len(block)
    return "\n\n".join(kept)


def _load_latest_themes_block(db_path: Path, episode_date: date) -> str:
    """
    Pull the most recent PM rollup whose window ended STRICTLY BEFORE today.
    Same-day rule applies here too: a rollup that includes today's note
    would leak post-listen feedback back into today's episode.
    """
    try:
        from scanner.database import list_weekly_themes
        from scanner.pm import format_themes_for_prompt
    except Exception as e:
        log.debug("PM rollup unavailable: %s", e)
        return ""

    try:
        rollups = list_weekly_themes(db_path, limit=12)
    except Exception as e:
        log.warning("Could not load weekly_themes: %s", e)
        return ""

    today_iso = episode_date.isoformat()
    rollup = next(
        (r for r in rollups if (r.get("week_end") or "") < today_iso),
        None,
    )
    if not rollup:
        return ""

    block = format_themes_for_prompt(rollup)
    if len(block) > _MAX_THEMES_CHARS:
        block = block[:_MAX_THEMES_CHARS]
    return block


def _build_user_prompt(ep_num: int, ep_title: str, episode_date: date,
                        draft: str, prior_excerpts: str, notes_block: str,
                        themes_block: str = "") -> str:
    date_str = episode_date.strftime("%A, %B %d, %Y")
    prior_section = prior_excerpts or "(no prior scripts available)"
    notes_section = notes_block or "(no audience notes in range)"
    themes_section = themes_block or "(no PM rollup available yet)"
    return (
        f"Episode {ep_num}: {ep_title} — {date_str}\n\n"
        "=== PRIOR EPISODE EXCERPTS (for repetition check) ===\n"
        f"{prior_section}\n\n"
        "=== AUDIENCE DAILY NOTES (raw, recent) ===\n"
        f"{notes_section}\n\n"
        "=== PM ROLLUP (recurring themes, open questions, underserved topics) ===\n"
        f"{themes_section}\n\n"
        "=== DRAFT SCRIPT TO REVIEW ===\n"
        f"{draft}\n\n"
        "Return your response in the three-section format now."
    )


# ──────────────────────────────────────────────────────────────────────────────
# Parsing helpers
# ──────────────────────────────────────────────────────────────────────────────

_SCRIPT_DELIM_RE = re.compile(r"^\s*=+\s*SCRIPT\s*=+\s*$", re.IGNORECASE | re.MULTILINE)
_NOTES_RE = re.compile(r"^\s*NOTES\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_VERDICT_RE = re.compile(
    r"^\s*VERDICT\s*:\s*(approved|revised|order[_ ]rewrite)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_REWRITE_REASON_RE = re.compile(
    r"^\s*REWRITE[_ ]REASON\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE,
)
# Legacy fallback — older prompt used CHANGED: yes|no
_CHANGED_RE = re.compile(r"^\s*CHANGED\s*:\s*(yes|no|true|false)\s*$",
                          re.IGNORECASE | re.MULTILINE)


def _parse_editor_response(raw: str) -> Optional[Dict]:
    """Parse the editor's four-section output. Returns None on failure."""
    if not raw:
        return None
    text = re.sub(r"^```[a-zA-Z]*\s*\n?", "", raw.strip())
    text = re.sub(r"\n?```\s*$", "", text).strip()

    m_delim = _SCRIPT_DELIM_RE.search(text)
    head = text[: m_delim.start()] if m_delim else text
    tail = text[m_delim.end():] if m_delim else ""

    m_notes = _NOTES_RE.search(head)
    if not m_notes:
        return None

    m_verdict = _VERDICT_RE.search(head)
    if m_verdict:
        v = m_verdict.group(1).lower().replace(" ", "_")
        verdict = "order_rewrite" if v.startswith("order") else v
    else:
        # Legacy fallback
        m_changed = _CHANGED_RE.search(head)
        if not m_changed:
            return None
        verdict = "revised" if m_changed.group(1).lower() in ("yes", "true") else "approved"

    m_reason = _REWRITE_REASON_RE.search(head)
    rewrite_reason = m_reason.group(1).strip() if m_reason else ""

    return {
        "notes": m_notes.group(1).strip(),
        "verdict": verdict,
        "rewrite_reason": rewrite_reason,
        "script": tail.strip(),
    }


def _sanitize_dialogue(text: str) -> str:
    """Keep only ALEX:/JORDAN: lines, strip markdown/stage-directions."""
    cleaned: List[str] = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        if not re.match(r"^(ALEX|JORDAN)\s*:", line, re.IGNORECASE):
            continue
        line = re.sub(r"^(alex|jordan)\s*:",
                       lambda m: m.group(1).upper() + ":",
                       line, flags=re.IGNORECASE)
        line = re.sub(r"[\*_]", "", line)
        line = re.sub(r"\[[^\]]+\]", "", line)
        cleaned.append(line)
    return "\n".join(cleaned).strip()
