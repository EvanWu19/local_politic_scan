"""
Candidate dossier orchestrator — emits Cowork briefs that ask the Cowork
agent (Opus 4.7 with web search) to build a sourced research dossier per
candidate the listener has flagged or the Collector has discovered.

Why this module exists
----------------------
The listener has explicitly asked, in their daily_notes, for actual voting
records, prior public statements, and policy histories per candidate — and
the existing Sonnet-driven podcast pipeline doesn't fetch any of that. The
processor only sees ~800 chars of text per event, which is fine for tagging
but useless for building a candidate biography.

This module:
  1. Picks the candidates that should have a fresh dossier (filed for the
     2026 ballot, mentioned recently in daily_notes, or with an active
     consistency-score row),
  2. Pulls whatever local context we already have (the politician row, their
     linked events, their latest consistency score, listener-flagged topics
     from weekly_themes),
  3. Writes one cowork_inbox/ brief per candidate.

The Cowork agent picks these up on its next scheduled run, web-searches each
candidate, writes the dossier file, and the Author / deep-dive paths read it
back the next morning.

Run via ``python main.py dossier`` or programmatically.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from config import Config as _Cfg
from scanner.cowork_bridge import (
    Brief,
    build_dossier_brief,
    write_brief,
)

log = logging.getLogger(__name__)

# Don't refresh more often than this — web research is the expensive bit and
# voting records don't change daily. Listener can force a re-run with
# ``main.py dossier --force "Wes Moore"``.
DEFAULT_REFRESH_DAYS = 7
MAX_DOSSIERS_PER_RUN = 8   # cap so a fresh DB doesn't queue 200 briefs at once

# Look at the last 14 days of brief outcomes when deciding whether to retry.
RETRY_LOOKBACK_DAYS = 14

# The Opus model the Cowork agent should use when running these briefs.
# Override with COWORK_DOSSIER_MODEL env var if the listener's account
# doesn't yet expose 4.7.
DEFAULT_COWORK_MODEL = "claude-opus-4-7"


def queue_dossier_briefs(
    db_path: Path,
    *,
    output_dir: Optional[Path] = None,
    refresh_days: int = DEFAULT_REFRESH_DAYS,
    only_names: Optional[Sequence[str]] = None,
    today: Optional[date] = None,
    force: bool = False,
    max_briefs: int = MAX_DOSSIERS_PER_RUN,
) -> List[str]:
    """
    Decide who needs a dossier and queue a Cowork brief for each.

    Returns the list of candidate names that were queued.
    """
    today = today or date.today()
    output_dir = output_dir or _Cfg.CANDIDATE_DOSSIER_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    candidates = _select_candidates(
        db_path, today=today, refresh_days=refresh_days,
        only_names=only_names, force=force, output_dir=output_dir,
    )

    if not candidates:
        log.info("dossier: nothing to queue (refresh_days=%d, force=%s)",
                 refresh_days, force)
        return []

    listener_focus = _load_listener_focus(db_path)
    queued: List[str] = []

    for cand in candidates[:max_briefs]:
        events = _load_events_for_candidate(db_path, cand["id"])
        brief = build_dossier_brief(
            candidate_name=cand["name"],
            office=cand.get("office", "") or "",
            party=cand.get("party", "") or "",
            district=cand.get("district", "") or "",
            known_events=events,
            listener_focus=listener_focus,
            output_dir=output_dir,
            today=today.isoformat(),
        )
        write_brief(brief)
        queued.append(cand["name"])
        log.info("dossier: queued brief for %s", cand["name"])

    return queued


# ──────────────────────────────────────────────────────────────────────────────
# Candidate selection
# ──────────────────────────────────────────────────────────────────────────────

def _select_candidates(
    db_path: Path, *, today: date, refresh_days: int,
    only_names: Optional[Sequence[str]], force: bool, output_dir: Path,
) -> List[Dict[str, Any]]:
    """
    Decide which politicians get a fresh dossier this run. Order of priority:

      1. Names the user passed via ``only_names`` (always queued).
      2. Names mentioned in the 5 most recent daily_notes (listener interest).
      3. Politicians with ``candidate_status='filed'`` for this year's ballot.
      4. Politicians with a recent ``consistency_scores`` row (Analyst flagged
         them as having enough events to assess).

    A candidate is skipped if a dossier file already exists AND was modified
    in the last ``refresh_days`` days, unless ``force=True``.
    """
    cutoff_mtime = (today - timedelta(days=refresh_days))
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        all_pols = {r["name"].lower(): dict(r)
                    for r in con.execute(
                        "select id, name, office, party, level, district, "
                        "       candidate_status, ballot_year "
                        "from politicians")}
    finally:
        con.close()

    chosen: Dict[str, Dict[str, Any]] = {}

    def _add(name: str) -> None:
        row = all_pols.get(name.lower())
        if row is None:
            log.debug("dossier: %r not in politicians table — skipping", name)
            return
        if row["name"] in chosen:
            return
        if not force and _dossier_is_fresh(output_dir, row["name"], cutoff_mtime):
            log.debug("dossier: %s is fresh — skipping", row["name"])
            return
        chosen[row["name"]] = row

    # 1. Explicit names
    for n in only_names or []:
        _add(n)

    # 2. Listener interest (recent notes)
    for name in _names_from_recent_notes(db_path, today):
        _add(name)

    # 3. Filed candidates for this ballot year
    for row in all_pols.values():
        if (row.get("candidate_status") or "").lower() == "filed":
            if row.get("ballot_year") in (None, "", today.year, str(today.year)):
                _add(row["name"])

    # 4. Recent Analyst output
    for name in _names_with_recent_consistency(db_path, today):
        _add(name)

    # Stable ordering: only_names first, then alphabetical
    explicit = [n for n in (only_names or []) if all_pols.get(n.lower())]
    rest = sorted([r for r in chosen.values() if r["name"] not in explicit],
                  key=lambda r: r["name"])
    explicit_rows = [chosen[all_pols[n.lower()]["name"]] for n in explicit
                     if all_pols.get(n.lower()) and
                        all_pols[n.lower()]["name"] in chosen]
    return explicit_rows + rest


def _dossier_is_fresh(output_dir: Path, name: str, cutoff: date) -> bool:
    from scanner.cowork_bridge import _slugify  # internal helper
    p = output_dir / f"{_slugify(name)}.md"
    if not p.exists():
        return False
    mtime = date.fromtimestamp(p.stat().st_mtime)
    return mtime >= cutoff


def _names_from_recent_notes(db_path: Path, today: date,
                              days: int = 7) -> List[str]:
    """Pull names that appear in the last `days` of daily_notes."""
    con = sqlite3.connect(str(db_path))
    try:
        start = (today - timedelta(days=days)).isoformat()
        rows = list(con.execute(
            "select content from daily_notes where report_date >= ? "
            "order by report_date desc", (start,)))
    finally:
        con.close()
    if not rows:
        return []

    # Pull every politician name and look for case-insensitive substring hits.
    con = sqlite3.connect(str(db_path))
    try:
        all_names = [r[0] for r in con.execute("select name from politicians")]
    finally:
        con.close()

    found: List[str] = []
    for content, in rows:
        text = (content or "").lower()
        for n in all_names:
            if n.lower() in text and n not in found:
                found.append(n)
    return found


# ──────────────────────────────────────────────────────────────────────────────
# Brief-status introspection — used by the reporter spotlight fallback and
# the --retry-failed CLI path so we can tell a stuck brief from one that's
# still in flight.
# ──────────────────────────────────────────────────────────────────────────────

def _list_briefs_for(slug: str, status_suffix: str) -> List[Path]:
    from scanner.cowork_bridge import INBOX_DIR
    if not INBOX_DIR.exists():
        return []
    return sorted(INBOX_DIR.glob(f"dossier_*_{slug}{status_suffix}"))


def describe_dossier_status(name: str) -> Dict[str, str]:
    """Return {state, when} describing the latest Cowork outcome for `name`:
      - state='done'    → most recent brief finished (file should be on disk)
      - state='error'   → most recent brief errored
      - state='queued'  → brief is sitting in the inbox awaiting Cowork
      - state='missing' → no brief found in the inbox at all
    """
    from scanner.cowork_bridge import _slugify
    slug = _slugify(name)

    candidates: List[tuple] = []
    for suffix, state in ((".done.json", "done"),
                          (".error.json", "error"),
                          (".json", "queued")):
        for p in _list_briefs_for(slug, suffix):
            # `.json` glob also catches .done/.error — filter those out.
            if suffix == ".json" and (
                p.name.endswith(".done.json") or p.name.endswith(".error.json")
            ):
                continue
            candidates.append((p.stat().st_mtime, state, p))
    if not candidates:
        return {"state": "missing", "when": ""}

    candidates.sort(reverse=True)
    _, state, p = candidates[0]
    when = datetime.fromtimestamp(p.stat().st_mtime).date().isoformat()
    return {"state": state, "when": when}


def list_failed_briefs(within_days: int = RETRY_LOOKBACK_DAYS) -> List[Dict[str, Any]]:
    """Return [{slug, candidate_name, error_path, age_days}] for every
    .error.json brief in the last `within_days`."""
    from scanner.cowork_bridge import INBOX_DIR
    out: List[Dict[str, Any]] = []
    cutoff = datetime.now() - timedelta(days=within_days)
    if not INBOX_DIR.exists():
        return out
    for p in INBOX_DIR.glob("dossier_*.error.json"):
        if datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "slug": p.stem.replace(".error", "").split("_", 2)[-1],
            "candidate_name": data.get("context", {}).get("candidate_name", ""),
            "error_path": str(p),
            "age_days": (datetime.now()
                         - datetime.fromtimestamp(p.stat().st_mtime)).days,
        })
    return out


def retry_failed_briefs(db_path: Path, *,
                         output_dir: Optional[Path] = None,
                         today: Optional[date] = None,
                         max_briefs: int = MAX_DOSSIERS_PER_RUN) -> List[str]:
    """Requeue every candidate whose last brief errored in the last
    `RETRY_LOOKBACK_DAYS`. Uses the gap-filling instructions so Cowork
    no longer refuses on candidates with empty office/party/district."""
    failed = list_failed_briefs()
    if not failed:
        log.info("dossier retry: nothing in error state")
        return []
    names = [f["candidate_name"] for f in failed if f["candidate_name"]]
    if not names:
        log.warning("dossier retry: %d error briefs but none parseable",
                    len(failed))
        return []
    log.info("dossier retry: requeuing %d candidate(s)", len(names))
    return queue_dossier_briefs(
        db_path=db_path,
        output_dir=output_dir,
        only_names=names,
        today=today,
        force=True,
        max_briefs=max_briefs,
    )


def _names_with_recent_consistency(db_path: Path, today: date,
                                     days: int = 14) -> List[str]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        cutoff = (today - timedelta(days=days)).isoformat()
        rows = list(con.execute(
            "select distinct p.name "
            "from consistency_scores cs "
            "join politicians p on p.id = cs.politician_id "
            "where cs.generated_at >= ? "
            "order by cs.generated_at desc", (cutoff,)))
    finally:
        con.close()
    return [r["name"] for r in rows]


def _load_events_for_candidate(db_path: Path, pol_id: int,
                                 limit: int = 30) -> List[Dict[str, Any]]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = list(con.execute(
            "select e.id, e.title, e.summary, e.source_url, e.date, e.level, "
            "       pe.role, pe.stance "
            "from politician_events pe "
            "join events e on e.id = pe.event_id "
            "where pe.politician_id = ? "
            "order by e.date desc limit ?",
            (pol_id, limit)))
    finally:
        con.close()
    return [dict(r) for r in rows]


def _load_listener_focus(db_path: Path) -> List[str]:
    """Pull the most recent weekly_themes underserved_topics + open_questions."""
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "select underserved_topics, open_questions "
            "from weekly_themes order by week_end desc limit 1").fetchone()
    finally:
        con.close()
    if not row:
        return []
    out: List[str] = []
    for field in ("underserved_topics", "open_questions"):
        raw = row[field] or ""
        try:
            data = json.loads(raw) if raw.strip().startswith("[") else None
        except Exception:
            data = None
        if isinstance(data, list):
            out.extend(str(x) for x in data)
        else:
            # fallback: bullet-list text
            for line in raw.splitlines():
                line = line.strip("-• ").strip()
                if line:
                    out.append(line)
    # Dedupe, cap
    seen, unique = set(), []
    for s in out:
        k = s.lower()
        if k not in seen:
            seen.add(k)
            unique.append(s)
    return unique[:12]
