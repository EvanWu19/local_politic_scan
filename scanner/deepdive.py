"""
Deep-dive (专题) episode generator — produces one ~30-minute, single-
candidate podcast episode focused on a specific person the listener
has asked about in their daily notes.

Flow (per candidate):
  1. Resolve name → politician row (case-insensitive)
  2. Pull their linked events + role/stance from `politician_events`
  3. Pull their most recent consistency-score row (if any)
  4. Build a Claude dialogue prompt that covers:
       - Who they are (office, district, ballot context)
       - Their voting record / positions by topic
       - The Analyst's stable positions + shifts, straight up
       - What's genuinely contested on their ballot
       - What a first-time voter in the listener's districts should weigh
  5. Run the script through the Editor (rewrite loop allowed)
  6. Synthesize TTS (unless --no-audio)
  7. Save as `podcast_<date>_deepdive_<slug>.txt` + `.mp3`

Triggering:
  • Manually:  `python main.py deepdive "Sidney Katz"`
  • Auto: main.py's `cmd_publish` reads
    `weekly_themes.listener_candidate_interest` and calls this module
    once per name it finds.
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

MAX_EVENTS = 40
DEFAULT_TARGET_WORDS = 3800   # ~30 min at ~130 wpm


def generate_deep_dive(db_path: Path, podcasts_dir: Path, anthropic_key: str,
                        candidate_name: str,
                        openai_key: str = "",
                        target_date: Optional[date] = None,
                        script_model: str = None,
                        tts_model: str = None,
                        no_audio: bool = False,
                        skip_editor: bool = False) -> Optional[Dict]:
    """
    Generate a single deep-dive episode for `candidate_name`.
    Returns a result dict (like generate_podcast_episodes items) or None
    if the candidate can't be resolved / has no usable record.
    """
    if not anthropic_key:
        log.warning("Deep dive: no anthropic_key, skipping")
        return None

    script_model = script_model or _Cfg.PODCAST_SCRIPT_MODEL
    tts_model = tts_model or _Cfg.PODCAST_TTS_MODEL
    target_date = target_date or date.today()
    podcasts_dir.mkdir(parents=True, exist_ok=True)

    pol = _resolve_candidate(db_path, candidate_name)
    if not pol:
        log.warning("Deep dive: candidate %r not in politicians table", candidate_name)
        return None

    events = _load_events_for_candidate(db_path, pol["id"])
    score = _load_latest_consistency(db_path, pol["id"])
    if not events and not score:
        log.info("Deep dive: no record for %s — need events or consistency score",
                 pol["name"])
        return None

    claude = anthropic.Anthropic(api_key=anthropic_key)
    date_str = target_date.strftime("%Y-%m-%d")
    ep_slug = f"deepdive_{_slugify(pol['name'])}"
    ep_title = f"Deep Dive: {pol['name']}"

    # Draft #1 — whole script in one Claude call (single focus, manageable
    # size; we don't need intro/segment/outro split for a one-topic show).
    draft_script = _write_deep_dive_script(
        client=claude, model=script_model,
        pol=pol, events=events, score=score, target_date=target_date,
        db_path=db_path,
    )
    draft_words = len(draft_script.split())
    draft_path = podcasts_dir / f"podcast_{date_str}_{ep_slug}.draft.txt"
    draft_path.write_text(draft_script, encoding="utf-8")
    log.info("Deep dive draft for %s: %d words", pol["name"], draft_words)

    # Editor pass (one-shot; rewrite loop via the same path podcast.py uses)
    editor_notes = ""
    editor_changed = False
    rewrite_attempted = False
    rewrite_reason = ""
    full_script = draft_script
    if not skip_editor:
        from scanner.editor import review_script
        review = review_script(
            draft=draft_script, ep_num=1, ep_title=ep_title,
            episode_date=target_date, podcasts_dir=podcasts_dir,
            db_path=db_path, anthropic_key=anthropic_key, model=script_model,
        )
        if review.get("verdict") == "order_rewrite":
            rewrite_attempted = True
            rewrite_reason = review.get("rewrite_reason", "")
            log.warning("Deep dive: editor ordered rewrite (%s)", rewrite_reason)
            draft_v2 = _write_deep_dive_script(
                client=claude, model=script_model,
                pol=pol, events=events, score=score,
                target_date=target_date, db_path=db_path,
                rewrite_reason=rewrite_reason,
            )
            (podcasts_dir / f"podcast_{date_str}_{ep_slug}.rewrite.txt").write_text(
                draft_v2, encoding="utf-8"
            )
            review = review_script(
                draft=draft_v2, ep_num=1, ep_title=ep_title,
                episode_date=target_date, podcasts_dir=podcasts_dir,
                db_path=db_path, anthropic_key=anthropic_key, model=script_model,
            )
        full_script = review["final_script"]
        editor_notes = review["notes"]
        editor_changed = review["changed"]
        log.info("Deep dive editor: verdict=%s changed=%s — %s",
                 review.get("verdict", "?"), editor_changed, editor_notes)

    word_count = len(full_script.split())
    script_path = podcasts_dir / f"podcast_{date_str}_{ep_slug}.txt"
    script_path.write_text(full_script, encoding="utf-8")

    editor_meta = {
        "candidate": pol["name"],
        "office": pol.get("office", ""),
        "date": date_str,
        "draft_words": draft_words,
        "final_words": word_count,
        "changed": editor_changed,
        "notes": editor_notes,
        "rewrite_attempted": rewrite_attempted,
        "rewrite_reason": rewrite_reason,
        "event_count": len(events),
        "has_consistency_score": score is not None,
    }
    (podcasts_dir / f"podcast_{date_str}_{ep_slug}.editor.json").write_text(
        json.dumps(editor_meta, indent=2), encoding="utf-8"
    )

    audio_path = ""
    duration = 0
    if not no_audio and openai_key:
        import openai
        from scanner.podcast import _synthesize_dialogue
        audio_path_p = podcasts_dir / f"podcast_{date_str}_{ep_slug}.mp3"
        openai_client = openai.OpenAI(api_key=openai_key)
        duration = _synthesize_dialogue(
            openai_client, full_script, audio_path_p, tts_model,
        )
        audio_path = str(audio_path_p)

    return {
        "episode_num": 0,
        "episode_title": ep_title,
        "episode_slug": ep_slug,
        "candidate": pol["name"],
        "script": full_script,
        "script_path": str(script_path),
        "draft_path": str(draft_path),
        "editor_notes": editor_notes,
        "editor_changed": editor_changed,
        "audio_path": audio_path,
        "duration_seconds": duration,
        "word_count": word_count,
        "status": "done" if audio_path else "script_only",
    }


# ──────────────────────────────────────────────────────────────────────────────
# Script writing
# ──────────────────────────────────────────────────────────────────────────────

def _write_deep_dive_script(client: anthropic.Anthropic, model: str,
                             pol: Dict, events: List[Dict],
                             score: Optional[Dict], target_date: date,
                             db_path: Path,
                             rewrite_reason: str = "") -> str:
    """Drive one Claude call that produces the whole ~30 min dialogue."""
    from scanner.podcast import DIALOGUE_SYSTEM, _claude_dialogue, _load_ballot_block

    events_text = _render_events(events)
    score_text = _render_score(score)
    ballot_block = _load_ballot_block(db_path, target_date)

    rewrite_note = ""
    if rewrite_reason:
        rewrite_note = (
            "\n\nHARD CONSTRAINT — the Editor rejected the previous draft. "
            "Fix specifically: " + rewrite_reason +
            "\nDo not reuse the same framings or anecdotes."
        )

    user = (
        f"Write a SINGLE ~30-minute deep-dive episode entirely about "
        f"{pol['name']} ({pol.get('party', 'unknown')}), "
        f"{pol.get('office', 'unknown office')}, "
        f"level={pol.get('level', '(unknown)')}, "
        f"district={pol.get('district', '(unknown)')}. "
        f"The listener asked for this episode by name in their daily notes.\n\n"
        f"TRACKED RECORD ({len(events)} events, most recent first):\n"
        f"{events_text}\n\n"
        f"ANALYST'S CONSISTENCY VERDICT:\n{score_text}\n\n"
        "EPISODE STRUCTURE (approximate):\n"
        "  1. Opening — why this person matters to the listener's ballot (~2 min)\n"
        "  2. Bio + office context — who they are, what the seat actually does (~4 min)\n"
        "  3. Their record on 3–5 topics that showed up in the events — "
        "each topic: what they did, how they voted/spoke, impact on residents (~18 min)\n"
        "  4. The Analyst's verdict — report the consistency score verbatim, "
        "walk through any shifts the Analyst flagged (~3 min)\n"
        "  5. What this means for the listener — what opposing candidates exist, "
        "what specific things to watch for before election day (~3 min)\n\n"
        f"Target length: ~{DEFAULT_TARGET_WORDS} words of ALEX/JORDAN dialogue.\n"
        "HARD RULES:\n"
        "  - Neutral tone. Present the record; don't spin.\n"
        "  - If the record is thin, say so out loud.\n"
        "  - Never tell the listener to go figure out their own candidates.\n"
        "  - Speak as though we already know the listener's races (they do).\n"
        f"{rewrite_note}"
    )
    suffix = f"\n\n{ballot_block}\n" if ballot_block else ""
    # _claude_dialogue pins DIALOGUE_SYSTEM + returns cleaned ALEX/JORDAN text
    return _claude_dialogue(client, model, user + suffix)


# ──────────────────────────────────────────────────────────────────────────────
# Data loaders
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_candidate(db_path: Path, name: str) -> Optional[Dict]:
    from scanner.database import get_connection
    needle = (name or "").strip()
    if not needle:
        return None
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM politicians WHERE LOWER(name) = LOWER(?)", (needle,),
        ).fetchone()
        if row:
            return dict(row)
        # Looser match: both first and last name tokens
        parts = needle.split()
        if len(parts) >= 2:
            first, last = parts[0], parts[-1]
            row = conn.execute(
                "SELECT * FROM politicians "
                "WHERE LOWER(name) LIKE LOWER(?) AND LOWER(name) LIKE LOWER(?) "
                "LIMIT 1",
                (f"%{first}%", f"%{last}%"),
            ).fetchone()
            if row:
                return dict(row)
    return None


def _load_events_for_candidate(db_path: Path, politician_id: int) -> List[Dict]:
    from scanner.database import get_connection
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT e.id, e.title, e.summary, e.date, e.level, e.type,
                      e.categories, pe.role, pe.stance, pe.notes
               FROM events e
               JOIN politician_events pe ON pe.event_id = e.id
               WHERE pe.politician_id = ?
               ORDER BY e.date DESC LIMIT ?""",
            (politician_id, MAX_EVENTS),
        ).fetchall()
    return [dict(r) for r in rows]


def _load_latest_consistency(db_path: Path, politician_id: int) -> Optional[Dict]:
    from scanner.database import get_latest_consistency_score
    try:
        return get_latest_consistency_score(db_path, politician_id)
    except Exception as e:
        log.debug("Consistency score load failed: %s", e)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Rendering
# ──────────────────────────────────────────────────────────────────────────────

def _render_events(events: List[Dict]) -> str:
    if not events:
        return "(no tracked events — the show should say so out loud)"
    lines: List[str] = []
    for e in events:
        cats = ""
        try:
            tags = json.loads(e.get("categories") or "[]")
            if tags:
                cats = f"  [tags: {', '.join(tags[:4])}]"
        except Exception:
            pass
        summary = (e.get("summary") or "").strip().replace("\n", " ")
        if len(summary) > 280:
            summary = summary[:280] + "…"
        lines.append(
            f"- #{e['id']}  {e.get('date', '????-??-??')}  "
            f"role={e.get('role', 'mentioned')}  stance={e.get('stance', 'unknown')}\n"
            f"    {e.get('title', '').strip()}\n"
            f"    {summary}{cats}"
        )
    return "\n".join(lines)


def _render_score(score: Optional[Dict]) -> str:
    if not score:
        return "(no consistency score on file — don't make one up)"
    lines = [
        f"verdict: {score.get('verdict', 'unknown')}",
        f"score:   {score.get('score')}",
        f"window:  {score.get('window_start', '')}..{score.get('window_end', '')}",
        f"events scored: {score.get('event_count', 0)}",
        f"summary: {(score.get('summary') or '').strip()}",
    ]
    stable = score.get("stable_positions") or []
    if stable:
        lines.append("stable positions:")
        for s in stable:
            lines.append(
                f"  - {s.get('topic')}: {s.get('position')} "
                f"(events={s.get('evidence_event_ids', [])})"
            )
    shifts = score.get("shifts") or []
    if shifts:
        lines.append("shifts:")
        for s in shifts:
            lines.append(
                f"  - {s.get('topic')}: {s.get('from')} → {s.get('to')} "
                f"on {s.get('when', '?')} "
                f"(events={s.get('evidence_event_ids', [])})"
            )
    return "\n".join(lines)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    return _SLUG_RE.sub("_", (name or "").lower()).strip("_") or "candidate"
