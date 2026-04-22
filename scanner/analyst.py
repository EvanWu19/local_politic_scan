"""
Data Analyst agent — scores each tracked politician on the consistency
of their stated positions across the events we've recorded.

Inputs (per politician):
  • All `politician_events` rows linked to the politician
  • Joined `events.summary` and `events.categories` for context

Output (one row per politician per run, in `consistency_scores`):
  • score: 0.0 (volatile) .. 1.0 (rock-solid)
  • verdict: consistent | mixed | inconsistent | insufficient
  • summary: one-paragraph plain-English assessment
  • stable_positions: [{topic, position, evidence_event_ids}]
  • shifts:           [{topic, from, to, when, evidence_event_ids}]

Run via `python main.py analyst` or programmatically. Each run inserts a
NEW row (no UPSERT) so the listener can later see how the assessment
evolves over time.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import anthropic

from config import Config as _Cfg

log = logging.getLogger(__name__)

_LOCALE = ", ".join(p for p in [_Cfg.CITY, _Cfg.COUNTY, _Cfg.STATE] if p) or "the local area"

MIN_EVENTS = 3        # below this, we record verdict=insufficient and skip Claude
MAX_EVENTS = 60       # cap context size — most-recent first
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

ANALYST_SYSTEM = f"""You are a Data Analyst working for a daily local-politics
podcast aimed at a first-time voter in {_LOCALE}. Your job is to assess how
consistent a single politician's positions have been across the events the
team has tracked for them.

You will receive:
  • The politician's name, office, party, and level
  • A reverse-chronological list of events they were linked to, with date,
    title, the event summary, and their role + stance for that event

Your job:
  1. Identify the small set of TOPICS this politician has acted on.
     Topics are short labels like "property tax", "school funding",
     "transit", "policing", "housing zoning", "labor". Two events about
     the same bill belong to the same topic.
  2. For each topic, decide whether their position has been STABLE or has
     SHIFTED. A stable position means every linked event points the same
     direction (support / oppose). A shift means the same politician took
     a clearly opposing stance on the same topic across two different events.
  3. Be conservative. Do NOT call something a shift just because the wording
     differs — the underlying position must actually flip. Procedural votes
     and "mentioned" links are weak evidence; weight clear sponsor /
     voted_yes / voted_no / opponent links far more heavily.
  4. Score the politician's overall consistency on a 0.0–1.0 scale:
       1.0  = every topic stable, no shifts
       0.7  = mostly stable, one minor shift
       0.4  = several shifts, hard to predict
       0.0  = chaotic / contradicts themselves repeatedly
     Ignore low-evidence topics (only one weak link) when scoring.
  5. Pick a one-word VERDICT: consistent | mixed | inconsistent
  6. Write a one-paragraph (2–4 sentences) plain-English summary the
     listener can hear on the podcast — neutral tone, no spin words like
     "flip-flopper" or "principled".

OUTPUT FORMAT — strict JSON, one object, no preamble, no markdown fences:

{{
  "score": <float 0.0..1.0>,
  "verdict": "<consistent|mixed|inconsistent>",
  "summary": "<one paragraph>",
  "stable_positions": [
    {{"topic": "<short label>", "position": "support|oppose|neutral",
      "evidence_event_ids": [<int>, ...]}}
  ],
  "shifts": [
    {{"topic": "<short label>", "from": "support|oppose|neutral",
      "to": "support|oppose|neutral", "when": "<YYYY-MM-DD or rough>",
      "evidence_event_ids": [<int>, ...]}}
  ]
}}

If there is genuinely not enough signal, return score=null, verdict=
"inconsistent", and arrays empty — but produce a one-sentence summary
explaining that.
"""


def analyze_all(db_path: Path, anthropic_key: str,
                level: Optional[str] = None,
                min_events: int = MIN_EVENTS,
                model: str = DEFAULT_MODEL) -> List[Dict]:
    """
    Score every politician with at least `min_events` linked events.
    Returns a list of saved score rows.
    """
    if not anthropic_key:
        log.warning("Analyst: no anthropic_key, skipping")
        return []

    from scanner.database import list_politicians
    pols = list_politicians(db_path, level=level, min_events=1)
    saved: List[Dict] = []
    for p in pols:
        try:
            row = analyze_one(
                db_path=db_path,
                anthropic_key=anthropic_key,
                politician_id=p["id"],
                politician_name=p["name"],
                min_events=min_events,
                model=model,
            )
        except Exception as e:
            log.error("Analyst failed for %s: %s", p.get("name"), e)
            continue
        if row:
            saved.append(row)
    return saved


def analyze_one(db_path: Path, anthropic_key: str, politician_id: int,
                politician_name: str, min_events: int = MIN_EVENTS,
                model: str = DEFAULT_MODEL) -> Optional[Dict]:
    """
    Score a single politician. Saves a row to consistency_scores even if
    the verdict is `insufficient` (so we have a record we tried).
    Returns the saved row dict, or None on hard failure.
    """
    from scanner.database import (
        save_consistency_score, get_latest_consistency_score,
    )

    pol_meta, events = _load_politician_events(db_path, politician_id)
    if not pol_meta:
        log.warning("Analyst: politician_id=%s not found", politician_id)
        return None

    if not events:
        log.info("Analyst: %s has no linked events — skipping", politician_name)
        return None

    window_start = min(e["date"] for e in events if e.get("date")) or ""
    window_end = max(e["date"] for e in events if e.get("date")) or ""

    if len(events) < min_events:
        save_consistency_score(
            db_path=db_path,
            politician_id=politician_id,
            politician_name=politician_name,
            window_start=window_start, window_end=window_end,
            event_count=len(events),
            score=None, verdict="insufficient",
            summary=(f"Only {len(events)} tracked event(s) for "
                     f"{politician_name}; need at least {min_events} to score "
                     "consistency."),
            stable_positions=[], shifts=[],
        )
        return get_latest_consistency_score(db_path, politician_id)

    user_prompt = _build_user_prompt(pol_meta, events)
    try:
        client = anthropic.Anthropic(api_key=anthropic_key)
        resp = client.messages.create(
            model=model,
            max_tokens=2500,
            system=ANALYST_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = resp.content[0].text.strip()
    except Exception as e:
        log.error("Analyst Claude call failed for %s: %s", politician_name, e)
        return None

    parsed = _parse_analyst_output(raw)
    if parsed is None:
        log.warning("Analyst: output for %s unparseable, not saving",
                    politician_name)
        return None

    save_consistency_score(
        db_path=db_path,
        politician_id=politician_id,
        politician_name=politician_name,
        window_start=window_start, window_end=window_end,
        event_count=len(events),
        score=parsed.get("score"),
        verdict=parsed.get("verdict") or "mixed",
        summary=parsed.get("summary") or "",
        stable_positions=parsed.get("stable_positions") or [],
        shifts=parsed.get("shifts") or [],
    )
    saved = get_latest_consistency_score(db_path, politician_id)
    log.info("Analyst: scored %s -> %s (%s) over %d events",
             politician_name, saved.get("score"), saved.get("verdict"),
             len(events))
    return saved


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _load_politician_events(db_path: Path,
                             politician_id: int) -> tuple[Optional[Dict], List[Dict]]:
    """Return (politician_row, [event rows joined with role/stance])."""
    from scanner.database import get_connection
    with get_connection(db_path) as conn:
        pol = conn.execute(
            "SELECT * FROM politicians WHERE id = ?", (politician_id,)
        ).fetchone()
        if not pol:
            return None, []
        rows = conn.execute(
            """SELECT e.id, e.title, e.summary, e.date, e.level, e.type,
                      e.categories, pe.role, pe.stance, pe.notes
               FROM events e
               JOIN politician_events pe ON pe.event_id = e.id
               WHERE pe.politician_id = ?
               ORDER BY e.date DESC LIMIT ?""",
            (politician_id, MAX_EVENTS),
        ).fetchall()
    return dict(pol), [dict(r) for r in rows]


def _build_user_prompt(pol: Dict, events: List[Dict]) -> str:
    header = (
        f"Politician: {pol.get('name','')}\n"
        f"Office:     {pol.get('office','(unknown)')}\n"
        f"Party:      {pol.get('party','(unknown)')}\n"
        f"Level:      {pol.get('level','(unknown)')}\n"
        f"District:   {pol.get('district','(unknown)')}\n"
        f"Tracked events ({len(events)}, most recent first):\n"
    )
    lines: List[str] = []
    for e in events:
        cats = ""
        try:
            tags = json.loads(e.get("categories") or "[]")
            if tags:
                cats = f"  [tags: {', '.join(tags[:5])}]"
        except Exception:
            pass
        summary = (e.get("summary") or "").strip().replace("\n", " ")
        if len(summary) > 350:
            summary = summary[:350] + "…"
        lines.append(
            f"#{e['id']}  {e.get('date','????-??-??')}  "
            f"role={e.get('role','mentioned')}  stance={e.get('stance','unknown')}\n"
            f"  title: {(e.get('title') or '').strip()}\n"
            f"  summary: {summary or '(no summary)'}{cats}"
        )
    return header + "\n" + "\n\n".join(lines) + "\n\nReturn the JSON object now."


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*\n?")
_FENCE_END_RE = re.compile(r"\n?```\s*$")


def _parse_analyst_output(raw: str) -> Optional[Dict]:
    """Strict JSON parse. Tolerates a leading/trailing markdown fence."""
    if not raw:
        return None
    text = _FENCE_RE.sub("", raw.strip())
    text = _FENCE_END_RE.sub("", text).strip()
    # Some models add a leading "Here is the JSON:" — strip up to first {
    brace = text.find("{")
    if brace > 0:
        text = text[brace:]
    try:
        obj = json.loads(text)
    except Exception as e:
        log.warning("Analyst JSON parse failed: %s", e)
        return None
    if not isinstance(obj, dict):
        return None

    # Coerce score to float | None and verdict to a known token
    score = obj.get("score")
    if isinstance(score, (int, float)):
        score = max(0.0, min(1.0, float(score)))
    else:
        score = None
    verdict = (obj.get("verdict") or "").lower().strip()
    if verdict not in ("consistent", "mixed", "inconsistent", "insufficient"):
        verdict = "mixed"

    return {
        "score": score,
        "verdict": verdict,
        "summary": (obj.get("summary") or "").strip(),
        "stable_positions": obj.get("stable_positions") or [],
        "shifts": obj.get("shifts") or [],
    }


def format_score_for_prompt(row: Dict) -> str:
    """Render a saved score row as a compact prompt-friendly block."""
    if not row:
        return ""
    lines = [
        f"{row.get('politician_name','?')}: "
        f"verdict={row.get('verdict','?')} "
        f"score={row.get('score','?')} "
        f"(over {row.get('event_count',0)} events, "
        f"{row.get('window_start','?')}..{row.get('window_end','?')})",
    ]
    if row.get("summary"):
        lines.append(f"  Summary: {row['summary']}")
    stable = row.get("stable_positions") or []
    if stable:
        lines.append("  Stable positions:")
        for s in stable:
            lines.append(
                f"    - {s.get('topic','?')}: {s.get('position','?')}"
            )
    shifts = row.get("shifts") or []
    if shifts:
        lines.append("  Shifts:")
        for s in shifts:
            lines.append(
                f"    - {s.get('topic','?')}: "
                f"{s.get('from','?')} -> {s.get('to','?')} "
                f"({s.get('when','?')})"
            )
    return "\n".join(lines)
