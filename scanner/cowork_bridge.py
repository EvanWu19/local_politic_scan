"""
Cowork bridge — hand off Opus-grade work to the Cowork agent (Claude Opus 4.7
running in the user's Cowork desktop app) instead of paying for a separate
Anthropic API call.

Mechanics
---------
The Python pipeline writes a JSON *brief* into ``cowork_inbox/``. A daily
Cowork scheduled task drains that folder, performs the work (with web search
and the latest Opus model), and writes the result file into ``cowork_outbox/``
plus the canonical destination the brief specifies (e.g. a podcast script
under ``podcasts/`` or a dossier under ``data/candidate_dossiers/``).

Brief schema
------------
::

    {
      "brief_id":    "deepdive_2026-04-28_sidney-katz",   # filename stem
      "type":        "candidate_dossier" | "deep_dive_script" | "editor_rewrite",
      "created_at":  "2026-04-27T22:14:00-04:00",
      "due_by":      "2026-04-28T07:00:00-04:00",         # informational only
      "output_file": "<absolute path the Cowork agent must write>",
      "instructions": "<free-text ask, written by the Python agent>",
      "context":      { ...arbitrary structured context... }
    }

The Cowork task processes ``*.json`` in ``cowork_inbox/`` oldest first, writes
the deliverable to ``output_file``, mirrors a copy into ``cowork_outbox/`` for
audit, and renames the brief to ``<id>.done.json`` (or ``<id>.error.json``).

Why a folder bridge instead of an HTTP/MCP call
-----------------------------------------------
* Zero new dependencies; the brief survives across machine restarts.
* The Windows pipeline never blocks on Cowork — if Cowork hasn't run yet, the
  Python side falls back to whatever it had (today's Sonnet draft, the prior
  dossier, etc.) and tries again tomorrow.
* The ``.done.json`` rename gives us a free idempotency check and audit log.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# Resolve relative to the project root regardless of where Python was invoked.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
INBOX_DIR = _PROJECT_ROOT / "cowork_inbox"
OUTBOX_DIR = _PROJECT_ROOT / "cowork_outbox"


# ──────────────────────────────────────────────────────────────────────────────
# Brief construction
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Brief:
    """One unit of work for the Cowork agent."""

    brief_id: str
    type: str
    output_file: str
    instructions: str
    context: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    due_by: str = ""

    def to_dict(self) -> Dict[str, Any]:
        if not self.created_at:
            self.created_at = datetime.now().astimezone().isoformat()
        return asdict(self)


def _slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    text = re.sub(r"[-\s]+", "-", text)
    return text or "x"


def write_brief(brief: Brief, *, replace: bool = True) -> Path:
    """
    Drop a brief in the inbox. Returns the path that was written.

    With ``replace=True`` (default) a brief with the same id is overwritten so
    that a re-run of the same morning's pipeline doesn't pile up duplicates.
    The Cowork side will pick the freshest version because it processes oldest
    first by mtime — overwriting bumps the mtime forward.
    """
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)

    target = INBOX_DIR / f"{brief.brief_id}.json"
    done_marker = INBOX_DIR / f"{brief.brief_id}.done.json"

    if done_marker.exists() and not replace:
        log.info("cowork_bridge: brief %s already completed — skipping", brief.brief_id)
        return done_marker

    payload = brief.to_dict()
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("cowork_bridge: wrote brief %s (%s)", brief.brief_id, brief.type)
    return target


# ──────────────────────────────────────────────────────────────────────────────
# Brief consumption (used by the Cowork-side processor and by the Python
# fallback that copies any newly-completed outputs into place if needed)
# ──────────────────────────────────────────────────────────────────────────────

def list_pending_briefs() -> List[Path]:
    """Return inbox briefs that haven't been completed yet, oldest first."""
    if not INBOX_DIR.exists():
        return []
    pending: List[Path] = []
    for p in INBOX_DIR.glob("*.json"):
        if p.name.endswith(".done.json") or p.name.endswith(".error.json"):
            continue
        pending.append(p)
    pending.sort(key=lambda p: p.stat().st_mtime)
    return pending


def load_brief(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def mark_done(brief_path: Path, output_summary: str = "") -> None:
    """Atomically rename the brief to <id>.done.json.

    Side effect: if the brief was a `candidate_dossier`, eagerly import the
    finished .md file's citations into the `candidate_sources` table so the
    research record accumulates permanently. The import is non-fatal — any
    failure logs and is swallowed so the bridge's drain loop keeps moving.
    """
    done = brief_path.with_name(brief_path.stem + ".done.json")
    data = load_brief(brief_path)
    data["completed_at"] = datetime.now().astimezone().isoformat()
    if output_summary:
        data["output_summary"] = output_summary
    done.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    brief_path.unlink(missing_ok=True)

    # Auto-import dossier sources on completion.
    if data.get("type") == "candidate_dossier":
        try:
            from scanner.dossier_importer import import_dossier_sources
            from config import Config as _Cfg
            out_path = Path(data.get("output_file", ""))
            cand_name = (data.get("payload", {}) or {}).get("candidate_name") \
                        or data.get("candidate_name") or ""
            # dossier_date is encoded in the brief id (dossier_YYYY-MM-DD_slug)
            m = re.fullmatch(r"dossier_(\d{4}-\d{2}-\d{2})_.+",
                             data.get("brief_id", ""))
            dossier_date = m.group(1) if m else None
            if out_path.exists() and cand_name:
                n = import_dossier_sources(_Cfg.DB_PATH, out_path,
                                            cand_name, dossier_date)
                log.info("mark_done: imported %d source(s) for %s", n, cand_name)
        except Exception as e:
            log.warning("mark_done: dossier source import failed (non-fatal): %s", e)


def mark_error(brief_path: Path, error: str) -> None:
    err = brief_path.with_name(brief_path.stem + ".error.json")
    data = load_brief(brief_path)
    data["error_at"] = datetime.now().astimezone().isoformat()
    data["error"] = error
    err.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    brief_path.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# Convenience helpers — typed brief builders the agents call into
# ──────────────────────────────────────────────────────────────────────────────

def build_dossier_brief(
    *,
    candidate_name: str,
    office: str,
    party: str,
    district: str,
    known_events: List[Dict[str, Any]],
    listener_focus: List[str],
    output_dir: Path,
    today: str,
    finance_block: str = "",
) -> Brief:
    slug = _slugify(candidate_name)
    output_file = output_dir / f"{slug}.md"
    instructions = (
        f"Build a DEEP candidate dossier for {candidate_name} ({office}, {party}, "
        f"district {district or 'unknown'}). The current draft of these "
        "dossiers has been too thin to support a single-candidate deep-dive "
        "episode — it covers a few recent votes and stops. The listener has "
        "asked for the *life* of the candidate, not just a roll-call list.\n\n"

        "Step 1 — RESOLVE THE PERSON.\n"
        f"  • Web-search to disambiguate. The name '{candidate_name}' may be "
        "    common; pin down the specific person running for this office in "
        "    this district. Cross-check with the campaign filing, the office "
        "    they're running for, the district, and party.\n"
        "  • Determine year of birth (or best estimate). This sets the "
        "    look-back window for step 3.\n\n"

        "Step 2 — BIOGRAPHICAL FOUNDATION.\n"
        "Build these sections from web research, citing every fact:\n"
        "  A. DEMOGRAPHICS — birth year & age, place of birth, current "
        "     residence, family (spouse, children if publicly disclosed), "
        "     race/ethnicity if publicly self-identified, religion if "
        "     publicly self-identified, languages spoken.\n"
        "  B. SOCIOECONOMIC — net worth & income (from public financial "
        "     disclosures, FEC filings, state ethics filings), home "
        "     ownership status if public, major business interests, "
        "     stock holdings, any disclosed conflicts of interest.\n"
        "  C. EDUCATION — every school attended (high school, college, "
        "     graduate), degrees earned with years, notable academic "
        "     awards or unfinished programs, any honorary degrees.\n"
        "  D. PROFESSIONAL HISTORY — every job held, in reverse chronology, "
        "     with start/end years and employer. For each role, one line on "
        "     what they actually did. Include military service, nonprofit "
        "     work, volunteer leadership, board memberships. Fill gaps "
        "     where possible — a five-year unaccounted-for stretch is itself "
        "     a finding worth noting.\n\n"

        "Step 3 — DEEP NEWS HISTORY.\n"
        "Once you know their age, search news, social media, archived web "
        "pages, court records, professional licensure boards, podcast "
        "interviews, op-eds, and academic publications across a window of "
        "**(candidate_age − 20) years, minimum 10 years**. So a 40-year-old "
        "→ 20 years back; a 30-year-old → 10 years back; a 28-year-old → "
        "10 years back (minimum); a 65-year-old → 45 years back; an "
        "80-year-old → 60 years back. The window deliberately skips "
        "childhood and early teens (nothing useful below age 20). Don't "
        "just sample recent press releases. Use these starting points:\n"
        "  • Google News with date-range filter (set after: tbs=cdr)\n"
        "  • news.google.com archive\n"
        "  • web.archive.org for the candidate's own site / employer pages\n"
        "  • Their LinkedIn, X/Twitter back to earliest posts\n"
        "  • Local newspapers' archives (Bethesda Magazine, Maryland Matters, "
        "    Washington Post, WTOP) — many are free or cached\n"
        "  • Court records (PACER for federal, Maryland Judiciary Case "
        "    Search for state)\n"
        "  • Charity filings (ProPublica Nonprofit Explorer)\n"
        "  • Academic citations (Google Scholar)\n"
        "  • For Maryland legislators: mgaleg.maryland.gov full bill history\n"
        "  • For Montgomery County officials: county council legislative DB\n"
        "  • For federal: congress.gov + GovTrack legacy data\n"
        "Surface anything substantive you find — early career controversies, "
        "switch in party affiliation, prior runs for office (won AND lost), "
        "endorsements they collected and gave, organizations they joined or "
        "founded, public statements that have aged interestingly.\n\n"

        "Step 4 — POLITICAL TRACK RECORD (if they've held office).\n"
        "  • Voting record by topic — table with bill numbers, dates, "
        "    direction (yes/no/abstain), brief description.\n"
        "  • Public statements & policy positions — quoted, with links and "
        "    dates. Show evolution where positions shifted.\n"
        "  • Sponsored / co-sponsored legislation — with status (passed / "
        "    failed / committee / introduced).\n"
        "  • Endorsements received and given — list with dates.\n\n"

        "Step 5 — CONTROVERSIES & NOTABLE PRESS.\n"
        "Neutral framing. If something is alleged, say so; if proven, "
        "say so; if dismissed, say so. Include ethics complaints, lawsuits, "
        "FEC complaints, news investigations.\n\n"

        "Step 6 — STANCE ON LISTENER-FLAGGED TOPICS:\n  "
        + ", ".join(listener_focus or [
            "sanctuary policy", "school funding", "property tax",
            "K-12 curriculum", "policing"])
        + "\nFor each topic give: (a) public statements with quotes/links, "
        "(b) any votes that bear on it, (c) clear bottom-line assessment "
        "(supports / opposes / mixed / unclear).\n\n"

        "Step 7 — OPEN QUESTIONS. What couldn't you find? Name the gaps "
        "explicitly so the next deep-dive episode can flag them as \"we "
        "don't know yet.\"\n\n"

        "OUTPUT.\n"
        "  • Format: Markdown to `output_file`.\n"
        f"  • Length target: 1800–3500 words (was 800–1500 before this "
        "    upgrade — the prior length wasn't enough to support 30 "
        "    minutes of dialogue).\n"
        "  • Cite every non-obvious claim with a URL. Inline footnotes or "
        "    bracketed [src: URL] are fine.\n"
        "  • If a fact CAN'T be sourced after honest searching, say so. "
        "    NEVER fabricate dates, employers, schools, family details, "
        "    quotes, vote counts, or bill numbers."
    )
    if finance_block:
        instructions += (
            "\n\nKNOWN CAMPAIGN-FINANCE DATA (from public filings — use as a "
            "starting point for Step 2B, and verify/extend with your own "
            "research):" + finance_block
        )
    return Brief(
        brief_id=f"dossier_{today}_{slug}",
        type="candidate_dossier",
        output_file=str(output_file),
        instructions=instructions,
        context={
            "candidate_name": candidate_name,
            "office": office,
            "party": party,
            "district": district,
            "listener_focus": listener_focus,
            "known_events": known_events[:30],
            "finance_block": finance_block,
        },
    )


def build_deep_dive_brief(
    *,
    candidate_name: str,
    target_date: str,
    politician_row: Dict[str, Any],
    events: List[Dict[str, Any]],
    consistency: Optional[Dict[str, Any]],
    listener_notes: List[Dict[str, Any]],
    avoid_list: List[str],
    dossier_path: Optional[Path],
    output_file: Path,
) -> Brief:
    slug = _slugify(candidate_name)
    instructions = (
        f"Write a ~30-minute single-candidate deep-dive podcast on "
        f"{candidate_name}. Two hosts: ALEX (presenter) and JORDAN (curious "
        "first-time voter from Rockville, MD). Every line begins ALEX: or "
        "JORDAN: — no stage directions, no markdown.\n\n"
        "Cover, in this order:\n"
        "  1. Who they are and which seat the listener is voting on.\n"
        "  2. Their actual voting record by topic — quote bill numbers and "
        "cite the dossier. Do NOT speak in generalities; if the dossier has a "
        "vote, name it.\n"
        "  3. Where their stated positions and votes have been stable, where "
        "they've shifted, and which votes the listener-flagged topics turn "
        "on (sanctuary policy, school funding, property tax, etc.).\n"
        "  4. What's contested in the upcoming primary and how this candidate "
        "differs from the others.\n"
        "  5. A 'what would change in Rockville if they win' grounded close.\n\n"
        "AVOID re-using any framing on this list:\n  - "
        + "\n  - ".join(avoid_list or ["(none specified)"])
        + "\n\n"
        "If the dossier is missing a fact you'd want to cite, say so on-air "
        "('we don't have a record of how they voted on X yet'), don't invent."
    )
    return Brief(
        brief_id=f"deepdive_{target_date}_{slug}",
        type="deep_dive_script",
        output_file=str(output_file),
        instructions=instructions,
        context={
            "candidate_name": candidate_name,
            "target_date": target_date,
            "politician_row": politician_row,
            "events": events[:40],
            "consistency": consistency,
            "listener_notes": listener_notes[-10:],
            "avoid_list": avoid_list,
            "dossier_path": str(dossier_path) if dossier_path else None,
        },
    )


def build_rewrite_brief(
    *,
    target_date: str,
    ep_num: int,
    ep_title: str,
    failed_draft: str,
    rewrite_reason: str,
    avoid_list: List[str],
    listener_notes: List[Dict[str, Any]],
    output_file: Path,
) -> Brief:
    instructions = (
        f"The Editor agent ordered a structural rewrite of episode {ep_num} "
        f"('{ep_title}', {target_date}). The original draft has been written "
        "twice already by the same Sonnet model and the Editor still flags it "
        "as repetitive. You are the escalation. Produce a fresh script.\n\n"
        f"REWRITE REASON FROM EDITOR:\n{rewrite_reason}\n\n"
        "Hard rules:\n"
        "  • Two hosts ALEX (presenter) and JORDAN (curious co-host). Every "
        "    line starts ALEX: or JORDAN: — no stage directions, no markdown.\n"
        "  • Do not re-use any framing on the avoid list.\n"
        "  • The listener's recent notes (below) are direct asks — answer at "
        "    least one of them in this episode by name.\n"
        "  • Same target length as the original draft (~3500–4500 words).\n"
        "  • Cite specific votes, statements, and dates — generalities are "
        "    why the prior drafts failed."
    )
    return Brief(
        brief_id=f"rewrite_{target_date}_ep{ep_num}",
        type="editor_rewrite",
        output_file=str(output_file),
        instructions=instructions,
        context={
            "target_date": target_date,
            "ep_num": ep_num,
            "ep_title": ep_title,
            "failed_draft": failed_draft,
            "rewrite_reason": rewrite_reason,
            "avoid_list": avoid_list,
            "listener_notes": listener_notes[-10:],
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# Brief builders for the rest of the AI surface — every Anthropic API call
# site in the project routes through one of these when USE_COWORK_FOR_AI=True.
# ──────────────────────────────────────────────────────────────────────────────

def build_enrich_events_brief(
    *,
    target_date: str,
    db_path: Path,
    event_ids: List[int],
    locale: str,
    federal_keywords: List[str],
    districts_profile: str,
    output_file: Optional[Path] = None,
) -> Brief:
    """One brief covering all events that need AI enrichment for this run."""
    instructions = (
        "Enrich raw political events stored in the SQLite DB. For each event "
        "id in `context.event_ids`, read the row from the `events` table at "
        f"`{db_path}` and update it in-place with:\n"
        "  • summary — 2 plain-English sentences explaining what this means "
        "for an average resident. No jargon. Why does it matter?\n"
        "  • relevance_score — float 0.0..1.0 for THIS resident. The voting "
        "districts in `context.districts_profile` should boost related items.\n"
        "  • categories — JSON array drawn from [tax, education, visa, health, "
        "police, fire, housing, budget, election, environment, transportation, "
        "business, trade, other].\n"
        "  • Then upsert any politicians named in the event into the "
        "`politicians` table (if missing) and link them via `politician_events` "
        "with role + stance.\n\n"
        "After updating each event, set its `ai_enriched_at` column to now() "
        "(add the column if it doesn't exist). At end of run, write a JSON "
        "summary to `output_file` listing how many events were enriched / "
        "skipped / failed."
    )
    output_file = output_file or (
        OUTBOX_DIR / f"enrich_{target_date}.json"
    )
    return Brief(
        brief_id=f"enrich_{target_date}",
        type="enrich_events",
        output_file=str(output_file),
        instructions=instructions,
        context={
            "target_date": target_date,
            "db_path": str(db_path),
            "event_ids": event_ids,
            "locale": locale,
            "federal_keywords": federal_keywords[:20],
            "districts_profile": districts_profile,
        },
    )


def build_consistency_brief(
    *,
    target_date: str,
    db_path: Path,
    politician_ids: List[int],
    locale: str,
    output_file: Optional[Path] = None,
) -> Brief:
    """One brief covering all politicians who need a fresh consistency score."""
    instructions = (
        "Score political consistency for each politician id listed in "
        "`context.politician_ids`. For each one:\n"
        f"  1. Read their row from `politicians` at `{db_path}`.\n"
        "  2. Read all linked rows from `politician_events` joined to "
        "`events.summary`, `events.event_date`, and `events.categories`.\n"
        "  3. If they have fewer than 3 events, write a row with "
        "verdict='insufficient' and a one-line summary, score=0.0.\n"
        "  4. Otherwise: identify the small set of TOPICS they've acted on. "
        "For each topic decide STABLE (every event same direction) vs "
        "SHIFTED (clearly opposing stances over time). Be conservative — "
        "wording differences are NOT shifts.\n"
        "  5. Write one new row into `consistency_scores` per politician with: "
        "score (0..1), verdict (consistent|mixed|inconsistent|insufficient), "
        "summary (one paragraph), stable_positions (JSON list of "
        "{topic, position, evidence_event_ids}), shifts (JSON list of "
        "{topic, from, to, when, evidence_event_ids}), event_count, "
        "window_start (earliest event date), window_end (today).\n\n"
        "DO NOT update existing rows — the listener uses the row history "
        "to see how assessments evolve. Always insert a new row per run."
    )
    output_file = output_file or (
        OUTBOX_DIR / f"consistency_{target_date}.json"
    )
    return Brief(
        brief_id=f"consistency_{target_date}",
        type="score_consistency",
        output_file=str(output_file),
        instructions=instructions,
        context={
            "target_date": target_date,
            "db_path": str(db_path),
            "politician_ids": politician_ids,
            "locale": locale,
        },
    )


def build_themes_brief(
    *,
    target_date: str,
    db_path: Path,
    window_days: int,
    locale: str,
    output_file: Optional[Path] = None,
) -> Brief:
    """One brief asking Cowork to write the daily/weekly themes rollup row."""
    instructions = (
        "Roll up the listener's recent daily_notes into a structured signal "
        "block the Editor and Author agents will use to plan the next "
        "episode.\n\n"
        f"Steps:\n"
        f"  1. Read the last {window_days} rows from `daily_notes` at "
        f"`{db_path}`.\n"
        "  2. Read the last 5 days of `podcast_*_ep*.txt` from the "
        "`podcasts/` directory — first ~30 lines each — so you can detect "
        "framings, taglines, and anecdotes the show has reused.\n"
        "  3. Distill into five sections:\n"
        "       SUMMARY — 2-3 sentence paragraph on what this listener "
        "cares about right now.\n"
        "       THEMES — 0–5 lines, `<theme> | <why it matters>`.\n"
        "       OPEN QUESTIONS — 0–5 lines, near-verbatim.\n"
        "       UNDERSERVED TOPICS — 0–5 lines.\n"
        "       RECENT COVERAGE TO AVOID — 0–10 lines, concrete framings/"
        "taglines/anecdotes already used in the last 5 days. Be specific.\n"
        "  4. UPSERT a row into `weekly_themes` keyed by "
        "(window_start, window_end). Fields: themes, open_questions, "
        "underserved_topics, summary (each as JSON arrays of strings or as "
        "the multi-line text the project's existing parser accepts), "
        "avoid_list (JSON array), note_count, generated_at.\n"
        "  5. Also write the structured rollup to `output_file` for audit.\n\n"
        "Be conservative on themes (no inventing from one passing comment), "
        "AGGRESSIVE on the avoid list — repetition is the listener's #1 "
        "complaint."
    )
    output_file = output_file or (
        OUTBOX_DIR / f"themes_{target_date}.json"
    )
    return Brief(
        brief_id=f"themes_{target_date}",
        type="weekly_themes",
        output_file=str(output_file),
        instructions=instructions,
        context={
            "target_date": target_date,
            "db_path": str(db_path),
            "window_days": window_days,
            "locale": locale,
        },
    )


def build_author_episode_brief(
    *,
    target_date: str,
    ep_num: int,
    ep_title: str,
    segments: List[Dict[str, Any]],
    avoid_list: List[str],
    listener_notes: List[Dict[str, Any]],
    ballot_block: str,
    themes_block: str,
    locale: str,
    districts_profile: str,
    output_file: Path,
    length_calibration: Optional[Dict[str, Any]] = None,
) -> Brief:
    """One brief = one ~30-min episode written as ALEX/JORDAN dialogue.

    `length_calibration` (optional) is a dict produced by
    ``scanner.podcast._compute_length_calibration``. When supplied, the brief
    embeds a concrete word-count target plus a natural-language nudge
    derived from the last 5 days of shipped scripts so the Author can self-
    correct toward the 30-minute target.
    """
    cal = length_calibration or {}
    target_words = int(cal.get("target_words") or 3900)
    target_minutes = int(cal.get("target_minutes") or (target_words // 130))
    cal_note = cal.get("calibration_note") or (
        f"Length target: {target_words} words (~{target_minutes} min)."
    )
    trend = cal.get("trend") or []
    trend_str = (
        " · ".join(f"{w}w" for w in trend) if trend else "no shipped baseline yet"
    )

    instructions = (
        f"Write a ~{target_minutes}-minute episode {ep_num} of the daily "
        f"local-politics podcast for {target_date}. Topic: '{ep_title}'.\n\n"
        "FORMAT:\n"
        "  • Two hosts ALEX (presenter) and JORDAN (curious co-host).\n"
        "  • Every line starts `ALEX:` or `JORDAN:` — no stage directions, "
        "no markdown, no emoji, no '[laughs]', no asterisks.\n"
        f"  • LENGTH TARGET — {target_words} words "
        f"(±300; ~{target_minutes} min at 130 wpm).\n"
        "  • Cover the segments in `context.segments` in order. Each segment "
        "is a topic + a list of events (title, summary, url, date, level, "
        "politicians, role/stance). Cite specific votes and dates from the "
        "events — generalities are why prior drafts felt repetitive.\n\n"
        "PM LENGTH CALIBRATION (read this before drafting):\n"
        f"  • Recent shipped word counts: {trend_str}.\n"
        f"  • {cal_note}\n"
        "  • Hit the target. If you finish all the segments early, EXTEND "
        "    the candidate-history reads and the JORDAN pushbacks rather "
        "    than wrap; if a segment is bleeding length, tighten it without "
        "    losing specific citations.\n\n"
        "AVOID — do not reuse any framing on `context.avoid_list`. The PM "
        "agent has flagged these as already-shipped this week:\n  - "
        + "\n  - ".join((avoid_list or ['(none specified)'])[:30])
        + "\n\n"
        "LISTENER POSITIONING:\n"
        "  • The listener is a first-time voter in the configured locale.\n"
        f"  • Locale: {locale}.\n"
        f"  • Their districts:\n{districts_profile or '  (none configured)'}\n"
        "  • NEVER tell them to 'figure out who their candidate is', 'look "
        "up their district', or 'find out who's on their ballot'. The tool "
        "owns that work.\n"
        "  • The listener's recent daily_notes (in `context.listener_notes`) "
        "are direct asks — address at least ONE of them by name in this "
        "episode.\n\n"
        f"BALLOT CONTEXT (already-known races):\n{ballot_block or '(none)'}\n\n"
        f"PM SIGNAL BLOCK (what the listener cares about right now):\n"
        f"{themes_block or '(none yet)'}\n\n"
        "Write the full dialogue to `output_file`. Plain text only."
    )
    return Brief(
        brief_id=f"author_{target_date}_ep{ep_num}",
        type="author_episode",
        output_file=str(output_file),
        instructions=instructions,
        context={
            "target_date": target_date,
            "ep_num": ep_num,
            "ep_title": ep_title,
            "segments": segments,
            "avoid_list": avoid_list,
            "listener_notes": listener_notes[-10:],
            "ballot_block": ballot_block,
            "themes_block": themes_block,
            "locale": locale,
            "districts_profile": districts_profile,
            "length_calibration": cal,
        },
    )


def build_review_episode_brief(
    *,
    target_date: str,
    ep_num: int,
    ep_title: str,
    draft: str,
    prior_excerpts: str,
    notes_block: str,
    themes_block: str,
    output_file: Path,
) -> Brief:
    """Editor pass — Cowork reads the draft, returns the final + notes."""
    instructions = (
        f"Editor pass on episode {ep_num} ('{ep_title}', {target_date}).\n\n"
        "What to check:\n"
        "  1. REPETITION — compare against the last 5 days of shipped "
        "scripts shown in `context.prior_excerpts`. Flag verbatim AND "
        "paraphrased reuse: same framing, tagline, anecdote, metaphor.\n"
        "  2. AUDIENCE QUESTIONS — the listener notes in "
        "`context.notes_block` are from earlier days. If any can be "
        "addressed by today's draft, add 1-2 lines of delayed callback.\n"
        "  3. RECURRING THEMES — `context.themes_block` is the PM rollup. "
        "If today ignores a flagged theme or reuses an avoid-list item, "
        "treat as repetition.\n"
        "  4. LISTENER POSITIONING — never instruct the listener to look "
        "up their own district / candidate / ballot.\n"
        "  5. FORMAT — every line begins ALEX: or JORDAN: only. No stage "
        "directions, markdown, brackets, asterisks, emoji.\n\n"
        "Output to `output_file` as plain text in this exact format:\n\n"
        "NOTES: <one short sentence>\n"
        "VERDICT: approved | revised | order_rewrite\n"
        "REWRITE_REASON: <only when order_rewrite — one-sentence reason "
        "plus a semicolon-separated list of framings/taglines/anecdotes the "
        "rewrite must avoid; blank otherwise>\n"
        "===SCRIPT===\n"
        "<if VERDICT is 'revised', the full revised dialogue here, every "
        "line ALEX:/JORDAN:; if 'approved' or 'order_rewrite', leave empty>\n\n"
        "Hard rules:\n"
        "  • Preserve length and tone on surgical edits.\n"
        "  • Smallest change that fixes a real problem.\n"
        "  • If repetition is pervasive (>3 reused framings or full through-"
        "line mirrors a prior episode), VERDICT=order_rewrite and let the "
        "Author rebuild from scratch.\n"
        "  • Never invent facts."
    )
    return Brief(
        brief_id=f"review_{target_date}_ep{ep_num}",
        type="review_episode",
        output_file=str(output_file),
        instructions=instructions,
        context={
            "target_date": target_date,
            "ep_num": ep_num,
            "ep_title": ep_title,
            "draft": draft,
            "prior_excerpts": prior_excerpts,
            "notes_block": notes_block,
            "themes_block": themes_block,
        },
    )


def build_chat_brief(
    *,
    db_path: Path,
    conversation_id: int,
    message_id: int,
    question: str,
    digest_excerpt: str,
    locale: str,
    output_file: Optional[Path] = None,
) -> Brief:
    """One brief per user chat question. Cowork answers and updates the row."""
    instructions = (
        "Answer the user's question about today's local-politics digest.\n\n"
        f"Question:\n  {question}\n\n"
        "Use the digest excerpt in `context.digest_excerpt` plus, if "
        "helpful, web search to ground or update your answer. The user is "
        f"a first-time voter in {locale} — be concrete, cite sources, link "
        "to public records when applicable.\n\n"
        "Write your answer to `output_file` and ALSO update the SQLite "
        f"row at `{db_path}`: in `messages`, set the row with id="
        f"{message_id} (role='assistant') to your full markdown answer in "
        "the `content` column and set `status='answered'`. The web UI "
        "will then render the answer the next time the listener loads "
        "the conversation.\n\n"
        "If the question can be answered without web research from the "
        "digest alone, do that — be fast. If the digest doesn't contain "
        "enough, web-search the relevant offices, bills, or candidates."
    )
    output_file = output_file or (
        OUTBOX_DIR / f"chat_{conversation_id}_{message_id}.md"
    )
    return Brief(
        brief_id=f"chat_{conversation_id}_{message_id}",
        type="chat_question",
        output_file=str(output_file),
        instructions=instructions,
        context={
            "db_path": str(db_path),
            "conversation_id": conversation_id,
            "message_id": message_id,
            "question": question,
            "digest_excerpt": digest_excerpt[:8000],
            "locale": locale,
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# Per-candidate 4-episode SERIES (专题) — 4 × ~30-min episodes per candidate.
# Drives the candidate_series.json schedule built by scanner/series.py.
# ──────────────────────────────────────────────────────────────────────────────

# Standard 4-episode template. Each episode is its own brief.
SERIES_EPISODE_TEMPLATE = {
    1: {
        "title": "Origins",
        "scope": (
            "Birth, family, hometown, demographics. Schooling K-12, college, "
            "graduate work. Formative experiences and early-life public "
            "record (≤ age 25). Cite sources for everything. If you find a "
            "five-year unaccounted-for stretch, name it."
        ),
        "must_cover": [
            "year of birth + age",
            "place of birth + current residence",
            "family (spouse / kids if publicly disclosed)",
            "every school attended with degrees/years",
            "any high-school or college press coverage",
            "religion / language / ethnicity if publicly self-identified",
            "earliest public statements you can find (op-eds, interviews, social media archive)",
        ],
    },
    2: {
        "title": "Career",
        "scope": (
            "Professional history end-to-end, in chronological order, with "
            "employer + start/end years for every role. Public-facing work: "
            "media, op-eds, books, speeches, podcast interviews, X/Twitter "
            "and LinkedIn back to earliest archived posts. Court records, "
            "professional licensure, board memberships, charity work. "
            "Net worth and disclosed conflicts of interest."
        ),
        "must_cover": [
            "every job held with employer and dates",
            "military service if any",
            "boards / nonprofits / volunteer leadership",
            "public statements outside politics (op-eds, books, podcast guest spots)",
            "social media history — earliest posts you can find",
            "court records (PACER + state) if any",
            "publicly-disclosed financial picture",
        ],
    },
    3: {
        "title": "Political Record",
        "scope": (
            "Every prior political run (won AND lost). Every vote of public "
            "record. Sponsored / co-sponsored legislation with status. "
            "Endorsements received and given. Controversies, ethics "
            "complaints, FEC filings, news investigations — neutrally framed."
        ),
        "must_cover": [
            "all prior runs for office (won/lost) with dates and margins",
            "voting record by topic with bill numbers + dates",
            "sponsored/co-sponsored bills and their fate",
            "endorsements received and given, with dates",
            "controversies — alleged vs. proven vs. dismissed",
            "stance shifts over time, with the receipts",
        ],
    },
    4: {
        "title": "This Race",
        "scope": (
            "What's actually on the ballot for this seat in June 2026: who "
            "the opponents are, what each is saying they'll do, where they "
            "differ, what's at stake for a Rockville voter. End with: "
            "'if {candidate} wins, what changes in your daily life?'"
        ),
        "must_cover": [
            "the seat being contested + when the term starts",
            "every other candidate filed in the same primary, in one paragraph each",
            "the 3-5 substantive policy disagreements among them",
            "what listener-flagged topics (sanctuary policy, school funding, property tax, etc) come down to in this race",
            "what concretely changes for the listener if THIS candidate wins",
        ],
    },
}


def build_series_episode_brief(
    *,
    candidate_name: str,
    office: str,
    party: str,
    district: str,
    target_date: str,
    ep_num: int,
    dossier_path: Optional[Path],
    avoid_list: List[str],
    listener_notes: List[Dict[str, Any]],
    locale: str,
    districts_profile: str,
    output_file: Path,
    length_calibration: Optional[Dict[str, Any]] = None,
) -> Brief:
    """One brief = one episode of a candidate's 4-part series.

    The dossier produced by `candidate_dossier` is the source of truth — the
    Author reads it first, then writes the dialogue. If the dossier is thin
    (missing biographical sections), the brief instructs Cowork to deepen it
    BEFORE writing the script.
    """
    if ep_num not in SERIES_EPISODE_TEMPLATE:
        raise ValueError(f"ep_num must be 1-4, got {ep_num}")
    spec = SERIES_EPISODE_TEMPLATE[ep_num]
    cal = length_calibration or {}
    target_words = int(cal.get("target_words") or 3900)
    target_minutes = int(cal.get("target_minutes") or (target_words // 130))
    cal_note = cal.get("calibration_note") or (
        f"Length target: {target_words} words (~{target_minutes} min)."
    )

    must_cover_block = "\n".join(f"  • {item}" for item in spec["must_cover"])
    dossier_line = (
        f"DOSSIER (READ FIRST): `{dossier_path}` — this is the source of "
        "truth for biographical / career / political facts. Cite from it. "
        "If a section you need for episode {ep_num} is missing or thin, "
        "STOP and write a fresh `candidate_dossier` brief into the inbox "
        "for this candidate before writing today's script."
        if dossier_path else
        "No dossier exists yet for this candidate. STOP — write a fresh "
        "`candidate_dossier` brief into `cowork_inbox/` for this candidate "
        "first, drain it, THEN come back to this episode."
    ).format(ep_num=ep_num)

    instructions = (
        f"Write Episode {ep_num} ('{spec['title']}') of the 4-episode "
        f"专题 series on {candidate_name} ({office}, {party}, "
        f"district {district or 'unknown'}). Air date: {target_date}.\n\n"

        f"EPISODE {ep_num} SCOPE — {spec['title']}:\n{spec['scope']}\n\n"

        f"MUST COVER (do not skip):\n{must_cover_block}\n\n"

        f"{dossier_line}\n\n"

        "FORMAT:\n"
        "  • Two hosts ALEX (presenter) and JORDAN (curious co-host).\n"
        "  • Every line starts `ALEX:` or `JORDAN:` — no stage directions, "
        "    no markdown, no emoji, no '[laughs]', no asterisks.\n"
        f"  • LENGTH TARGET — {target_words} words "
        f"(±300; ~{target_minutes} min at 130 wpm).\n"
        "  • Cite specific names, dates, employers, schools, bill numbers, "
        "    URLs from the dossier — generalities are why earlier drafts "
        "    felt thin.\n\n"

        "PM LENGTH CALIBRATION:\n"
        f"  • {cal_note}\n"
        "  • Hit the target. If you finish the must-cover list early, "
        "    extend the JORDAN follow-up questions and add depth to the "
        "    biographical reads — never pad with filler.\n\n"

        "AVOID — do not reuse any framing on `context.avoid_list`. The PM "
        "agent has flagged these as already-shipped this week:\n  - "
        + "\n  - ".join((avoid_list or ['(none specified)'])[:30])
        + "\n\n"

        "LISTENER POSITIONING:\n"
        f"  • Locale: {locale}.\n"
        f"  • Their districts:\n{districts_profile or '  (none configured)'}\n"
        "  • The listener is a registered Democrat in Rockville, MD, "
        "    primary on June 23, 2026. Speak as though we already know "
        "    their districts; never tell them to 'look up their ballot'.\n"
        "  • If any item from `context.listener_notes` ties into this "
        "    candidate, address it by name in the dialogue.\n\n"

        "Write the full dialogue to `output_file`. Plain text only. "
        "If the dossier doesn't have the facts you need, do NOT invent — "
        "have a host say on-air 'we don't have a public record of X yet' "
        "and move on."
    )
    return Brief(
        brief_id=f"series_{target_date}_{_slugify(candidate_name)}_ep{ep_num}",
        type="series_episode",
        output_file=str(output_file),
        instructions=instructions,
        context={
            "candidate_name": candidate_name,
            "office": office,
            "party": party,
            "district": district,
            "target_date": target_date,
            "ep_num": ep_num,
            "ep_title": spec["title"],
            "ep_scope": spec["scope"],
            "must_cover": spec["must_cover"],
            "dossier_path": str(dossier_path) if dossier_path else None,
            "avoid_list": avoid_list,
            "listener_notes": listener_notes[-10:],
            "locale": locale,
            "districts_profile": districts_profile,
            "length_calibration": cal,
        },
    )


def build_filing_monitor_brief(
    *,
    state_board_url: str = "https://elections.maryland.gov/elections/2026/primary_candidates/2026_GP_statewide_candidatelist.html",
    local_board_url: str = "https://elections.maryland.gov/elections/2026/primary_candidates/2026_GP_all_counties_candidatelist.html",
    registry_path: Path,
    relevant_offices: List[str],
    relevant_districts: List[str],
    output_file: Optional[Path] = None,
) -> Brief:
    """
    Brief that asks Cowork to re-fetch the State Board of Elections candidate
    lists daily, diff against `candidate_series.json`, and add/remove rows
    for races that affect this listener (their districts only — federal MD,
    statewide, MD-8, Leg 19, Council D7, etc).
    """
    today = datetime.now().astimezone().date().isoformat()
    instructions = (
        "Fetch the two SBE candidate-list pages and diff them against the "
        f"local registry at `{registry_path}`.\n\n"
        f"Statewide list URL: {state_board_url}\n"
        f"Local-races list URL: {local_board_url}\n\n"
        "Within those pages, ONLY consider candidates running for offices "
        "the listener can vote on — match by office + district against:\n"
        + "\n".join(f"  • {o}" for o in (relevant_offices or []))
        + "\n\nAnd districts:\n"
        + "\n".join(f"  • {d}" for d in (relevant_districts or []))
        + "\n\n"
        "For every relevant candidate found on the SBE page that is NOT in "
        "the registry: append a new entry following the schema of existing "
        "rows (4 episodes, status='pending', dossier_status='pending', and "
        "scheduled_date set to the next available open day after the last "
        "scheduled candidate, one per day). Tier follows: tier 1 = your-"
        "district races (Council D7, House D19, Senate D19, MD-8, MoCo "
        "Executive, MoCo Council At-Large), tier 2 = statewide, tier 3 = "
        "county-wide non-district (state's attorney, sheriff, clerk, "
        "register, orphans), tier 4 = judicial / school board.\n\n"
        "For every registry candidate NOT on the SBE page (i.e., they "
        "withdrew): set their `dossier_status` to 'withdrawn' and "
        "`scheduled_date` to null. Don't delete — keep history.\n\n"
        "Update `last_sbe_check` to today's ISO timestamp. If the SBE page "
        "indicates the filing window has closed AND a full final list "
        "is published (after Feb 24 + any cure period), set "
        "`list_finalized` to true.\n\n"
        "Write a short JSON summary to `output_file`: "
        "`{added: [names...], removed: [names...], renamed: [...], "
        "total_active: N, list_finalized: bool, run_date: ISO}`. Also "
        "append one line to `cowork_outbox/_log.md`: "
        "'YYYY-MM-DD monitor: +N new, -K withdrawn (total active: T)'."
    )
    output_file = output_file or (
        OUTBOX_DIR / f"filing_monitor_{today}.json"
    )
    return Brief(
        brief_id=f"filing_monitor_{today}",
        type="filing_monitor",
        output_file=str(output_file),
        instructions=instructions,
        context={
            "state_board_url": state_board_url,
            "local_board_url": local_board_url,
            "registry_path": str(registry_path),
            "relevant_offices": relevant_offices,
            "relevant_districts": relevant_districts,
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# Dossier scout — fast reconnaissance per candidate.  Run once early in the
# series cycle to estimate which candidates have rich public records vs. thin
# ones, so the schedule can be re-ordered (rich-first) and thin candidates
# get extra research days before their air date.
# ──────────────────────────────────────────────────────────────────────────────

def build_dossier_scout_brief(
    *,
    candidate_name: str,
    office: str,
    party: str,
    district: str,
    output_file: Path,
) -> Brief:
    """Cheap web reconnaissance — NOT the full dossier. ~5-10 min of search,
    NO synthesis to Markdown. Returns a JSON assessment so the scheduler can
    decide ordering."""
    instructions = (
        f"Do a 5-10 minute web reconnaissance on {candidate_name} ({office}, "
        f"{party}, district {district or 'unknown'}). Goal: estimate how "
        "rich vs. thin their public record is, so the series scheduler can "
        "decide when to air their episodes.\n\n"

        "What to do (fast, NOT exhaustive):\n"
        "  1. One Google search for their full name + 'Maryland' or the "
        "     office they're running for. Note rough hit count + quality.\n"
        "  2. Check if they have a campaign website. Note url + how much "
        "     biographical content it has.\n"
        "  3. Check Wikipedia / Ballotpedia.\n"
        "  4. Check LinkedIn (look for first archived appearance year).\n"
        "  5. Check whether their name appears in:\n"
        "       • Maryland legislative DB (mgaleg.maryland.gov)\n"
        "       • congress.gov / GovTrack legacy\n"
        "       • Montgomery County legislative DB\n"
        "       • Court records (PACER, MD Judiciary Case Search)\n"
        "       • Any major newspaper archive (Baltimore Banner, Bethesda "
        "         Magazine, WTOP, Maryland Matters, Washington Post)\n"
        "  6. Note their estimated year of birth if findable.\n\n"

        "Do NOT write the dossier. Do NOT do the deep age-(x-20) lookback. "
        "Do NOT cite every claim. This is just RECONNAISSANCE.\n\n"

        "Output a JSON file at `output_file` exactly like:\n"
        "{\n"
        '  "candidate_name": "<name>",\n'
        '  "richness_score": 0.0..1.0,         # 1.0 = decades of public record, 0.1 = first-time activist with no trail\n'
        '  "estimated_age": <int or null>,\n'
        '  "estimated_research_hours": <float>,  # how long the deep dossier will take. 0.5 (rich) to 6.0 (need archival digging) or 99 if essentially nothing exists\n'
        '  "has_campaign_site": true|false,\n'
        '  "has_wikipedia": true|false,\n'
        '  "has_ballotpedia": true|false,\n'
        '  "has_legislative_record": true|false,\n'
        '  "has_court_record_search_hits": true|false,\n'
        '  "earliest_news_year": <int or null>,\n'
        '  "missing_critical_sections": ["education", "career", "political_record"],  # any of: demographics, socioeconomic, education, career, political_record\n'
        '  "notes": "<one paragraph for the scheduler — what is and isn\'t findable, any name disambiguation issues, any plausible angles for an office_primer episode if dossier is thin>"\n'
        "}\n\n"
        "Be honest. If a candidate genuinely has nothing — say richness_score "
        "0.1 and explain why in `notes`. The system uses these to decide "
        "whether to switch episode 4 from biography to an office-primer "
        "episode about the seat itself."
    )
    slug = _slugify(candidate_name)
    return Brief(
        brief_id=f"scout_{slug}",
        type="dossier_scout",
        output_file=str(output_file),
        instructions=instructions,
        context={
            "candidate_name": candidate_name,
            "office": office,
            "party": party,
            "district": district,
        },
    )


def build_office_primer_brief(
    *,
    office: str,
    district: str,
    candidates_in_race: List[str],
    listener_locale: str,
    target_date: str,
    ep_num: int,
    output_file: Path,
    avoid_list: List[str],
    listener_notes: List[Dict[str, Any]],
    length_calibration: Optional[Dict[str, Any]] = None,
    why_this_episode: str = "",
) -> Brief:
    """
    Fallback episode for days when a candidate's dossier is too thin to
    sustain biography. Talks about the OFFICE itself: what it does, who
    has held it, why it matters in Rockville, what the listener is
    actually choosing between in this primary.

    Used as Episode 4 when scout flags a thin candidate, OR as a full-day
    replacement when the listener writes "switch X to position content"
    in their daily_note.
    """
    cal = length_calibration or {}
    target_words = int(cal.get("target_words") or 3900)
    target_minutes = int(cal.get("target_minutes") or (target_words // 130))
    cal_note = cal.get("calibration_note") or (
        f"Length target: {target_words} words (~{target_minutes} min)."
    )

    rivals_block = (
        "\n  ".join(f"• {c}" for c in (candidates_in_race or []))
        or "  (registry didn't supply other candidates yet)"
    )

    instructions = (
        f"Write a ~{target_minutes}-min ALEX/JORDAN dialogue episode about "
        f"THE SEAT itself: {office} (district {district or 'n/a'}).\n\n"

        f"Why this episode (instead of a candidate biography): {why_this_episode}\n\n"

        "Cover, in order:\n"
        "  1. WHAT THE SEAT DOES — formal powers, what bills/budget/policy "
        "     levers come through this office, term length, salary, staff. "
        "     Cite the relevant section of the Maryland Constitution / "
        "     Annotated Code / county charter.\n"
        "  2. SHORT HISTORY — the past three or four occupants, with their "
        "     years and one notable thing each. Cite Wikipedia / official "
        "     biographies. If it's a brand-new seat (post-redistricting), "
        "     say so.\n"
        "  3. WHY IT MATTERS IN ROCKVILLE — the concrete decisions this "
        "     seat makes that touch the listener's daily life: property "
        "     tax rates, school funding, sanctuary policy, transit, "
        "     development zoning, etc. Be specific.\n"
        "  4. THE FIELD IN 2026 — every candidate filed for this seat:\n"
        f"  {rivals_block}\n"
        "     For each, one paragraph: what they're saying they'll do, "
        "     where their record (or lack of it) leaves the listener.\n"
        "  5. WHAT THE LISTENER IS ACTUALLY DECIDING — frame the trade-off "
        "     as concretely as possible: \"if you vote for X you get more "
        "     of Y; if Z wins you get more of W.\"\n\n"

        "FORMAT:\n"
        "  • ALEX (presenter) and JORDAN (curious co-host).\n"
        "  • Every line `ALEX:` or `JORDAN:`. No stage directions, "
        "    markdown, emoji, brackets.\n"
        f"  • Length target: {target_words} words.\n"
        f"  • {cal_note}\n\n"

        "AVOID — not on this list:\n  - "
        + "\n  - ".join((avoid_list or ['(none specified)'])[:30])
        + "\n\n"

        f"Listener locale: {listener_locale}.\n"
        "If any item from `context.listener_notes` ties into this office, "
        "address it by name.\n\n"

        "Plain-text dialogue to `output_file`. Cite sources for the office "
        "powers section (constitutional / statutory references)."
    )
    return Brief(
        brief_id=f"office_primer_{target_date}_{_slugify(office)}_ep{ep_num}",
        type="office_primer",
        output_file=str(output_file),
        instructions=instructions,
        context={
            "office": office,
            "district": district,
            "candidates_in_race": candidates_in_race,
            "listener_locale": listener_locale,
            "target_date": target_date,
            "ep_num": ep_num,
            "avoid_list": avoid_list,
            "listener_notes": listener_notes[-10:],
            "length_calibration": cal,
            "why_this_episode": why_this_episode,
        },
    )
