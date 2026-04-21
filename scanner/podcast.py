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

# ── Speaker/voice mapping ─────────────────────────────────────────────────────
ALEX = "ALEX"
JORDAN = "JORDAN"

# ── System prompt for dialogue generation (cached) ────────────────────────────
DIALOGUE_SYSTEM = f"""You are writing a natural two-host podcast conversation about
local politics for a first-time voter in {_LOCALE}.

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

Output ONLY the dialogue lines in the ALEX/JORDAN format above. No preamble."""


# ── Voice per speaker (set from Config at runtime) ────────────────────────────
VOICES = {"ALEX": "onyx", "JORDAN": "nova"}


# ── Episode definitions ───────────────────────────────────────────────────────
EPISODE_CONFIGS = [
    {
        "num": 1,
        "title": "Federal",
        "slug": "federal",
        "levels": ["federal"],
        "focus": f"federal policy on {_FED_FOCUS}",
        "top_n": 8,
    },
    {
        "num": 2,
        "title": f"{_STATE} State Legislature",
        "slug": "state",
        "levels": ["state"],
        "focus": f"what's happening in the {_STATE} legislature",
        "top_n": 8,
    },
    {
        "num": 3,
        "title": f"{_COUNTY} and Schools",
        "slug": "county",
        "levels": ["county", "school", "local"],
        "focus": f"{_COUNTY} council, school board, and local services",
        "top_n": 10,
    },
    {
        "num": 4,
        "title": "Week in Review",
        "slug": "review",
        "levels": ["federal", "state", "county", "school", "local"],
        "focus": "the week's most impactful stories across all levels of government",
        "top_n": 6,
    },
]


def generate_podcast_episodes(db_path: Path, podcasts_dir: Path,
                               anthropic_key: str, openai_key: str,
                               target_date: Optional[date] = None,
                               script_model: str = "claude-sonnet-4-5-20250929",
                               tts_model: str = "tts-1",
                               alex_voice: str = "onyx",
                               jordan_voice: str = "nova",
                               no_audio: bool = False,
                               filter_incidents: bool = True) -> List[Dict]:
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

    results: List[Dict] = []

    for ep_cfg in EPISODE_CONFIGS:
        ep_num = ep_cfg["num"]
        ep_slug = ep_cfg["slug"]
        ep_title = ep_cfg["title"]
        log.info("=== Episode %d: %s ===", ep_num, ep_title)

        # Pick events for this episode
        ep_events = [ev for ev in all_events if ev.get("level") in ep_cfg["levels"]]
        # For the review episode: already have all levels, just dedupe from prev episodes
        if ep_cfg.get("slug") == "review":
            used_urls = {ev.get("source_url") for ep in results for ev in ep.get("_events", [])}
            ep_events = [ev for ev in ep_events if ev.get("source_url") not in used_urls]
        ep_events = sorted(ep_events, key=lambda e: -e.get("relevance_score", 0))
        ep_events = ep_events[: ep_cfg["top_n"]]
        log.info("  %d stories selected for this episode", len(ep_events))

        # Build segment list (one segment per level present)
        segments = _build_episode_segments(ep_events, ep_cfg)

        # ── Write script ──────────────────────────────────────────────────────
        script_parts = []
        script_parts.append(_write_episode_intro(claude, script_model,
                                                  target_date, ep_num, ep_title, segments))

        for seg in segments:
            if not seg["events"]:
                continue
            log.info("  writing segment: %s (%d stories)…",
                     seg["label"], len(seg["events"]))
            script_parts.append(_write_segment(claude, script_model, seg))

        script_parts.append(_write_episode_outro(claude, script_model,
                                                  target_date, ep_num, ep_title, segments))

        full_script = "\n\n".join(script_parts)
        word_count = len(full_script.split())
        log.info("  Script: %d words (~%d min)", word_count, word_count // 130)

        script_fname = f"podcast_{date_str}_ep{ep_num}_{ep_slug}.txt"
        script_path = podcasts_dir / script_fname
        script_path.write_text(full_script, encoding="utf-8")

        if no_audio:
            results.append({
                "episode_num": ep_num,
                "episode_title": ep_title,
                "episode_slug": ep_slug,
                "script": full_script,
                "script_path": str(script_path),
                "audio_path": "",
                "duration_seconds": 0,
                "word_count": word_count,
                "status": "script_only",
                "_events": ep_events,
            })
            continue

        audio_fname = f"podcast_{date_str}_ep{ep_num}_{ep_slug}.mp3"
        audio_path = podcasts_dir / audio_fname
        duration = _synthesize_dialogue(openai_client, full_script, audio_path, tts_model)

        results.append({
            "episode_num": ep_num,
            "episode_title": ep_title,
            "episode_slug": ep_slug,
            "script": full_script,
            "script_path": str(script_path),
            "audio_path": str(audio_path),
            "duration_seconds": duration,
            "word_count": word_count,
            "status": "done",
            "_events": ep_events,
        })

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

def _build_episode_segments(events: List[Dict], ep_cfg: Dict) -> List[Dict]:
    """Group episode events into per-level sub-segments."""
    level_labels = {
        "federal": f"Federal — {_FED_FOCUS}",
        "state":   f"{_STATE} State Legislature",
        "county":  f"{_COUNTY} Council",
        "school":  "School Board",
        "local":   "Local Services — Police, Fire, Health",
    }
    # One bucket per level in this episode's scope
    buckets: Dict[str, Dict] = {}
    for lvl in ep_cfg["levels"]:
        buckets[lvl] = {
            "key": lvl,
            "label": level_labels.get(lvl, lvl.title()),
            "events": [],
            "intro_focus": level_labels.get(lvl, lvl),
        }

    for ev in events:
        lvl = ev.get("level", "county")
        if lvl in buckets:
            buckets[lvl]["events"].append(ev)

    return [b for b in buckets.values() if b["events"]]


def _write_episode_intro(client: anthropic.Anthropic, model: str,
                          target_date: date, ep_num: int,
                          ep_title: str, segments: List[Dict]) -> str:
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
    return _claude_dialogue(client, model, user)


def _write_episode_outro(client: anthropic.Anthropic, model: str,
                          target_date: date, ep_num: int,
                          ep_title: str, segments: List[Dict]) -> str:
    total = sum(len(s["events"]) for s in segments)
    user = (
        f"Write a closing for Episode {ep_num}: '{ep_title}' of {_SHOW_NAME!r}.\n\n"
        f"Covered {total} stories across {len(segments)} segments.\n\n"
        "Closing should: (1) 2-3 things worth remembering, "
        "(2) one concrete action the listener can take this week, "
        "(3) warm sign-off. Target: ~250 words."
    )
    return _claude_dialogue(client, model, user)


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


def _write_segment(client: anthropic.Anthropic, model: str, segment: Dict) -> str:
    """Write ~20 minutes of dialogue for a segment."""
    events_text = ""
    for i, ev in enumerate(segment["events"], 1):
        title = ev.get("title", "")
        summary = ev.get("summary") or ev.get("description", "")
        source = ev.get("source_name", "")
        pols = ev.get("politicians", "")
        categories = ev.get("categories", "")
        events_text += (
            f"\n[Story {i}] {title}\n"
            f"  Summary: {summary}\n"
            f"  Source: {source}\n"
            f"  Politicians: {pols or '(none identified)'}\n"
            f"  Tags: {categories}\n"
        )

    target_words = max(1500, 800 * len(segment["events"]))  # ~6min per story
    user = (
        f"Write the '{segment['label']}' segment of today's podcast.\n\n"
        f"This segment focuses on {segment['intro_focus']}.\n\n"
        f"Here are the stories to cover (in order):\n{events_text}\n\n"
        f"GUIDELINES:\n"
        "- Start with a transition line from the previous segment\n"
        "- For each story: Alex presents it, Jordan asks the 'so what' questions,\n"
        "  they explore it together, then move to the next\n"
        f"- Total length: ~{target_words} words of dialogue\n"
        f"- Emphasize the LOCAL ({_LOCALE}) angle even for state/federal\n"
        "- End with a brief transition to the next segment"
    )
    return _claude_dialogue(client, model, user)


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
