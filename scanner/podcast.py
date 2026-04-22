"""
Podcast generator: turns the daily digest into a ~2-hour two-host audio episode.

Pipeline:
  1. Pick top-N most relevant events from the DB for a given date
  2. Group into segments (Federal / State / County / Schools / Local)
  3. Use Claude to write natural two-host dialogue for each segment
  4. Use OpenAI TTS (alloy + nova voices) to synthesize each line
  5. Concatenate MP3 chunks → single episode file in podcasts/

Two hosts:
  • ALEX   — news presenter, reports the facts (onyx voice)
  • JORDAN — curious co-host, asks "why does this matter to me?" (nova voice)
"""
import io
import json
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import List, Dict, Tuple, Optional

import anthropic
import openai

from config import Config as _Cfg

log = logging.getLogger(__name__)

_LOCALE = ", ".join(p for p in [_Cfg.CITY, _Cfg.COUNTY, _Cfg.STATE] if p) or "the local area"
_STATE = _Cfg.STATE or "the state"
_COUNTY = _Cfg.COUNTY or "the county"
_SHOW_NAME = f"{_Cfg.CITY or _Cfg.COUNTY or 'Local'} Politics Today"
_FED_FOCUS = ", ".join(_Cfg.FEDERAL_KEYWORDS[:6]) if _Cfg.FEDERAL_KEYWORDS else "federal legislation that affects daily life"
_DISTRICTS = _Cfg.districts_profile()
_DISTRICT_BLOCK = f"\n\nLISTENER'S VOTING DISTRICTS — prioritize stories touching these offices:\n{_DISTRICTS}\n" if _DISTRICTS else ""

# ── Speaker/voice mapping ─────────────────────────────────────────────────────
ALEX = "ALEX"
JORDAN = "JORDAN"

# ── System prompt for dialogue generation (cached) ────────────────────────────
DIALOGUE_SYSTEM = f"""You are writing a natural two-host podcast conversation about
local politics for a first-time voter in {_LOCALE}.{_DISTRICT_BLOCK}

HOSTS:
  • ALEX   — The news presenter. Reads the facts, provides context, explains the issue.
            Warm, informed, but plain-spoken. Avoids jargon.
  • JORDAN — The curious co-host. Asks "why does this matter to me?" / "so what does
            that actually mean?" / "wait, who votes on this?" Plays the role of a
            smart-but-new voter figuring things out in real time.

STYLE RULES:
  - Strict format — one line per turn, like this:
      ALEX: <1–3 sentences>
      JORDAN: <1–3 sentences>
      ALEX: <1–3 sentences>
  - NO stage directions, sound effects, music cues, brackets, asterisks, or emoji.
  - Real conversational speech — contractions ("it's", "can't"), small filler words
    ("yeah", "right", "okay so"), genuine curiosity, pushbacks. Not AI-flat.
  - NO "listeners" / "audience" addresses. Just two people talking.
  - Connect every story to the listener's life: taxes, commute, schools, safety,
    what this person can actually DO (vote, attend hearing, etc).
  - Jordan should sometimes catch Alex's jargon and ask for plain-English.
  - Political balance: describe positions fairly; no editorializing.
  - Natural rhythm — not every line is a question; sometimes Jordan says "huh" or
    "okay that makes sense" and Alex continues.
  - CONTENT FOCUS: Cover policy trends, legislation, budgets, votes, and systemic issues.
    Do NOT spend time on individual crime incidents or accidents. If a story is about a
    statistic or trend (e.g., "crime rates up 15%") cover it; if it's about a specific
    person's arrest, death, or accident, skip it entirely.
  - LISTENER POSITIONING: This podcast is a personalized guide for ONE specific
    first-time voter whose district config is known to the tool. The hosts must
    NEVER tell the listener to "figure out who your candidate is", "look up your
    district", "find out who's on your ballot", or any equivalent. Candidate and
    district research is the TOOL's job, not the listener's. Speak as though we
    already know their races and are walking them through their ballot choices.

Output ONLY the dialogue lines in the ALEX/JORDAN format above. No preamble."""


# ── Voice per speaker (set from Config at runtime) ────────────────────────────
VOICES = {"ALEX": "onyx", "JORDAN": "nova"}


NUM_EPISODES = 4
_WORDS_PER_EVENT = 350    # rough estimate for fill-threshold math
_TARGET_WORDS_EP = 2500   # ~19 min at 130 wpm; + intro/outro ≈ 30 min per episode


def generate_podcast_episodes(db_path: Path, podcasts_dir: Path,
                               anthropic_key: str, openai_key: str,
                               target_date: Optional[date] = None,
                               script_model: str = "claude-sonnet-4-5-20250929",
                               tts_model: str = "tts-1",
                               alex_voice: str = "onyx",
                               jordan_voice: str = "nova",
                               no_audio: bool = False,
                               filter_incidents: bool = True,
                               skip_editor: bool = False) -> List[Dict]:
    """
    Produce 4 separate ~30-min episode files.
    Returns a list of result dicts, one per episode.
    """
    if not anthropic_key:
        raise RuntimeError("ANTHROPIC_API_KEY required to write podcast script")
    if not no_audio and not openai_key:
        raise RuntimeError("OPENAI_API_KEY required for TTS (or pass --no-audio)")

    VOICES["ALEX"] = alex_voice
    VOICES["JORDAN"] = jordan_voice
    target_date = target_date or date.today()
    podcasts_dir.mkdir(parents=True, exist_ok=True)

    # ── Load all events once ──────────────────────────────────────────────────
    from scanner.database import get_recent_events
    from scanner.processor import is_individual_incident

    all_events = get_recent_events(db_path, days=2, min_relevance=0.0)
    if not all_events:
        raise RuntimeError("No events in DB — run `python main.py scan` first")

    if filter_incidents:
        before = len(all_events)
        all_events = [ev for ev in all_events if not is_individual_incident(ev)]
        log.info("Incident filter: dropped %d individual-incident stories",
                 before - len(all_events))

    claude = anthropic.Anthropic(api_key=anthropic_key)
    openai_client = openai.OpenAI(api_key=openai_key) if not no_audio else None
    date_str = target_date.strftime("%Y-%m-%d")

    # ── 2. Get politician data for fill-in segments ───────────────────────────
    politicians_ranked = _get_politicians_ranked(db_path)
    pol_offset = 0  # how many politicians we've already covered

    # ── 3. Split events evenly across episodes (globally by relevance) ────────
    n = len(all_events)
    per_ep = max(1, n // NUM_EPISODES)
    event_slices: List[List[Dict]] = []
    for i in range(NUM_EPISODES):
        start = i * per_ep
        end = n if i == NUM_EPISODES - 1 else start + per_ep
        event_slices.append(all_events[start:end])

    results: List[Dict] = []

    for ep_num in range(1, NUM_EPISODES + 1):
        ep_slug = f"ep{ep_num}"
        ep_events = event_slices[ep_num - 1]
        log.info("=== Episode %d: %d events ===", ep_num, len(ep_events))

        # Group events by level for coherent presentation within each episode
        segments = _group_events_by_level(ep_events)

        # ── Fill sparse episodes with politician deep-dives ───────────────────
        est_words = len(ep_events) * _WORDS_PER_EVENT
        fill_threshold = _TARGET_WORDS_EP * 0.55  # below this, add politician content
        if est_words < fill_threshold:
            words_still_needed = _TARGET_WORDS_EP - est_words
            pols_to_add = max(2, int(words_still_needed // 450))
            pol_slice = politicians_ranked[pol_offset: pol_offset + pols_to_add]
            pol_offset += len(pol_slice)
            if pol_slice:
                segments.append(_make_politician_segment(pol_slice, is_final=False))
                log.info("  Adding %d politician spotlights to fill ~%d words",
                         len(pol_slice), words_still_needed)

        # Episode 4 always closes with remaining candidates / politician recap
        if ep_num == NUM_EPISODES:
            remaining_pols = politicians_ranked[pol_offset: pol_offset + 6]
            if remaining_pols:
                segments.append(_make_politician_segment(remaining_pols, is_final=True))
                log.info("  Final episode: added %d politician summaries",
                         len(remaining_pols))

        ep_title = _infer_episode_title(ep_num, segments)
        log.info("  Title: %s", ep_title)

        # ── Build avoid-list from PM rollup (framings/taglines to skip) ───────
        base_avoid_items = _load_avoid_list(db_path, target_date)
        avoid_block = _format_avoid_block(base_avoid_items)

        # ── Build ballot block so Author frames races as "X vs Y" ─────────────
        ballot_block = _load_ballot_block(db_path, target_date)

        # ── Write draft #1 ────────────────────────────────────────────────────
        draft_script = _build_full_script(
            claude=claude, model=script_model,
            target_date=target_date, ep_num=ep_num, ep_title=ep_title,
            segments=segments, avoid_block=avoid_block,
            ballot_block=ballot_block,
        )
        draft_words = len(draft_script.split())
        log.info("  Draft: %d words (~%d min)", draft_words, draft_words // 130)

        draft_fname = f"podcast_{date_str}_{ep_slug}.draft.txt"
        draft_path = podcasts_dir / draft_fname
        draft_path.write_text(draft_script, encoding="utf-8")

        # ── Editor pass (with optional rewrite loop, max 1 retry) ─────────────
        editor_notes = ""
        editor_changed = False
        rewrite_attempted = False
        rewrite_reason = ""
        if skip_editor:
            full_script = draft_script
            log.info("  Editor: skipped")
        else:
            from scanner.editor import review_script
            review = review_script(
                draft=draft_script,
                ep_num=ep_num,
                ep_title=ep_title,
                episode_date=target_date,
                podcasts_dir=podcasts_dir,
                db_path=db_path,
                anthropic_key=anthropic_key,
                model=script_model,
            )

            if review.get("verdict") == "order_rewrite":
                rewrite_attempted = True
                rewrite_reason = review.get("rewrite_reason", "")
                log.warning(
                    "  Editor ordered REWRITE — regenerating with extended avoid list. Reason: %s",
                    rewrite_reason,
                )
                extended_avoid = base_avoid_items + _parse_avoid_from_reason(rewrite_reason)
                avoid_block_v2 = _format_avoid_block(extended_avoid, strict=True)

                draft_script_v2 = _build_full_script(
                    claude=claude, model=script_model,
                    target_date=target_date, ep_num=ep_num, ep_title=ep_title,
                    segments=segments, avoid_block=avoid_block_v2,
                    ballot_block=ballot_block,
                )
                draft_v2_path = podcasts_dir / f"podcast_{date_str}_{ep_slug}.rewrite.txt"
                draft_v2_path.write_text(draft_script_v2, encoding="utf-8")

                review = review_script(
                    draft=draft_script_v2,
                    ep_num=ep_num,
                    ep_title=ep_title,
                    episode_date=target_date,
                    podcasts_dir=podcasts_dir,
                    db_path=db_path,
                    anthropic_key=anthropic_key,
                    model=script_model,
                )
                draft_script = draft_script_v2  # the rewrite becomes the baseline

            full_script = review["final_script"]
            editor_notes = review["notes"]
            editor_changed = review["changed"]
            log.info(
                "  Editor: verdict=%s changed=%s%s — %s",
                review.get("verdict", "?"),
                editor_changed,
                " (after rewrite)" if rewrite_attempted else "",
                editor_notes,
            )

        word_count = len(full_script.split())
        script_fname = f"podcast_{date_str}_{ep_slug}.txt"
        script_path = podcasts_dir / script_fname
        script_path.write_text(full_script, encoding="utf-8")

        editor_meta = {
            "episode_num": ep_num,
            "episode_title": ep_title,
            "date": date_str,
            "draft_words": draft_words,
            "final_words": word_count,
            "changed": editor_changed,
            "notes": editor_notes,
            "skip_editor": skip_editor,
            "rewrite_attempted": rewrite_attempted,
            "rewrite_reason": rewrite_reason,
            "avoid_list_size": len(base_avoid_items),
        }
        (podcasts_dir / f"podcast_{date_str}_{ep_slug}.editor.json").write_text(
            json.dumps(editor_meta, indent=2), encoding="utf-8"
        )

        if no_audio:
            results.append({
                "episode_num": ep_num,
                "episode_title": ep_title,
                "episode_slug": ep_slug,
                "script": full_script,
                "script_path": str(script_path),
                "draft_path": str(draft_path),
                "editor_notes": editor_notes,
                "editor_changed": editor_changed,
                "audio_path": "",
                "duration_seconds": 0,
                "word_count": word_count,
                "status": "script_only",
                "_events": ep_events,
            })
            continue

        audio_fname = f"podcast_{date_str}_{ep_slug}.mp3"
        audio_path = podcasts_dir / audio_fname
        duration = _synthesize_dialogue(openai_client, full_script, audio_path, tts_model)

        results.append({
            "episode_num": ep_num,
            "episode_title": ep_title,
            "episode_slug": ep_slug,
            "script": full_script,
            "script_path": str(script_path),
            "draft_path": str(draft_path),
            "editor_notes": editor_notes,
            "editor_changed": editor_changed,
            "audio_path": str(audio_path),
            "duration_seconds": duration,
            "word_count": word_count,
            "status": "done",
            "_events": ep_events,
        })

    # Save index JSON so the digest player can show episode titles
    index = {
        "date": date_str,
        "episodes": [
            {"num": ep["episode_num"], "title": ep["episode_title"],
             "slug": ep["episode_slug"], "word_count": ep["word_count"],
             "status": ep["status"]}
            for ep in results
        ],
    }
    (podcasts_dir / f"podcast_{date_str}_index.json").write_text(
        json.dumps(index, indent=2), encoding="utf-8"
    )

    return results


def generate_podcast(db_path: Path, podcasts_dir: Path,
                     anthropic_key: str, openai_key: str,
                     target_date: Optional[date] = None,
                     script_model: str = "claude-sonnet-4-5-20250929",
                     tts_model: str = "tts-1",
                     top_n: int = 8,
                     alex_voice: str = "onyx",
                     jordan_voice: str = "nova",
                     no_audio: bool = False) -> Dict:
    """Backward-compatible wrapper: generates all 4 episodes and returns the first."""
    results = generate_podcast_episodes(
        db_path=db_path,
        podcasts_dir=podcasts_dir,
        anthropic_key=anthropic_key,
        openai_key=openai_key,
        target_date=target_date,
        script_model=script_model,
        tts_model=tts_model,
        alex_voice=alex_voice,
        jordan_voice=jordan_voice,
        no_audio=no_audio,
    )
    return results[0] if results else {}


# ──────────────────────────────────────────────────────────────────────────────
# Script generation helpers
# ──────────────────────────────────────────────────────────────────────────────

_LEVEL_ORDER = ["federal", "state", "county", "school", "local"]
_LEVEL_LABELS = {
    "federal": f"Federal — {_FED_FOCUS}",
    "state":   f"{_STATE} State Legislature",
    "county":  f"{_COUNTY} Council",
    "school":  "School Board",
    "local":   "Local Services — Police, Fire, Health",
}
_LEVEL_SHORT = {
    "federal": "Federal", "state": _STATE,
    "county": _COUNTY, "school": "Schools", "local": "Local Services",
}


def _group_events_by_level(events: List[Dict]) -> List[Dict]:
    """Group a flat event list into per-level segment dicts, preserving relevance order."""
    by_level: Dict[str, List[Dict]] = {}
    for ev in events:
        lvl = ev.get("level", "county")
        by_level.setdefault(lvl, []).append(ev)
    return [
        {
            "key": lvl,
            "label": _LEVEL_LABELS.get(lvl, lvl.title()),
            "events": by_level[lvl],
            "intro_focus": _LEVEL_LABELS.get(lvl, lvl),
        }
        for lvl in _LEVEL_ORDER
        if lvl in by_level
    ]


def _get_politicians_ranked(db_path: Path) -> List[Dict]:
    """Return politicians sorted by recent mention count, each with their top events."""
    from scanner.database import get_connection
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT p.id, p.name, p.office, p.party, p.level,
                      COUNT(DISTINCT pe.event_id) AS mention_count
               FROM politicians p
               JOIN politician_events pe ON p.id = pe.politician_id
               GROUP BY p.id
               ORDER BY mention_count DESC
               LIMIT 25"""
        ).fetchall()
        result = []
        for row in rows:
            pol = dict(row)
            pol_id = pol.pop("id")
            ev_rows = conn.execute(
                """SELECT e.title, e.date, pe.role, pe.stance
                   FROM politician_events pe
                   JOIN events e ON pe.event_id = e.id
                   WHERE pe.politician_id = ?
                   ORDER BY e.date DESC
                   LIMIT 3""",
                (pol_id,),
            ).fetchall()
            pol["recent_events"] = [dict(r) for r in ev_rows]
            result.append(pol)
    return result


def _make_politician_segment(politicians: List[Dict],
                              is_final: bool = False) -> Dict:
    """Format a list of DB-enriched politician dicts as a podcast segment."""
    events = []
    for pol in politicians:
        recent = pol.get("recent_events", [])
        activity = "; ".join(
            f"[{ev.get('role','mentioned').replace('_',' ').title()}] "
            f"{ev.get('date','')}: {ev.get('title','')[:70]}"
            for ev in recent[:2]
        ) or "No recent tracked activity"
        events.append({
            "title": f"{pol['name']} — {pol.get('office', '')}",
            "summary": (
                f"{pol['name']} ({pol.get('party','?')}) has appeared "
                f"{pol.get('mention_count', 0)} time(s) in recent news. "
                f"Recent: {activity}"
            ),
            "source_name": "Politician Tracker",
            "politicians": pol["name"],
            "categories": ["election"],
            "relevance_score": 0.85,
        })
    label = "Candidate Tracker & Wrap-Up" if is_final else "Politician Spotlight"
    focus = (
        "where your candidates and elected officials stand — their records, "
        "recent votes, and what that means for you"
        if is_final else
        "what your local representatives have been doing recently"
    )
    return {
        "key": "politicians",
        "label": label,
        "events": events,
        "intro_focus": focus,
        "is_politician_segment": True,
    }


def _infer_episode_title(ep_num: int, segments: List[Dict]) -> str:
    """Infer an episode title from the segments it contains."""
    parts = [_LEVEL_SHORT.get(s["key"], s["key"].title())
             for s in segments if s.get("events")]
    if not parts:
        return f"Episode {ep_num}"
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return " & ".join(parts)
    return f"{parts[0]} + More"


def _load_avoid_list(db_path: Path, target_date: date) -> List[str]:
    """
    Pull the most recent PM rollup whose window ended STRICTLY BEFORE today
    and return its avoid_list. Empty list if no usable rollup.
    """
    try:
        from scanner.database import list_weekly_themes
    except Exception as e:
        log.debug("PM rollup unavailable for avoid_list: %s", e)
        return []
    try:
        rollups = list_weekly_themes(db_path, limit=12)
    except Exception as e:
        log.warning("Could not load weekly_themes for avoid_list: %s", e)
        return []
    today_iso = target_date.isoformat()
    rollup = next(
        (r for r in rollups if (r.get("week_end") or "") < today_iso),
        None,
    )
    if not rollup:
        return []
    items = rollup.get("avoid_list") or []
    return [str(x).strip() for x in items if str(x).strip()]


def _load_ballot_block(db_path: Path, target_date: date) -> str:
    """
    Render the listener's known ballot candidates as a prompt block the
    Author can use to frame races as "Candidate A vs Candidate B". Empty
    string if nothing has been discovered yet for the current ballot year.
    """
    try:
        from scanner.ballot import build_ballot_block
    except Exception as e:
        log.debug("Ballot block unavailable: %s", e)
        return ""
    try:
        return build_ballot_block(db_path, ballot_year=target_date.year)
    except Exception as e:
        log.warning("Could not build ballot block: %s", e)
        return ""


def _ctx(ballot_block: str, avoid_block: str) -> str:
    """Join the ballot + avoid blocks into a single suffix for user prompts."""
    parts: List[str] = []
    if ballot_block:
        parts.append("\n\n" + ballot_block + "\n")
    if avoid_block:
        parts.append(avoid_block)
    return "".join(parts)


def _format_avoid_block(items: List[str], strict: bool = False) -> str:
    """Render an avoid-list into a compact append-to-user-prompt block."""
    if not items:
        return ""
    header = (
        "HARD CONSTRAINT — the Editor has ordered a rewrite. You must NOT "
        "reuse any of the following framings, taglines, anecdotes, or "
        "phrasings that the show has already used recently. Find fresh angles."
        if strict else
        "AVOID — do NOT reuse these framings, taglines, anecdotes, or "
        "phrasings; the show has already used them in the last few days:"
    )
    body = "\n".join(f"  - {it}" for it in items[:20])
    return f"\n\n{header}\n{body}\n"


def _parse_avoid_from_reason(reason: str) -> List[str]:
    """
    The Editor's rewrite_reason is free-form but typically ends with a
    semicolon-separated list of things to avoid. Peel those off so we
    can feed them explicitly to the next draft.
    """
    if not reason:
        return []
    # Take everything after the first colon or semicolon if one exists,
    # else the whole string.
    tail = reason
    for sep in (":", ";"):
        if sep in reason:
            tail = reason.split(sep, 1)[1]
            break
    items = [p.strip(" .\t") for p in re.split(r"[;,]", tail) if p.strip()]
    # also include the full sentence as a safety net
    return [reason.strip()] + items


def _build_full_script(claude: anthropic.Anthropic, model: str,
                        target_date: date, ep_num: int, ep_title: str,
                        segments: List[Dict], avoid_block: str,
                        ballot_block: str = "") -> str:
    """Intro + per-segment dialogue + outro, concatenated."""
    parts: List[str] = []
    parts.append(_write_episode_intro(
        claude, model, target_date, ep_num, ep_title, segments,
        avoid_block, ballot_block,
    ))
    for seg in segments:
        if not seg["events"]:
            continue
        log.info("  writing segment: %s (%d items)…",
                 seg["label"], len(seg["events"]))
        parts.append(_write_segment(claude, model, seg, avoid_block, ballot_block))
    parts.append(_write_episode_outro(
        claude, model, target_date, ep_num, ep_title, segments,
        avoid_block, ballot_block,
    ))
    return "\n\n".join(parts)


def _write_episode_intro(client: anthropic.Anthropic, model: str,
                          target_date: date, ep_num: int,
                          ep_title: str, segments: List[Dict],
                          avoid_block: str = "",
                          ballot_block: str = "") -> str:
    date_str = target_date.strftime("%A, %B %d, %Y")
    preview = "\n".join(
        f"  - {seg['label']}: {len(seg['events'])} stories "
        f"(top: {seg['events'][0].get('title','')[:70]})"
        for seg in segments if seg["events"]
    )
    user = (
        f"Write the OPENING for Episode {ep_num}: '{ep_title}' "
        f"of {_SHOW_NAME!r} ({date_str}).\n\n"
        f"Stories today:\n{preview}\n\n"
        "Opening should: (1) name the episode, (2) quick preview of the 2 biggest stories, "
        "(3) set expectations for what's covered. Target: ~350 words."
    )
    return _claude_dialogue(client, model, user + _ctx(ballot_block, avoid_block))


def _write_episode_outro(client: anthropic.Anthropic, model: str,
                          target_date: date, ep_num: int,
                          ep_title: str, segments: List[Dict],
                          avoid_block: str = "",
                          ballot_block: str = "") -> str:
    total = sum(len(s["events"]) for s in segments)
    user = (
        f"Write a closing for Episode {ep_num}: '{ep_title}' of {_SHOW_NAME!r}.\n\n"
        f"Covered {total} stories across {len(segments)} segments.\n\n"
        "Closing should: (1) 2-3 things worth remembering, "
        "(2) one concrete action the listener can take this week, "
        "(3) warm sign-off. Target: ~250 words."
    )
    return _claude_dialogue(client, model, user + _ctx(ballot_block, avoid_block))


def _group_by_segment(events: List[Dict], top_n: int) -> List[Dict]:
    """Bucket events into podcast segments, keeping top-N by relevance per bucket."""
    buckets = [
        {"key": "federal", "label": f"Federal — {_FED_FOCUS}",
         "events": [], "intro_focus": f"federal news on {_FED_FOCUS}"},
        {"key": "state",   "label": f"{_STATE} State Legislature",
         "events": [], "intro_focus": f"what's happening in the {_STATE} legislature"},
        {"key": "county",  "label": f"{_COUNTY} Council",
         "events": [], "intro_focus": f"county-level actions that shape daily life in {_LOCALE}"},
        {"key": "school",  "label": "School Board",
         "events": [], "intro_focus": f"education in {_COUNTY}"},
        {"key": "local",   "label": "Local Services — Police, Fire, Health",
         "events": [], "intro_focus": "police, fire/rescue, and public health in the county"},
    ]
    by_key = {b["key"]: b for b in buckets}

    # Sort all events globally by relevance, then distribute
    events_sorted = sorted(events, key=lambda e: -e.get("relevance_score", 0))
    picked = 0
    for ev in events_sorted:
        if picked >= top_n:
            break
        lvl = ev.get("level", "county")
        if lvl in by_key:
            # Cap each bucket to avoid one topic dominating
            if len(by_key[lvl]["events"]) < 6:
                by_key[lvl]["events"].append(ev)
                picked += 1

    # Drop empty buckets
    return [b for b in buckets if b["events"]]


def _write_intro(client: anthropic.Anthropic, model: str,
                 target_date: date, segments: List[Dict]) -> str:
    date_str = target_date.strftime("%A, %B %d, %Y")
    stories_preview = "\n".join(
        f"  - {seg['label']}: {len(seg['events'])} stories  (top: "
        f"{seg['events'][0].get('title','')[:80]}...)"
        for seg in segments if seg["events"]
    )
    user = (
        f"Write a 2-3 minute podcast OPENING for today's episode ({date_str}).\n\n"
        f"Today's stories by segment:\n{stories_preview}\n\n"
        "The opening should:\n"
        "1. Warm greeting — name the show {_SHOW_NAME!r}\n"
        "2. Quick preview of the 2-3 biggest stories (pick the juiciest from the list)\n"
        "3. Set expectations: 'We'll walk through federal, state, county, schools, and local services'\n"
        "4. Remind that everything connects back to 'what you can actually do about it'\n"
        "Target: ~400 words of dialogue."
    )
    return _claude_dialogue(client, model, user)


def _write_outro(client: anthropic.Anthropic, model: str,
                 target_date: date, segments: List[Dict]) -> str:
    total_stories = sum(len(s["events"]) for s in segments)
    user = (
        f"Write a 2-minute podcast CLOSING.\n\n"
        f"We covered {total_stories} stories today across {len(segments)} segments.\n\n"
        "The closing should:\n"
        "1. Quick recap — 'the 3 things worth remembering from today'\n"
        "2. One concrete action the listener can take this week "
        "(attend a hearing, email a council member, register to vote, etc.)\n"
        "3. Warm sign-off, tease tomorrow\n"
        "Target: ~300 words of dialogue."
    )
    return _claude_dialogue(client, model, user)


def _write_segment(client: anthropic.Anthropic, model: str, segment: Dict,
                    avoid_block: str = "",
                    ballot_block: str = "") -> str:
    """Write dialogue for one segment — news stories or politician spotlights."""
    is_pol = segment.get("is_politician_segment", False)

    items_text = ""
    for i, ev in enumerate(segment["events"], 1):
        title   = ev.get("title", "")
        summary = ev.get("summary") or ev.get("description", "")
        source  = ev.get("source_name", "")
        pols    = ev.get("politicians", "")
        cats    = ev.get("categories", "")
        if is_pol:
            items_text += (
                f"\n[Person {i}] {title}\n"
                f"  Background: {summary}\n"
            )
        else:
            items_text += (
                f"\n[Story {i}] {title}\n"
                f"  Summary: {summary}\n"
                f"  Source: {source}\n"
                f"  Politicians: {pols or '(none identified)'}\n"
                f"  Tags: {cats}\n"
            )

    if is_pol:
        target_words = max(900, 450 * len(segment["events"]))
        user = (
            f"Write the '{segment['label']}' segment of today's podcast.\n\n"
            f"Focus on {segment['intro_focus']}.\n\n"
            f"Politicians to profile:\n{items_text}\n\n"
            "GUIDELINES:\n"
            "- Start with a smooth transition into this segment\n"
            "- For each person: who they are, what they've done recently, "
            "and what that means for residents\n"
            "- Jordan should ask things like 'so what does their track record "
            "tell us before the election?' or 'did they vote for or against X?'\n"
            f"- Total: ~{target_words} words of dialogue\n"
            "- Be factual and nonpartisan; present the record, not opinion"
        )
        return _claude_dialogue(client, model, user + _ctx(ballot_block, avoid_block))
    else:
        target_words = max(1500, 700 * len(segment["events"]))
        user = (
            f"Write the '{segment['label']}' segment of today's podcast.\n\n"
            f"This segment focuses on {segment['intro_focus']}.\n\n"
            f"Stories to cover (in order):\n{items_text}\n\n"
            "GUIDELINES:\n"
            "- Start with a transition line from the previous segment\n"
            "- For each story: Alex presents it, Jordan asks the 'so what' questions,\n"
            "  they explore it together, then move to the next\n"
            f"- Total length: ~{target_words} words of dialogue\n"
            f"- Emphasize the LOCAL ({_LOCALE}) angle even for state/federal stories\n"
            "- End with a brief transition to the next segment"
        )
    return _claude_dialogue(client, model, user + _ctx(ballot_block, avoid_block))


def _claude_dialogue(client: anthropic.Anthropic, model: str, user_prompt: str) -> str:
    """Call Claude to produce ALEX:/JORDAN: dialogue."""
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            system=[
                {"type": "text", "text": DIALOGUE_SYSTEM,
                 "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = resp.content[0].text.strip()
        return _clean_dialogue(text)
    except Exception as e:
        log.error("Claude dialogue generation failed: %s", e)
        return f"ALEX: Sorry, I couldn't generate this segment due to an error.\nJORDAN: We'll try again next episode.\n"


def _clean_dialogue(text: str) -> str:
    """Strip any accidental stage directions, markdown, or non-dialogue lines."""
    cleaned = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Skip lines not starting with ALEX: / JORDAN:
        if not re.match(r"^(ALEX|JORDAN)\s*:", line, re.IGNORECASE):
            continue
        # Normalize capitalization
        line = re.sub(r"^(alex|jordan)\s*:", lambda m: m.group(1).upper() + ":",
                       line, flags=re.IGNORECASE)
        # Strip markdown bold/italic and stage directions
        line = re.sub(r"[\*_]", "", line)
        line = re.sub(r"\[[^\]]+\]", "", line)
        cleaned.append(line)
    return "\n".join(cleaned)


# ──────────────────────────────────────────────────────────────────────────────
# TTS / audio synthesis
# ──────────────────────────────────────────────────────────────────────────────

# OpenAI TTS has a 4096-char input limit per call
_TTS_MAX_CHARS = 4000


def _synthesize_dialogue(client: openai.OpenAI, script: str,
                          out_path: Path, model: str = "tts-1") -> int:
    """
    Synthesize the full dialogue → single MP3 at `out_path`.
    Returns approximate duration in seconds.
    """
    chunks = _chunk_dialogue_for_tts(script)
    log.info("TTS: synthesizing %d chunks", len(chunks))

    total_bytes = 0
    # Open output file once; append each chunk's MP3 bytes.
    # OpenAI TTS returns valid MP3 frames that concatenate cleanly for playback.
    with open(out_path, "wb") as out_f:
        for i, (speaker, text) in enumerate(chunks, 1):
            voice = VOICES.get(speaker, "alloy")
            retry = 0
            while True:
                try:
                    resp = client.audio.speech.create(
                        model=model,
                        voice=voice,
                        input=text,
                        response_format="mp3",
                    )
                    audio_bytes = resp.content
                    out_f.write(audio_bytes)
                    total_bytes += len(audio_bytes)
                    if i % 10 == 0 or i == len(chunks):
                        log.info("  [%d/%d] %s chunks done (%.1f MB)",
                                 i, len(chunks), speaker, total_bytes / 1_048_576)
                    break
                except Exception as e:
                    retry += 1
                    if retry >= 3:
                        log.error("TTS failed on chunk %d after 3 retries: %s", i, e)
                        raise
                    log.warning("TTS chunk %d failed (%s), retrying…", i, e)
                    time.sleep(2 * retry)

    # Rough duration: MP3 at 32kbps ≈ 4 KB/sec (OpenAI's default)
    duration = total_bytes // 4000
    log.info("Audio: %.1f MB, ~%d:%02d",
             total_bytes / 1_048_576, duration // 60, duration % 60)
    return duration


def _chunk_dialogue_for_tts(script: str) -> List[Tuple[str, str]]:
    """
    Split dialogue into (speaker, text) chunks ≤ _TTS_MAX_CHARS, merging
    consecutive same-speaker lines for smoother flow.
    """
    raw: List[Tuple[str, str]] = []
    for line in script.split("\n"):
        line = line.strip()
        m = re.match(r"^(ALEX|JORDAN)\s*:\s*(.+)$", line, re.IGNORECASE)
        if not m:
            continue
        speaker = m.group(1).upper()
        content = m.group(2).strip()
        if not content:
            continue
        raw.append((speaker, content))

    # Merge consecutive same-speaker lines, then split if > max
    merged: List[Tuple[str, str]] = []
    for speaker, content in raw:
        if merged and merged[-1][0] == speaker \
           and len(merged[-1][1]) + len(content) + 1 < _TTS_MAX_CHARS:
            merged[-1] = (speaker, merged[-1][1] + " " + content)
        else:
            merged.append((speaker, content))

    # Hard-split any chunk that somehow exceeds limit (on sentence boundaries)
    final: List[Tuple[str, str]] = []
    for speaker, content in merged:
        if len(content) <= _TTS_MAX_CHARS:
            final.append((speaker, content))
            continue
        # Split on sentence endings
        sentences = re.split(r"(?<=[.!?])\s+", content)
        buf = ""
        for s in sentences:
            if len(buf) + len(s) + 1 > _TTS_MAX_CHARS:
                if buf:
                    final.append((speaker, buf.strip()))
                buf = s
            else:
                buf += (" " + s) if buf else s
        if buf:
            final.append((speaker, buf.strip()))
    return final
