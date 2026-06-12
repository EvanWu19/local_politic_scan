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
                        reg: Optional[Dict[str, Any]] = None,
                        *, skip_processed: bool = False
                        ) -> Optional[Dict[str, Any]]:
    """Return the registry entry scheduled for `target_date`, or None.

    With ``skip_processed=True`` (multi-per-day mode, 2026-06-11), entries
    whose episodes are ALL already queued/done are skipped, so repeated
    calls walk through every candidate sharing the same scheduled_date.
    """
    reg = reg or load_registry()
    iso = target_date.isoformat()
    for c in reg.get("candidates", []):
        if c.get("scheduled_date") == iso and c.get("dossier_status") != "withdrawn":
            if skip_processed:
                eps = c.get("episodes", [])
                if eps and all(e.get("status") in ("queued", "done") for e in eps):
                    continue
            return c
    return None


def candidates_for_date(target_date: date,
                         reg: Optional[Dict[str, Any]] = None
                         ) -> List[Dict[str, Any]]:
    """ALL registry entries scheduled for `target_date` (2026-06-12).

    The pre-primary crunch schedules up to 5 candidates per day; callers
    that present "today's candidate" to the listener (reporter spotlight,
    status pages) should use this instead of `candidate_for_date`, which
    returns only the first match.
    """
    reg = reg or load_registry()
    iso = target_date.isoformat()
    return [c for c in reg.get("candidates", [])
            if c.get("scheduled_date") == iso
            and c.get("dossier_status") != "withdrawn"]


def _next_upcoming_candidate(from_date: date,
                              reg: Optional[Dict[str, Any]] = None
                              ) -> Optional[Dict[str, Any]]:
    """Return the candidate whose `scheduled_date` is the soonest after
    `from_date` (strictly greater), skipping withdrawn entries.

    Used as the no-news fallback in `queue_today_series` and as the guard
    signal in `podcast.py`: if any future candidate exists, news-format
    episodes (`author_episode` briefs) must not be generated. This makes
    the registry's forward schedule load-bearing — running out of future
    candidates is an explicit failure mode that surfaces loudly rather
    than silently degrading to news.
    """
    reg = reg or load_registry()
    iso = from_date.isoformat()
    future = [
        c for c in reg.get("candidates", [])
        if c.get("scheduled_date") and c.get("scheduled_date") > iso
           and c.get("dossier_status") != "withdrawn"
    ]
    if not future:
        return None
    future.sort(key=lambda c: c["scheduled_date"])
    return future[0]


def _candidate_has_complete_series(candidate_name: str, podcasts_dir) -> bool:
    """Return True if all 4 series episodes exist as MP3s for this candidate.

    Used to filter out already-covered candidates from the deepdive queue so
    the nightly cowork-queue step never re-queues a Friedson-style deepdive
    for a candidate whose series is already fully recorded.
    """
    from pathlib import Path
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", candidate_name.lower()).strip("-")
    podcasts_dir = Path(podcasts_dir)
    return any(podcasts_dir.glob(f"podcast_*_series_{slug}_ep4.mp3"))


def _airing_dates_for_candidate(candidate_name: str, podcasts_dir) -> List[str]:
    """Return ISO dates on which this candidate has a *complete* (all 4
    episodes) series airing on disk. Empty list = not fully aired anywhere.

    Shared by the entry-block cascade in ``queue_today_series`` and the
    existing in-flow dedup guard so both code paths use one definition of
    "already aired in full" — file-size-filtered to avoid counting empty
    or .partial leftovers as real airings.
    """
    from pathlib import Path
    import re as _re
    if not candidate_name:
        return []
    slug = _slug(candidate_name)
    podcasts_dir = Path(podcasts_dir)
    airings: Dict[str, set] = {}
    for p in podcasts_dir.glob(f"podcast_*_series_{slug}_ep*.mp3"):
        m = _re.search(r"podcast_(\d{4}-\d{2}-\d{2})_series_.+_ep(\d+)\.mp3$", p.name)
        if not m:
            continue
        try:
            if p.stat().st_size < 1024:
                continue
        except OSError:
            continue
        airings.setdefault(m.group(1), set()).add(int(m.group(2)))
    return sorted(d for d, eps in airings.items() if eps >= {1, 2, 3, 4})


def _next_unaired_candidate(from_date: date,
                              reg: Optional[Dict[str, Any]] = None,
                              podcasts_dir=None
                              ) -> Optional[Dict[str, Any]]:
    """Walk the future schedule and return the first candidate whose
    ``scheduled_date > from_date`` AND who does NOT already have a
    complete series on disk.

    Bug fix (2026-05-25): the old `_next_upcoming_candidate` was
    single-shot. If the soonest future candidate had already aired (e.g.
    Vaughn Stewart got pulled forward to May 20 even though his registry
    slot was July 22), the dedup-guard refused the airing and the function
    silently returned ``already_aired`` — producing zero MP3s for every
    day until the registry was edited. This walks past already-aired
    entries instead.
    """
    if podcasts_dir is None:
        try:
            from config import Config as _Cfg
            podcasts_dir = _Cfg.PODCASTS_DIR
        except Exception:
            from pathlib import Path
            podcasts_dir = Path("podcasts")
    reg = reg or load_registry()
    iso = from_date.isoformat()
    future = [
        c for c in reg.get("candidates", [])
        if c.get("scheduled_date") and c.get("scheduled_date") > iso
           and c.get("dossier_status") != "withdrawn"
    ]
    future.sort(key=lambda c: c["scheduled_date"])
    for c in future:
        if _airing_dates_for_candidate(c.get("name", ""), podcasts_dir):
            log.info(
                "series: skipping %s (scheduled %s) in forward-lookup — "
                "already aired in full",
                c.get("name"), c.get("scheduled_date"),
            )
            continue
        return c
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Pool-supply tracking + pool-exhausted closing brief
# ──────────────────────────────────────────────────────────────────────────────

# Registry-supply floor. When fewer than this many *unstarted* upcoming
# candidates remain, the supply-warning file is written and the operator
# gets a banner on the homepage.
SUPPLY_LOW_THRESHOLD = 7


def _count_existing_mp3s(podcasts_dir: Path, slug: str) -> int:
    """Return the number of `podcast_*_series_<slug>_epN.mp3` files on
    disk for this candidate (across all dates). Used to know whether a
    candidate has been started/completed."""
    if not podcasts_dir or not Path(podcasts_dir).exists():
        return 0
    return sum(
        1 for p in Path(podcasts_dir).glob(f"podcast_*_series_{slug}_ep*.mp3")
        if p.stat().st_size > 1024
    )


def check_candidate_supply(podcasts_dir: Path,
                            registry: Optional[Dict[str, Any]] = None,
                            *,
                            warning_path: Optional[Path] = None) -> Dict[str, Any]:
    """Count upcoming registry entries that haven't been started yet
    (zero MP3s on disk for them). If that count drops below
    ``SUPPLY_LOW_THRESHOLD``, persist a warning file the homepage can
    read; otherwise remove any stale warning.

    Returns a stats dict:
        {unstarted_count, next_unstarted: {name, scheduled_date} | None,
         threshold, warning_active, ...}
    """
    registry = registry or load_registry()
    today = date.today().isoformat()
    podcasts_dir = Path(podcasts_dir) if podcasts_dir else None

    if warning_path is None:
        try:
            from config import Config as _Cfg
            base = getattr(_Cfg, "BASE_DIR", Path("."))
        except Exception:
            base = Path(".")
        warning_path = Path(base) / "data" / "supply_warning.json"

    unstarted: List[Dict[str, Any]] = []
    for c in registry.get("candidates", []):
        sd = c.get("scheduled_date")
        if not sd or sd <= today:
            continue
        if c.get("dossier_status") == "withdrawn":
            continue
        slug = _slug(c.get("name", ""))
        if _count_existing_mp3s(podcasts_dir, slug) == 0:
            unstarted.append({
                "name": c.get("name", ""),
                "scheduled_date": sd,
                "slug": slug,
            })

    unstarted.sort(key=lambda e: e["scheduled_date"])
    next_unstarted = unstarted[0] if unstarted else None
    warning_active = len(unstarted) < SUPPLY_LOW_THRESHOLD

    payload = {
        "unstarted_count": len(unstarted),
        "threshold": SUPPLY_LOW_THRESHOLD,
        "next_unstarted": next_unstarted,
        "warning_active": warning_active,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }

    try:
        if warning_active:
            warning_path.parent.mkdir(parents=True, exist_ok=True)
            warning_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        else:
            # Above threshold — remove any stale warning file.
            if warning_path.exists():
                warning_path.unlink()
    except Exception as e:
        log.debug("supply-warning persistence skipped: %s", e)

    return payload


def _queue_closing_brief(target_date: date) -> Optional[Path]:
    """Write a ``podcast_closing_<date>.json`` brief into the Cowork inbox
    so the drain can produce a short (~90s) closing episode acknowledging
    that the registry has been fully covered. Returns the brief path, or
    None if writing failed."""
    try:
        from config import Config as _Cfg
        base = getattr(_Cfg, "BASE_DIR", Path("."))
    except Exception:
        base = Path(".")
    inbox = Path(base) / "cowork_inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    date_iso = target_date.isoformat()
    brief_id = f"podcast_closing_{date_iso}"
    path = inbox / f"{brief_id}.json"

    output_file = (
        Path(base) / "podcasts" / f"podcast_{date_iso}_closing.txt"
    )
    payload = {
        "brief_id": brief_id,
        "type": "podcast_closing",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "output_file": str(output_file),
        "text": "All candidates in the registry have been covered. "
                "The series is complete pending new additions.",
        "instructions": (
            "Write a short closing episode in the ALEX/JORDAN format. "
            "Total target: ~250 words / ~90 seconds of audio. Tone: "
            "wrap-up; the series has reached the end of the current "
            "registry. Acknowledge the listener by name (Rockville voter "
            "before the June 23, 2026 primary). Mention that if any "
            "newly-filed candidate appears in the registry, the show will "
            "resume the per-candidate series format. Do not invent new "
            "candidate biography. No URLs read aloud."
        ),
    }
    try:
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.warning("series: queued podcast_closing brief at %s", path)
        return path
    except Exception as e:
        log.error("series: failed to queue closing brief: %s", e)
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
        # PERMANENT RULE (2026-05-19): never silently fall through to the
        # retired news format. If today has no scheduled candidate, find
        # the next future candidate and air their series early. Only if the
        # entire forward schedule is empty do we surface an error.
        cand = _next_upcoming_candidate(target_date, reg)
        if not cand:
            log.error(
                "series: no candidate scheduled for %s and no future "
                "candidates found — refusing to generate news fallback",
                target_date,
            )
            return {
                "status": "no_candidate_and_no_fallback",
                "date": target_date.isoformat(),
            }
        log.warning(
            "series: no candidate scheduled for %s — using next scheduled: "
            "%s (originally %s)",
            target_date, cand["name"], cand.get("scheduled_date"),
        )

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
            select e.id, e.title, e.summary, e.source_url, e.date, e.level,
                   pe.role, pe.stance
              from politician_events pe
              join events e on e.id = pe.event_id
              join politicians p on p.id = pe.politician_id
             where lower(p.name) = lower(?)
             order by e.date desc limit ?
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
    # skip_processed=True (2026-06-11): with the 3-per-day pre-primary
    # schedule, several candidates share one scheduled_date. Skipping
    # already-queued entries lets queue_today_series_multi walk the day's
    # whole slate instead of re-picking the first candidate forever.
    cand = candidate_for_date(target_date, reg, skip_processed=True)

    # CASCADE FIX (2026-05-25): if today's scheduled candidate has already
    # aired in full (Vaughn Stewart May 20 → registry still says July 22),
    # demote to forward-lookup so we don't return ``already_aired`` and
    # produce nothing. The legacy in-flow dedup-guard below remains as a
    # belt-and-suspenders check; this earlier cascade just prevents the
    # silent-fail mode.
    from config import Config as _Cfg
    _pd = _Cfg.PODCASTS_DIR
    if cand and _airing_dates_for_candidate(cand.get("name", ""), _pd):
        log.warning(
            "series: today's scheduled %s already aired — cascading to "
            "next unaired future candidate",
            cand.get("name"),
        )
        cand = None

    if not cand:
        # PERMANENT RULE (2026-05-19): never silently fall through to the
        # retired news format. If today has no scheduled candidate, find
        # the next future candidate and air their series early. The
        # forward-lookup is now ``_next_unaired_candidate`` which walks
        # past entries that already have a complete on-disk series — fix
        # for the May 22–25 silent-fail (only Stewart was future, but he
        # already aired May 20, so the day produced nothing).
        cand = _next_unaired_candidate(target_date, reg, _pd)
        if not cand:
            # POOL-EXHAUSTED HANDLING (2026-05-20): registry is empty of
            # future candidates (or every remaining future entry has
            # already aired). Don't crash silently or just log — queue a
            # special `podcast_closing` brief so the drain produces a
            # short ~90-second closing episode acknowledging that the
            # series has covered everyone currently on file. Future
            # additions to the registry will resume normal series queueing.
            _queue_closing_brief(target_date)
            log.error(
                "series: no candidate scheduled for %s and no future "
                "unaired candidates — queued podcast_closing brief instead",
                target_date,
            )
            return {
                "status": "no_candidate_and_no_fallback",
                "date": target_date.isoformat(),
                "closing_brief_queued": True,
            }
        log.warning(
            "series: no candidate scheduled for %s — using next unaired: "
            "%s (originally %s)",
            target_date, cand["name"], cand.get("scheduled_date"),
        )

    # SUPPLY WARNING (2026-05-20): every queue pass, refresh the
    # supply-warning state so the operator gets a visible signal when the
    # forward registry runs thin. Cheap and idempotent.
    try:
        from config import Config as _Cfg
        check_candidate_supply(_Cfg.PODCASTS_DIR, reg)
    except Exception as e:
        log.debug("series: supply check skipped (non-fatal): %s", e)

    # DEDUP GUARD (2026-05-20): if this candidate already has all 4
    # episode MP3s on disk from a prior airing (any date), refuse to
    # generate duplicate content. Bug 3 case: Ben Kramer fully aired
    # May 13 but a stale registry entry queued him again for May 19.
    try:
        from config import Config as _Cfg
        from scanner.series import _slug
        slug = _slug(cand.get("name", ""))
        existing_mp3s = list(_Cfg.PODCASTS_DIR.glob(
            f"podcast_*_series_{slug}_ep*.mp3"
        ))
        # Group by date to count completed airings
        airings_by_date: Dict[str, set] = {}
        import re as _re
        for p in existing_mp3s:
            m = _re.search(r"podcast_(\d{4}-\d{2}-\d{2})_series_.+_ep(\d+)\.mp3$", p.name)
            if m and p.stat().st_size > 1024:
                airings_by_date.setdefault(m.group(1), set()).add(int(m.group(2)))
        completed_airings = [
            d for d, eps in airings_by_date.items() if eps >= {1, 2, 3, 4}
        ]
        if completed_airings:
            log.warning(
                "series: %s already aired in full on %s — refusing to queue "
                "duplicate series for %s. Update the registry to skip this "
                "candidate or assign someone new.",
                cand["name"], ", ".join(sorted(completed_airings)),
                target_date.isoformat(),
            )
            return {
                "status": "already_aired",
                "date": target_date.isoformat(),
                "candidate": cand["name"],
                "prior_airings": sorted(completed_airings),
            }
    except Exception as e:
        # Non-fatal — if the guard itself crashes we still try to queue
        log.debug("series: dedup-guard check failed (non-fatal): %s", e)

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


# ──────────────────────────────────────────────────────────────────────────────
# Multi-per-day queueing (2026-06-11 pre-primary crunch)
# ──────────────────────────────────────────────────────────────────────────────

_MAX_SERIES_PER_DAY = 6


def queue_today_series_multi(target_date: Optional[date] = None,
                               podcasts_dir: Optional[Path] = None,
                               dossier_dir: Optional[Path] = None,
                               db_path: Optional[Path] = None,
                               ) -> Dict[str, Any]:
    """Queue EVERY candidate scheduled for `target_date`, not just the first.

    Added 2026-06-11 after reconciling the registry against the scanned
    official ballot: 36 uncovered candidates remained with only 12 days to
    the Jun 23 primary, so the schedule now assigns 3 candidates per day.
    Each pass re-reads the registry; `candidate_for_date(skip_processed=
    True)` (inside v2) advances past candidates whose episodes were queued
    by an earlier pass. Iteration beyond the first pass is gated on another
    *same-date* unprocessed candidate existing, so the v2 forward-lookup
    fallback (which pulls future candidates early) can fire at most once.

    Returns a dict shaped like the v2 result for cmd_publish compatibility;
    when several candidates were queued, `candidate` is comma-joined and a
    `multi` list carries the per-candidate results.
    """
    target_date = target_date or date.today()
    iso = target_date.isoformat()
    results: List[Dict[str, Any]] = []
    for i in range(_MAX_SERIES_PER_DAY):
        if i > 0:
            nxt = candidate_for_date(target_date, load_registry(),
                                      skip_processed=True)
            if nxt is None:
                break
        res = queue_today_series_v2(target_date, podcasts_dir,
                                     dossier_dir, db_path)
        results.append(res)
        if res.get("status") != "queued":
            break
    if not results:
        return {"status": "no_candidate", "date": iso}
    out = dict(results[0])
    queued = [r for r in results if r.get("status") == "queued"]
    if len(queued) > 1:
        out["candidate"] = ", ".join(r.get("candidate", "?") for r in queued)
        out["office"] = "; ".join(r.get("office", "") for r in queued)
        # v2 episode entries are dicts ({num, type, path}); legacy entries
        # are bare ints. Aggregate on the episode number either way.
        out["episodes_queued"] = sorted(
            {n.get("num") if isinstance(n, dict) else n
             for r in queued for n in r.get("episodes_queued", [])})
        out["multi"] = [
            {"candidate": r.get("candidate"), "status": r.get("status"),
             "episodes_queued": r.get("episodes_queued")} for r in results
        ]
        log.info("series: multi-queue for %s — %d candidates: %s",
                 iso, len(queued), out["candidate"])
    return out


queue_today_series = queue_today_series_multi
