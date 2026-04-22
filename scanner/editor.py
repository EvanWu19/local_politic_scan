"""
Editor agent — reviews a draft podcast episode script before TTS.

Reads:
  • the last 3 days of prior episode scripts (to catch verbatim repetition)
  • the last 7 days of audience daily notes (to surface open questions)

Returns {"final_script", "notes", "changed"}. If Claude returns anything we
can't parse, we fall back to the draft unchanged.
"""
from __future__ import annotations

import json
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

PRIOR_DAYS = 3
NOTES_DAYS = 7
_MAX_PRIOR_CHARS = 6000
_MAX_NOTES_CHARS = 2500

EDITOR_SYSTEM = f"""You are the Editor for a local politics podcast aimed at a first-time voter in {_LOCALE}.{_DISTRICT_BLOCK}

An Author agent has written today's episode script as a two-host dialogue
(ALEX = presenter, JORDAN = curious co-host). Your job is a tight final pass
before text-to-speech synthesis.

CHECK FOR:
1. REPETITION — If today's script repeats whole framings, anecdotes, or taglines
   verbatim from the last few days, rewrite just those lines to briefly
   acknowledge prior coverage ("we touched on this Tuesday") and add a fresh
   angle. Do NOT remove the story itself.
2. AUDIENCE QUESTIONS — The listener writes daily notes. If a note raises a
   question that today's topics can speak to, add 1–2 lines where Alex or
   Jordan briefly answers it. Never invent facts not supported by the script.
3. FORMAT DISCIPLINE — Every line must begin `ALEX:` or `JORDAN:` with no
   stage directions, markdown, brackets, asterisks, or emoji. Remove any line
   that is not dialogue. Keep speakers alternating naturally.

HARD RULES:
- Preserve overall length and tone. Do not gut the script.
- Make the SMALLEST change that fixes a real problem. If the draft is clean,
  return it unchanged.
- NEVER invent facts the draft doesn't contain.

OUTPUT — a single JSON object with exactly these keys, and nothing else:
{{
  "notes": "<one short sentence describing what you changed and why; 'No changes needed.' if untouched>",
  "changed": <true | false>,
  "script": "<the full final dialogue — every line must start with ALEX: or JORDAN:>"
}}"""


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
        return {"final_script": draft, "notes": "Editor skipped: no API key", "changed": False}

    prior_excerpts = _load_prior_script_excerpts(podcasts_dir, episode_date, days=PRIOR_DAYS)
    notes_block = _load_recent_notes(db_path, episode_date, days=NOTES_DAYS)

    user_prompt = _build_user_prompt(
        ep_num=ep_num,
        ep_title=ep_title,
        episode_date=episode_date,
        draft=draft,
        prior_excerpts=prior_excerpts,
        notes_block=notes_block,
    )

    try:
        client = anthropic.Anthropic(api_key=anthropic_key)
        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            system=EDITOR_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = resp.content[0].text.strip()
    except Exception as e:
        log.error("Editor call failed: %s", e)
        return {"final_script": draft, "notes": f"Editor error: {e}", "changed": False}

    parsed = _parse_editor_json(raw)
    if not parsed:
        log.warning("Editor JSON unparseable; keeping draft")
        return {"final_script": draft, "notes": "Editor output unparseable, kept draft", "changed": False}

    final = _sanitize_dialogue(parsed.get("script", ""))
    notes = (parsed.get("notes") or "").strip() or "No notes."
    changed = bool(parsed.get("changed")) and final and final != draft.strip()

    if not final:
        return {"final_script": draft, "notes": "Editor returned empty script, kept draft", "changed": False}

    return {
        "final_script": final,
        "notes": notes,
        "changed": changed,
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
    """Pull daily_notes rows within the past `days` (exclusive of today+1)."""
    from scanner.database import list_daily_notes
    try:
        rows = list_daily_notes(db_path, limit=days * 2)
    except Exception as e:
        log.warning("Could not load daily_notes: %s", e)
        return ""

    cutoff = (episode_date - timedelta(days=days)).isoformat()
    kept: List[str] = []
    total = 0
    for row in rows:
        d = row.get("report_date", "")
        if d < cutoff or d > episode_date.isoformat():
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


def _build_user_prompt(ep_num: int, ep_title: str, episode_date: date,
                        draft: str, prior_excerpts: str, notes_block: str) -> str:
    date_str = episode_date.strftime("%A, %B %d, %Y")
    prior_section = prior_excerpts or "(no prior scripts available)"
    notes_section = notes_block or "(no audience notes in range)"
    return (
        f"Episode {ep_num}: {ep_title} — {date_str}\n\n"
        "=== PRIOR EPISODE EXCERPTS (for repetition check) ===\n"
        f"{prior_section}\n\n"
        "=== AUDIENCE DAILY NOTES ===\n"
        f"{notes_section}\n\n"
        "=== DRAFT SCRIPT TO REVIEW ===\n"
        f"{draft}\n\n"
        "Return your JSON response now."
    )


# ──────────────────────────────────────────────────────────────────────────────
# Parsing helpers
# ──────────────────────────────────────────────────────────────────────────────

_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


def _parse_editor_json(raw: str) -> Optional[Dict]:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = _JSON_OBJ_RE.search(raw)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


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
