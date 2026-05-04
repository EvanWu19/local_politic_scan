"""
4-episode-per-candidate series (专题) orchestration.

The user's June 23, 2026 ballot has ~22+ named candidates plus more to come
once the at-large council, judicial, sheriff, school board lists are pulled.
For each one, this module produces a 4-episode (~2-hour) deep-dive across:

    Ep 1 — Origins         (birth, family, education, formative years)
    Ep 2 — Career          (jobs, public statements, social-media trail)
    Ep 3 — Political Record (prior runs, votes, controversies, endorsements)
    Ep 4 — This Race       (opponents, what's at stake, what changes if they win)

Each episode is a Cowork brief (`series_episode` type). The dossier brief
(`candidate_dossier`) is the source of truth and must be filled BEFORE the
episodes ship — we instruct Cowork to escalate dossier-thin candidates back
to a fresh dossier brief.

Registry
--------
``data/candidate_series.json`` is the single source of truth for who's in,
when they air, and what's done. It looks like::

    {
      "version": 1,
      "primary_date": "2026-06-23",
      "list_finalized": false,
      "last_sbe_check": "2026-05-04T...",
      "candidates": [
        {
          "name": "Dawn Luedtke",
          "office": "Montgomery County Council District 7",
          "tier": 1,
          "scheduled_date": "2026-05-04",
          "dossier_status": "pending|exists_but_thin|complete|withdrawn",
          "dossier_path": "data/candidate_dossiers/dawn-luedtke.md",
          "episodes": [
            {"num": 1, "title": "Origins", "status": "pending|queued|done"},
            ...
          ]
        }, ...
      ]
    }

Daily flow
----------
1. ``cmd_publish`` (07:00 Windows) calls ``queue_today_series()`` which:
     a. Looks up today's candidate by ``scheduled_date``.
     b. Queues a fresh `candidate_dossier` brief if needed.
     c. Queues 4 `series_episode` briefs.
     d. Marks each episode status='queued'.
2. The Cowork drain task (22:30 nightly) processes the inbox: dossier first,
   then 4 episodes. Marks them 'done' when written.
3. ``cmd_tts_publish`` (07:00 next morning) renders MP3s for any
   newly-written episodes.

Monitor flow
------------
A separate Cowork scheduled task (``monitor-candidate-list``, daily) calls
``main.py series-monitor`` which writes a ``filing_monitor`` brief asking
Cowork to re-fetch the SBE pages and reconcile against the registry —
adding new candidates and marking withdrawn ones, until ``list_finalized``
flips true.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

# Resolved at import time relative to project root (this file lives in
# ``scanner/``, so parent.parent is the project root).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = _PROJECT_ROOT / "data" / "candidate_series.json"


# ──────────────────────────────────────────────────────────────────────────────
# Registry I/O
# ──────────────────────────────────────────────────────────────────────────────

def load_registry(path: Optional[Path] = None) -> Dict[str, Any]:
    p = path or REGISTRY_PATH
    if not p.exists():
        return {
            "version": 1,
            "primary_date": "2026-06-23",
            "list_finalized": False,
            "last_sbe_check": None,
            "candidates": [],
        }
    return json.loads(p.read_text(encoding="utf-8"))


def save_registry(reg: Dict[str, Any], path: Optional[Path] = None) -> None:
    p = path or REGISTRY_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, indent=2, ensure_ascii=False),
                 encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# Lookups
# ──────────────────────────────────────────────────────────────────────────────

def candidate_for_date(target_date: date,
                        reg: Optional[Dict[str, Any]] = None
                        ) -> Optional[Dict[str, Any]]:
    """Return the registry entry scheduled for `target_date`, or None."""
    reg = reg or load_registry()
    iso = target_date.isoformat()
    for c in reg.get("candidates", []):
        if c.get("scheduled_date") == iso and c.get("dossier_status") != "withdrawn":
            return c
    return None


def find_candidate(name: str, reg: Optional[Dict[str, Any]] = None
                    ) -> Optional[Dict[str, Any]]:
    reg = reg or load_registry()
    nl = name.lower().strip()
    for c in reg.get("candidates", []):
        if c.get("name", "").lower() == nl:
            return c
    # Fallback: substring match on either side
    for c in reg.get("candidates", []):
        cn = c.get("name", "").lower()
        if nl in cn or cn in nl:
            return c
    return None


def all_candidate_names(reg: Optional[Dict[str, Any]] = None,
                          *, active_only: bool = True) -> List[str]:
    """Names used to filter incoming news (processor.py)."""
    reg = reg or load_registry()
    out = []
    for c in reg.get("candidates", []):
        if active_only and c.get("dossier_status") == "withdrawn":
            continue
        nm = c.get("name", "").strip()
        if nm:
            out.append(nm)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Daily queueing — called by cmd_publish each morning
# ──────────────────────────────────────────────────────────────────────────────

def queue_today_series(target_date: Optional[date] = None,
                         podcasts_dir: Optional[Path] = None,
                         dossier_dir: Optional[Path] = None,
                         db_path: Optional[Path] = None,
                         ) -> Dict[str, Any]:
    """
    Queue the dossier (if needed) + 4 episode briefs for the candidate
    scheduled to air on `target_date`. Returns a result dict.
    """
    from scanner.cowork_bridge import (
        build_dossier_brief, build_series_episode_brief, write_brief,
    )

    target_date = target_date or date.today()
    reg = load_registry()
    cand = candidate_for_date(target_date, reg)
    if not cand:
        log.info("series: no candidate scheduled for %s", target_date)
        return {"status": "no_candidate", "date": target_date.isoformat()}

    name = cand["name"]
    log.info("series: today's candidate is %s (%s)", name, cand.get("office"))

    # ── Resolve paths ───────────────────────────────────────────────────────
    try:
        from config import Config as _Cfg
        podcasts_dir = podcasts_dir or _Cfg.PODCASTS_DIR
        dossier_dir = dossier_dir or _Cfg.CANDIDATE_DOSSIER_DIR
        db_path = db_path or _Cfg.DB_PATH
        locale = ", ".join(p for p in [_Cfg.CITY, _Cfg.COUNTY, _Cfg.STATE] if p) or "the local area"
        districts_profile = (_Cfg.districts_profile()
                              if hasattr(_Cfg, "districts_profile") else "")
    except Exception:
        podcasts_dir = podcasts_dir or _PROJECT_ROOT / "podcasts"
        dossier_dir = dossier_dir or _PROJECT_ROOT / "data" / "candidate_dossiers"
        db_path = db_path or _PROJECT_ROOT / "data" / "politics.db"
        locale = "Rockville, Montgomery County, Maryland"
        districts_profile = ""

    podcasts_dir = Path(podcasts_dir); podcasts_dir.mkdir(parents=True, exist_ok=True)
    dossier_dir = Path(dossier_dir); dossier_dir.mkdir(parents=True, exist_ok=True)

    dossier_path = (Path(cand.get("dossier_path"))
                     if cand.get("dossier_path") else dossier_dir / f"{_slug(name)}.md")
    if not dossier_path.is_absolute():
        dossier_path = _PROJECT_ROOT / dossier_path

    # ── 1. Dossier brief if needed ──────────────────────────────────────────
    needs_dossier = (
        cand.get("dossier_status") in (None, "pending", "exists_but_thin")
        or not dossier_path.exists()
        or (dossier_path.exists() and dossier_path.stat().st_size < 4000)
    )
    if needs_dossier:
        # Listener focus comes from the most recent weekly_themes row OR a
        # sensible default. Fall through to defaults if DB read fails.
        focus = _load_listener_focus(Path(db_path)) or [
            "sanctuary policy",
            "school funding",
            "property tax",
            "policing",
            "K-12 curriculum",
            "transit",
            "housing zoning",
        ]
        # Known events from the local DB — best effort.
        known_events = _load_events_for_name(Path(db_path), name)

        dossier_brief = build_dossier_brief(
            candidate_name=name,
            office=cand.get("office", ""),
            party=cand.get("party", ""),
            district=cand.get("district", ""),
            known_events=known_events,
            listener_focus=focus,
            output_dir=Path(dossier_dir),
            today=target_date.isoformat(),
        )
        write_brief(dossier_brief)
        cand["dossier_status"] = "queued"
        log.info("series: queued dossier brief for %s", name)

    # ── 2. Length calibration shared across all 4 episodes ──────────────────
    try:
        from scanner.podcast import _compute_length_calibration
        cal = _compute_length_calibration(podcasts_dir, target_date)
    except Exception as e:
        log.warning("series: calibration unavailable: %s", e)
        cal = None

    # ── 3. Listener notes payload ───────────────────────────────────────────
    listener_notes = _load_recent_listener_notes(Path(db_path), target_date)

    # ── 4. Avoid list (PM's recent-coverage list) ───────────────────────────
    avoid_list = _load_recent_avoid_list(Path(db_path))

    # ── 5. Queue 4 episode briefs ───────────────────────────────────────────
    queued_eps: List[int] = []
    for ep_meta in cand.get("episodes", []):
        ep_num = int(ep_meta.get("num", 0))
        if ep_num not in (1, 2, 3, 4):
            continue
        slug = _slug(name)
        out_file = (Path(podcasts_dir)
                    / f"podcast_{target_date.isoformat()}_series_{slug}_ep{ep_num}.txt")
        brief = build_series_episode_brief(
            candidate_name=name,
            office=cand.get("office", ""),
            party=cand.get("party", ""),
            district=cand.get("district", ""),
            target_date=target_date.isoformat(),
            ep_num=ep_num,
            dossier_path=dossier_path,
            avoid_list=avoid_list,
            listener_notes=listener_notes,
            locale=locale,
            districts_profile=districts_profile,
            output_file=out_file,
            length_calibration=cal,
        )
        write_brief(brief)
        ep_meta["status"] = "queued"
        ep_meta["script_path"] = str(out_file)
        queued_eps.append(ep_num)
        log.info("series: queued ep%d brief for %s", ep_num, name)

    save_registry(reg)
    return {
        "status": "queued",
        "date": target_date.isoformat(),
        "candidate": name,
        "office": cand.get("office", ""),
        "dossier_queued": needs_dossier,
        "episodes_queued": queued_eps,
    }


def queue_filing_monitor() -> Path:
    """
    Write a `filing_monitor` brief into the inbox so the Cowork agent re-
    fetches the State Board of Elections candidate-list pages and reconciles
    against `data/candidate_series.json` overnight.
    """
    from scanner.cowork_bridge import build_filing_monitor_brief, write_brief

    relevant_offices = [
        "Governor of Maryland",
        "Maryland Attorney General",
        "Maryland Comptroller",
        "U.S. Representative MD-8",
        "Maryland State Senate District 19",
        "Maryland House of Delegates District 19",
        "Montgomery County Executive",
        "Montgomery County Council District 7",
        "Montgomery County Council At-Large",
        "Montgomery County State's Attorney",
        "Montgomery County Sheriff",
        "Montgomery County Clerk of the Circuit Court",
        "Montgomery County Register of Wills",
        "Montgomery County Judge of the Orphans' Court",
        "Montgomery County Board of Education",
        "Circuit Court Judge — 6th Judicial Circuit",
    ]
    relevant_districts = [
        "MD-8",
        "Legislative District 19",
        "Council D7",
        "MoCo countywide",
        "MD statewide",
    ]
    brief = build_filing_monitor_brief(
        registry_path=REGISTRY_PATH,
        relevant_offices=relevant_offices,
        relevant_districts=relevant_districts,
    )
    return write_brief(brief)


# ──────────────────────────────────────────────────────────────────────────────
# Episode completion bookkeeping (called when Cowork-produced .txt lands)
# ──────────────────────────────────────────────────────────────────────────────

def reconcile_completed_episodes(podcasts_dir: Optional[Path] = None) -> int:
    """
    Walk every registry candidate; for each episode whose script_path now
    exists on disk with real content (>500 bytes), set status='done'.
    Returns count of newly-completed episodes.
    """
    reg = load_registry()
    pod = Path(podcasts_dir) if podcasts_dir else _PROJECT_ROOT / "podcasts"
    new_done = 0
    for cand in reg.get("candidates", []):
        for ep in cand.get("episodes", []):
            if ep.get("status") == "done":
                continue
            sp = ep.get("script_path")
            if not sp:
                continue
            p = Path(sp)
            if p.exists() and p.stat().st_size > 500:
                ep["status"] = "done"
                new_done += 1
    if new_done:
        save_registry(reg)
        log.info("series: reconciled %d episode(s) → done", new_done)
    return new_done


# ──────────────────────────────────────────────────────────────────────────────
# Status / printing
# ──────────────────────────────────────────────────────────────────────────────

def status_summary(reg: Optional[Dict[str, Any]] = None) -> str:
    reg = reg or load_registry()
    lines: List[str] = []
    today = date.today().isoformat()
    cands = reg.get("candidates", [])
    lines.append(f"primary_date     : {reg.get('primary_date')}")
    lines.append(f"list_finalized   : {reg.get('list_finalized')}")
    lines.append(f"last_sbe_check   : {reg.get('last_sbe_check')}")
    lines.append(f"total candidates : {len(cands)}")
    by_status = {"pending": 0, "queued": 0, "done": 0, "withdrawn": 0}
    for c in cands:
        status_counts = [ep.get("status", "pending") for ep in c.get("episodes", [])]
        if c.get("dossier_status") == "withdrawn":
            by_status["withdrawn"] += 1
        elif all(s == "done" for s in status_counts):
            by_status["done"] += 1
        elif any(s in ("queued", "done") for s in status_counts):
            by_status["queued"] += 1
        else:
            by_status["pending"] += 1
    lines.append(f"  by status      : {by_status}")
    lines.append("")
    lines.append("Schedule (next 14 days):")
    upcoming = [c for c in cands
                if c.get("scheduled_date")
                and c.get("scheduled_date") >= today
                and c.get("dossier_status") != "withdrawn"]
    upcoming.sort(key=lambda c: c["scheduled_date"])
    for c in upcoming[:14]:
        eps = c.get("episodes", [])
        n_done = sum(1 for e in eps if e.get("status") == "done")
        lines.append(
            f"  {c['scheduled_date']} · {c['name']:30} · {c.get('office','')} "
            f"({n_done}/{len(eps)} eps done)"
        )
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _slug(text: str) -> str:
    import re
    s = re.sub(r"[^\w\s-]", "", (text or "").lower()).strip()
    s = re.sub(r"[-\s]+", "-", s)
    return s or "x"


def _load_recent_avoid_list(db_path: Path, limit: int = 30) -> List[str]:
    import sqlite3
    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        row = con.execute(
            "select avoid_list from weekly_themes order by week_end desc limit 1"
        ).fetchone()
        con.close()
    except Exception:
        return []
    if not row or not row["avoid_list"]:
        return []
    raw = row["avoid_list"]
    try:
        data = json.loads(raw) if raw.strip().startswith("[") else None
    except Exception:
        data = None
    if isinstance(data, list):
        return [str(x) for x in data[:limit]]
    out: List[str] = []
    for line in raw.splitlines():
        line = line.strip("-• ").strip()
        if line:
            out.append(line)
        if len(out) >= limit:
            break
    return out


def _load_recent_listener_notes(db_path: Path, target_date: date,
                                  days: int = 10) -> List[Dict[str, Any]]:
    import sqlite3
    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        start = (target_date - timedelta(days=days)).isoformat()
        rows = list(con.execute(
            "select report_date, content from daily_notes "
            "where report_date >= ? and report_date < ? "
            "order by report_date desc",
            (start, target_date.isoformat())))
        con.close()
    except Exception:
        return []
    return [{"date": r["report_date"], "note": r["content"]} for r in rows]


def _load_listener_focus(db_path: Path) -> List[str]:
    import sqlite3
    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        row = con.execute(
            "select underserved_topics, open_questions from weekly_themes "
            "order by week_end desc limit 1"
        ).fetchone()
        con.close()
    except Exception:
        return []
    if not row:
        return []
    out: List[str] = []
    for f in ("underserved_topics", "open_questions"):
        raw = row[f] or ""
        try:
            data = json.loads(raw) if raw.strip().startswith("[") else None
        except Exception:
            data = None
        if isinstance(data, list):
            out.extend(str(x) for x in data)
        else:
            for line in raw.splitlines():
                line = line.strip("-• ").strip()
                if line:
                    out.append(line)
    seen, unique = set(), []
    for s in out:
        k = s.lower()
        if k not in seen:
            seen.add(k); unique.append(s)
    return unique[:12]


def _load_events_for_name(db_path: Path, name: str,
                            limit: int = 30) -> List[Dict[str, Any]]:
    import sqlite3
    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        rows = list(con.execute("""
            select e.id, e.title, e.summary, e.url, e.event_date, e.level,
                   pe.role, pe.stance
              from politician_events pe
              join events e on e.id = pe.event_id
              join politicians p on p.id = pe.politician_id
             where lower(p.name) = lower(?)
             order by e.event_date desc limit ?
        """, (name, limit)))
        con.close()
    except Exception:
        return []
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────────────────────────────────────
# Dossier scout — fast richness recon for every candidate
# ──────────────────────────────────────────────────────────────────────────────

# Where the scout JSON results land (one file per candidate)
SCOUT_DIR = _PROJECT_ROOT / "data" / "candidate_scouts"


def queue_scout_all(reg: Optional[Dict[str, Any]] = None,
                     *, force: bool = False) -> List[str]:
    """
    Queue a `dossier_scout` brief for every active registry candidate that
    doesn't already have a scout result on disk. Returns names queued.

    Run once when the registry is first seeded, then again any time the
    monitor brief adds new candidates.
    """
    from scanner.cowork_bridge import build_dossier_scout_brief, write_brief
    SCOUT_DIR.mkdir(parents=True, exist_ok=True)

    reg = reg or load_registry()
    queued: List[str] = []
    for c in reg.get("candidates", []):
        if c.get("dossier_status") == "withdrawn":
            continue
        slug = _slug(c["name"])
        out = SCOUT_DIR / f"{slug}.json"
        # Skip if already scouted, unless forced or candidate has no
        # richness_score yet
        if not force and out.exists() and c.get("richness_score") is not None:
            continue
        brief = build_dossier_scout_brief(
            candidate_name=c["name"],
            office=c.get("office", ""),
            party=c.get("party", ""),
            district=c.get("district", ""),
            output_file=out,
        )
        write_brief(brief)
        c["scout_status"] = "queued"
        queued.append(c["name"])
    if queued:
        save_registry(reg)
    return queued


def apply_scout_results(reg: Optional[Dict[str, Any]] = None) -> int:
    """
    Read every `data/candidate_scouts/<slug>.json` Cowork has written and
    copy the relevant fields into the registry. Returns count updated.
    """
    if not SCOUT_DIR.exists():
        return 0
    reg = reg or load_registry()
    by_name = {c["name"].lower(): c for c in reg.get("candidates", [])}
    n = 0
    for p in SCOUT_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        nm = (data.get("candidate_name") or "").strip()
        if not nm:
            continue
        c = by_name.get(nm.lower())
        if not c:
            continue
        c["richness_score"] = float(data.get("richness_score") or 0.0)
        c["estimated_research_hours"] = float(data.get("estimated_research_hours") or 99)
        c["estimated_age"] = data.get("estimated_age")
        if c["estimated_age"] is not None:
            c["lookback_years"] = max(10, int(c["estimated_age"]) - 20)
        c["missing_critical_sections"] = data.get("missing_critical_sections", [])
        c["scout_status"] = "complete"
        c["scout_notes"] = data.get("notes", "")[:500]
        n += 1
    if n:
        save_registry(reg)
    return n


def reschedule_by_readiness(reg: Optional[Dict[str, Any]] = None,
                              *, schedule_start: Optional[date] = None,
                              ) -> int:
    """
    Reorder air dates within each tier so that:
      • Rich, ready candidates (high richness_score) air first.
      • Thin candidates air later, giving more nights for deep research.
      • Within a tier, candidates from the same race stay clustered so the
        listener compares them in successive days.

    Returns count of candidates whose date changed.
    """
    reg = reg or load_registry()
    cands = [c for c in reg.get("candidates", [])
             if c.get("dossier_status") != "withdrawn"]
    if not cands:
        return 0

    schedule_start = schedule_start or _next_unscheduled_day(reg)

    # Within tier, sort by (-richness, contest cluster, name).
    # Cluster key: the office string — keeps candidates from same race together.
    def sort_key(c):
        tier = int(c.get("tier") or 9)
        rich = float(c.get("richness_score") or 0.0)
        # 1 - rich so higher richness comes first
        return (tier, c.get("office", ""), -rich, c.get("name", ""))

    cands.sort(key=sort_key)

    changes = 0
    d = schedule_start
    for c in cands:
        new_iso = d.isoformat()
        if c.get("scheduled_date") != new_iso:
            c["scheduled_date"] = new_iso
            changes += 1
        d += timedelta(days=1)
    if changes:
        # Persist by index in original list (we sorted a copy, so put back
        # the old order with new dates)
        save_registry(reg)
    return changes


def _next_unscheduled_day(reg: Dict[str, Any]) -> date:
    """First day on/after today that isn't already scheduled."""
    today = date.today()
    used = set()
    for c in reg.get("candidates", []):
        if c.get("scheduled_date"):
            used.add(c["scheduled_date"])
    d = today
    while d.isoformat() in used:
        d += timedelta(days=1)
    return d


# ──────────────────────────────────────────────────────────────────────────────
# Listener-note override — user can write "switch X to office content" in
# their daily_note and the next morning's queue picks it up.
# ──────────────────────────────────────────────────────────────────────────────

def _listener_override_for(candidate_name: str, db_path: Path,
                             today: date, days: int = 3) -> Optional[str]:
    """
    Scan recent daily_notes for an instruction to switch THIS candidate's day
    to a different content type. Recognized triggers (case-insensitive):
      • "switch <candidate> to office" / "to position" / "to seat content"
      • "<candidate> has nothing — talk about the seat"
      • "<candidate> dossier is thin — do an office primer"
    Returns "office_primer" or None.
    """
    import sqlite3, re
    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        start = (today - timedelta(days=days)).isoformat()
        rows = list(con.execute(
            "select content from daily_notes where report_date >= ? "
            "order by report_date desc", (start,)))
        con.close()
    except Exception:
        return None

    nm = candidate_name.lower()
    keywords = ["office", "position", "seat", "primer"]
    for content, in [(r["content"],) for r in rows]:
        text = (content or "").lower()
        if nm not in text:
            continue
        # Look for a switch directive within ~80 chars of the candidate name
        idx = text.find(nm)
        window = text[max(0, idx-50): idx + 200]
        if any(re.search(rf"\b{kw}\b", window) for kw in keywords) and \
           any(verb in window for verb in ("switch", "swap", "replace", "instead", "skip")):
            return "office_primer"
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Updated queueing — handles thin-content fallback for ep4
# ──────────────────────────────────────────────────────────────────────────────

# Below this richness score we automatically swap Episode 4 (This Race) for
# an office_primer episode. The listener gets context about the seat instead
# of 30 minutes of "we don't have a public record of X."
_RICHNESS_THIN_THRESHOLD = 0.35


def queue_today_series_v2(target_date: Optional[date] = None,
                            podcasts_dir: Optional[Path] = None,
                            dossier_dir: Optional[Path] = None,
                            db_path: Optional[Path] = None,
                            ) -> Dict[str, Any]:
    """
    Updated queue path that also:
      • Honors a listener-note override (switches a candidate's whole day to
        an office_primer if the user said so).
      • For thin candidates (richness_score < threshold), keeps Eps 1-3 as
        biography-best-effort but swaps Ep 4 to an office_primer.
      • For very thin candidates (richness_score < 0.15) or those flagged
        with `force_office_primer`, swaps ALL 4 episodes to office_primer.
    """
    from scanner.cowork_bridge import (
        build_dossier_brief, build_series_episode_brief,
        build_office_primer_brief, write_brief,
    )

    target_date = target_date or date.today()
    reg = load_registry()
    cand = candidate_for_date(target_date, reg)
    if not cand:
        log.info("series: no candidate scheduled for %s", target_date)
        return {"status": "no_candidate", "date": target_date.isoformat()}

    # Resolve config
    try:
        from config import Config as _Cfg
        podcasts_dir = podcasts_dir or _Cfg.PODCASTS_DIR
        dossier_dir = dossier_dir or _Cfg.CANDIDATE_DOSSIER_DIR
        db_path = db_path or _Cfg.DB_PATH
        locale = ", ".join(p for p in [_Cfg.CITY, _Cfg.COUNTY, _Cfg.STATE] if p) or "the local area"
        districts_profile = (_Cfg.districts_profile()
                              if hasattr(_Cfg, "districts_profile") else "")
    except Exception:
        podcasts_dir = podcasts_dir or _PROJECT_ROOT / "podcasts"
        dossier_dir = dossier_dir or _PROJECT_ROOT / "data" / "candidate_dossiers"
        db_path = db_path or _PROJECT_ROOT / "data" / "politics.db"
        locale = "Rockville, Montgomery County, Maryland"
        districts_profile = ""

    podcasts_dir = Path(podcasts_dir); podcasts_dir.mkdir(parents=True, exist_ok=True)
    dossier_dir = Path(dossier_dir); dossier_dir.mkdir(parents=True, exist_ok=True)

    name = cand["name"]
    richness = cand.get("richness_score")
    override = _listener_override_for(name, Path(db_path), target_date)
    force_full_primer = (
        cand.get("force_office_primer") is True
        or override == "office_primer"
        or (richness is not None and richness < 0.15)
    )
    swap_ep4_only = (
        not force_full_primer and richness is not None
        and richness < _RICHNESS_THIN_THRESHOLD
    )

    log.info("series v2: %s · richness=%s · override=%s · "
             "full_primer=%s · ep4_swap=%s",
             name, richness, override, force_full_primer, swap_ep4_only)

    # Listener calibration shared across all 4 episodes
    try:
        from scanner.podcast import _compute_length_calibration
        cal = _compute_length_calibration(podcasts_dir, target_date)
    except Exception:
        cal = None
    listener_notes = _load_recent_listener_notes(Path(db_path), target_date)
    avoid_list = _load_recent_avoid_list(Path(db_path))

    candidates_in_race = [
        c["name"] for c in reg.get("candidates", [])
        if c.get("office") == cand.get("office")
        and c.get("dossier_status") != "withdrawn"
    ]

    queued_eps: List[Dict[str, Any]] = []
    slug = _slug(name)

    # ── If forcing full office_primer day, write 4 office_primer briefs ─────
    if force_full_primer:
        why = (override and f"listener daily-note override said switch {name} to office content"
               ) or (richness is not None and f"scout returned richness={richness:.2f} — too thin for biography"
               ) or "force_office_primer flag set in registry"
        for ep_num in (1, 2, 3, 4):
            out_file = (podcasts_dir
                        / f"podcast_{target_date.isoformat()}_office_{slug}_ep{ep_num}.txt")
            brief = build_office_primer_brief(
                office=cand.get("office", ""),
                district=cand.get("district", ""),
                candidates_in_race=candidates_in_race,
                listener_locale=locale,
                target_date=target_date.isoformat(),
                ep_num=ep_num,
                output_file=out_file,
                avoid_list=avoid_list,
                listener_notes=listener_notes,
                length_calibration=cal,
                why_this_episode=why,
            )
            write_brief(brief)
            queued_eps.append({"num": ep_num, "type": "office_primer",
                                 "path": str(out_file)})
            for em in cand.get("episodes", []):
                if int(em.get("num", 0)) == ep_num:
                    em["status"] = "queued"
                    em["mode"] = "office_primer"
                    em["script_path"] = str(out_file)
        save_registry(reg)
        return {
            "status": "queued",
            "date": target_date.isoformat(),
            "candidate": name,
            "office": cand.get("office", ""),
            "mode": "full_office_primer",
            "reason": why,
            "episodes_queued": queued_eps,
        }

    # ── Normal series path: dossier + 4 episodes (with maybe ep4 swapped) ──
    dossier_path = (Path(cand.get("dossier_path"))
                     if cand.get("dossier_path") else dossier_dir / f"{slug}.md")
    if not dossier_path.is_absolute():
        dossier_path = _PROJECT_ROOT / dossier_path

    needs_dossier = (
        cand.get("dossier_status") in (None, "pending", "exists_but_thin")
        or not dossier_path.exists()
        or (dossier_path.exists() and dossier_path.stat().st_size < 4000)
    )
    if needs_dossier:
        focus = _load_listener_focus(Path(db_path)) or [
            "sanctuary policy", "school funding", "property tax",
            "policing", "K-12 curriculum", "transit", "housing zoning",
        ]
        known_events = _load_events_for_name(Path(db_path), name)
        dossier_brief = build_dossier_brief(
            candidate_name=name,
            office=cand.get("office", ""),
            party=cand.get("party", ""),
            district=cand.get("district", ""),
            known_events=known_events,
            listener_focus=focus,
            output_dir=Path(dossier_dir),
            today=target_date.isoformat(),
        )
        write_brief(dossier_brief)
        cand["dossier_status"] = "queued"

    for ep_num in (1, 2, 3, 4):
        out_file = (podcasts_dir
                    / f"podcast_{target_date.isoformat()}_series_{slug}_ep{ep_num}.txt")
        if ep_num == 4 and swap_ep4_only:
            why = f"scout flagged candidate as thin (richness={richness:.2f}); ep4 covers the seat itself instead"
            brief = build_office_primer_brief(
                office=cand.get("office", ""),
                district=cand.get("district", ""),
                candidates_in_race=candidates_in_race,
                listener_locale=locale,
                target_date=target_date.isoformat(),
                ep_num=ep_num,
                output_file=out_file,
                avoid_list=avoid_list,
                listener_notes=listener_notes,
                length_calibration=cal,
                why_this_episode=why,
            )
            mode = "office_primer"
        else:
            brief = build_series_episode_brief(
                candidate_name=name,
                office=cand.get("office", ""),
                party=cand.get("party", ""),
                district=cand.get("district", ""),
                target_date=target_date.isoformat(),
                ep_num=ep_num,
                dossier_path=dossier_path,
                avoid_list=avoid_list,
                listener_notes=listener_notes,
                locale=locale,
                districts_profile=districts_profile,
                output_file=out_file,
                length_calibration=cal,
            )
            mode = "biography"
        write_brief(brief)
        queued_eps.append({"num": ep_num, "type": mode, "path": str(out_file)})
        for em in cand.get("episodes", []):
            if int(em.get("num", 0)) == ep_num:
                em["status"] = "queued"
                em["mode"] = mode
                em["script_path"] = str(out_file)

    save_registry(reg)
    return {
        "status": "queued",
        "date": target_date.isoformat(),
        "candidate": name,
        "office": cand.get("office", ""),
        "mode": "ep4_swap" if swap_ep4_only else "biography",
        "richness_score": richness,
        "dossier_queued": needs_dossier,
        "episodes_queued": queued_eps,
    }


# Make the existing entry point use the new logic so `cmd_publish` automatically
# benefits from scout-aware routing + thin-content fallbacks. The original
# `queue_today_series` body above is preserved for any caller that explicitly
# wants the "always biography" mode; the v2 wrapper is the default.
_queue_today_series_legacy = queue_today_series
queue_today_series = queue_today_series_v2
