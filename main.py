"""
Local Politics Scanner — main entry point.

Usage:
  python main.py scan          Run a full scan now and generate today's report
  python main.py report        (Re-)generate report from already-scanned data
  python main.py politician    Look up a politician's recent activity
  python main.py setup         Set up daily Windows Task Scheduler job
  python main.py status        Show recent scan history

Run `python main.py --help` for full help.
"""
import argparse
import logging
import os
import sys
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

# Force UTF-8 output on Windows so Unicode symbols render correctly
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scanner")

# ── Project imports ────────────────────────────────────────────────────────────
from config import Config
from scanner.database import (
    initialize_db, seed_politicians, upsert_event, update_event_ai,
    link_politician_event, get_recent_events, get_politician_summary,
    start_scan_run, finish_scan_run, save_report, get_connection,
    save_podcast, list_podcasts,
)
from scanner.processor import process_batch, score_federal_relevance
from scanner.reporter import generate as gen_report, save_html_report
from scanner.server import run_server, get_tailscale_ip
from scanner.podcast import generate_podcast_episodes
from scanner.sources.federal import fetch_bills
from scanner.sources.state import fetch_state_bills, fetch_state_hearings
from scanner.sources.montgomery import (
    fetch_county_council, fetch_county_hearings,
    fetch_mcps_board, fetch_local_services,
)
from scanner.sources.news import fetch_rss_feeds
from scanner.sources.candidates import print_candidates_table

cfg = Config()


# ──────────────────────────────────────────────────────────────────────────────
# FETCH / PUBLISH / SCAN
#
# Pipeline order (daily rhythm):
#
#   MORNING (cmd_publish): PM → Analyst → Author → Editor → TTS
#       Uses data already in the DB — i.e. the previous night's fetch —
#       so that yesterday's audience note can influence today's episode.
#
#   EVENING (cmd_fetch):   Data Collector runs LAST
#       Fetches fresh material and primes tomorrow morning's publish.
#
#   cmd_scan is kept as a backward-compatible alias that does
#   fetch-then-publish in one shot (same-day data drives same-day episode).
#   Prefer fetch + publish on a schedule — see `main.py setup`.
# ──────────────────────────────────────────────────────────────────────────────

def cmd_fetch(args):
    """Collector phase: fetch all sources → AI enrich → save to DB.

    This is the Data Collector. It does NOT produce the report or podcast;
    run cmd_publish for that (typically the next morning).
    """
    print("\n── Local Politics Scanner — FETCH ──────────────────────")
    print(f"   Location : {cfg.CITY}, {cfg.COUNTY}, {cfg.STATE}")
    print(f"   Date     : {date.today()}")
    _districts = cfg.districts_profile()
    if _districts:
        print("   Districts:")
        for line in _districts.splitlines():
            print(f"   {line}")
    print("────────────────────────────────────────────────────────\n")

    # ── Init DB
    initialize_db(cfg.DB_PATH)
    seed_politicians(cfg.DB_PATH, cfg.KNOWN_POLITICIANS)
    run_id = start_scan_run(cfg.DB_PATH)

    all_raw: list = []
    errors: list = []

    # ── Fetch all sources
    print("Fetching sources…")

    try:
        print("  [1/6] Federal bills (Congress.gov)…", end=" ", flush=True)
        federal = fetch_bills(
            cfg.CONGRESS_API_KEY, cfg.FEDERAL_KEYWORDS,
            days_back=cfg.SCAN_DAYS_BACK, max_per_keyword=5
        )
        # Pre-filter: only keep federal items that match keywords
        federal = [ev for ev in federal
                   if score_federal_relevance(ev, cfg.FEDERAL_KEYWORDS) > 0]
        print(f"✓ {len(federal)} items")
        all_raw.extend(federal)
    except Exception as e:
        errors.append(f"federal: {e}")
        print(f"✗ {e}")

    try:
        print(f"  [2/6] {cfg.STATE or 'State'} bills (OpenStates)…", end=" ", flush=True)
        state_bills = fetch_state_bills(cfg.OPENSTATES_API_KEY,
                                        days_back=cfg.SCAN_DAYS_BACK,
                                        max_items=cfg.MAX_ITEMS_PER_SOURCE)
        print(f"✓ {len(state_bills)} items")
        all_raw.extend(state_bills)
    except Exception as e:
        errors.append(f"state: {e}")
        print(f"✗ {e}")

    try:
        print("  [3/6] Montgomery County council + hearings…", end=" ", flush=True)
        county = fetch_county_council(cfg.MAX_ITEMS_PER_SOURCE)
        county += fetch_county_hearings(10)
        print(f"✓ {len(county)} items")
        all_raw.extend(county)
    except Exception as e:
        errors.append(f"county: {e}")
        print(f"✗ {e}")

    try:
        print("  [4/6] MCPS school board…", end=" ", flush=True)
        mcps = fetch_mcps_board(cfg.MAX_ITEMS_PER_SOURCE)
        print(f"✓ {len(mcps)} items")
        all_raw.extend(mcps)
    except Exception as e:
        errors.append(f"mcps: {e}")
        print(f"✗ {e}")

    try:
        print("  [5/6] Local services (police/fire/health)…", end=" ", flush=True)
        local = fetch_local_services(cfg.MAX_ITEMS_PER_SOURCE // 2)
        print(f"✓ {len(local)} items")
        all_raw.extend(local)
    except Exception as e:
        errors.append(f"local_services: {e}")
        print(f"✗ {e}")

    try:
        print("  [6/6] News RSS feeds…", end=" ", flush=True)
        news = fetch_rss_feeds(cfg.NEWS_FEEDS, days_back=cfg.SCAN_DAYS_BACK,
                                max_per_feed=cfg.MAX_ITEMS_PER_SOURCE)
        print(f"✓ {len(news)} items")
        all_raw.extend(news)
    except Exception as e:
        errors.append(f"news_rss: {e}")
        print(f"✗ {e}")

    total_found = len(all_raw)
    print(f"\nTotal fetched: {total_found} items\n")

    # ── AI enrichment
    if cfg.ANTHROPIC_API_KEY:
        print(f"AI enrichment via Claude ({len(all_raw)} items in batches of 8)…")
        all_raw = process_batch(cfg.ANTHROPIC_API_KEY, all_raw)
        print("  Done.\n")
    else:
        print("⚠️  No ANTHROPIC_API_KEY — skipping AI enrichment.\n"
              "   Add it to your .env file for summaries and relevance scoring.\n")

    # ── Save to DB
    print("Saving to database…")
    new_count = 0
    for ev in all_raw:
        event_id = upsert_event(cfg.DB_PATH, ev)
        if event_id is None:
            continue
        new_count += 1

        # Update AI fields if present
        if ev.get("summary") or ev.get("relevance_score"):
            update_event_ai(
                cfg.DB_PATH, event_id,
                ev.get("summary", ""),
                ev.get("relevance_score", 0),
                ev.get("categories", []),
            )

        # Link politicians extracted by AI
        for pol in ev.get("_politicians", []):
            name = pol.get("name", "")
            if name:
                link_politician_event(
                    cfg.DB_PATH, name, event_id,
                    role=pol.get("role", "mentioned"),
                    stance=pol.get("stance", "unknown"),
                )

        # Also link sponsors from bills
        for sponsor in ev.get("sponsors", []):
            if sponsor:
                link_politician_event(
                    cfg.DB_PATH, sponsor, event_id,
                    role="sponsor", stance="support",
                )

    print(f"  Saved {new_count} new events to DB\n")
    finish_scan_run(cfg.DB_PATH, run_id, total_found, new_count,
                    status="ok", error="; ".join(errors))

    # ── Candidate discovery (refresh weekly so the Author has current ballot info)
    if cfg.ANTHROPIC_API_KEY and _should_refresh_candidates(cfg.DB_PATH):
        try:
            print("🗳️   Candidate discovery (weekly refresh)…")
            from scanner.sources.candidate_discover import discover_all
            runs = discover_all(
                db_path=cfg.DB_PATH,
                anthropic_key=cfg.ANTHROPIC_API_KEY,
                ballot_year=date.today().year,
            )
            saved = sum(r.get("candidates_saved", 0) for r in runs)
            print(f"   ✓ Refreshed {len(runs)} contests, {saved} candidate rows touched\n")
        except Exception as e:
            log.warning("Candidate discovery failed (non-fatal): %s", e)
            print(f"   ⚠️  Discovery failed: {e}\n")

    print("✅  Fetch complete. (Run `python main.py publish` to generate "
          "today's report + podcast.)\n")


def _should_refresh_candidates(db_path: Path, max_age_days: int = 7) -> bool:
    """True if we haven't touched any ai_discovery candidate row in N days."""
    from scanner.database import get_connection
    try:
        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT MAX(last_updated) AS last FROM politicians "
                "WHERE discovered_via = 'ai_discovery'"
            ).fetchone()
    except Exception:
        return True
    last = (row["last"] if row else None) or ""
    if not last:
        return True
    try:
        last_d = datetime.strptime(last, "%Y-%m-%d").date()
    except Exception:
        return True
    return (date.today() - last_d).days >= max_age_days


def cmd_publish(args):
    """Publish phase: PM rollup → Analyst → report → podcast.

    Uses whatever is already in the DB. Designed to run in the morning
    off the PREVIOUS night's fetch so that yesterday's audience notes
    can influence today's podcast.
    """
    print("\n── Local Politics Scanner — PUBLISH ────────────────────")
    print(f"   Date     : {date.today()}")
    print("────────────────────────────────────────────────────────\n")

    initialize_db(cfg.DB_PATH)

    # ── PM rollup (non-fatal if it skips)
    if cfg.ANTHROPIC_API_KEY:
        try:
            print("📊  PM agent — rolling up recent audience notes…")
            from scanner.pm import generate_weekly_themes
            saved = generate_weekly_themes(
                db_path=cfg.DB_PATH,
                anthropic_key=cfg.ANTHROPIC_API_KEY,
                window_end=date.today(),
            )
            if saved:
                print(f"   ✓ Rollup saved ({saved.get('note_count',0)} notes, "
                      f"{len(saved.get('themes') or [])} themes, "
                      f"{len(saved.get('avoid_list') or [])} avoid items)\n")
            else:
                print("   (not enough notes yet — skipped)\n")
        except Exception as e:
            log.warning("PM rollup failed (non-fatal): %s", e)
            print(f"   ⚠️  PM rollup failed: {e}\n")
    else:
        print("⚠️  No ANTHROPIC_API_KEY — skipping PM rollup.\n")

    # ── Analyst pass (non-fatal)
    if cfg.ANTHROPIC_API_KEY:
        try:
            print("📊  Analyst — scoring tracked politicians on consistency…")
            from scanner.analyst import analyze_all
            rows = analyze_all(
                db_path=cfg.DB_PATH,
                anthropic_key=cfg.ANTHROPIC_API_KEY,
                min_events=3,
            )
            print(f"   ✓ Scored {len(rows)} politicians\n")
        except Exception as e:
            log.warning("Analyst pass failed (non-fatal): %s", e)
            print(f"   ⚠️  Analyst failed: {e}\n")

    # ── Dossier briefs for Cowork (non-fatal)
    # Queue per-candidate research briefs the Cowork agent (Opus 4.7 with
    # web search) drains overnight. Keeps voting-record / public-statement
    # research fresh without paying for a separate Opus API call.
    if getattr(cfg, "USE_COWORK_FOR_OPUS", False):
        try:
            print("🗂️   Dossier briefs — queueing per-candidate research for Cowork…")
            from scanner.dossier import queue_dossier_briefs
            queued = queue_dossier_briefs(
                db_path=cfg.DB_PATH,
                output_dir=cfg.CANDIDATE_DOSSIER_DIR,
            )
            if queued:
                print(f"   ✓ Queued {len(queued)} dossier brief(s): "
                      f"{', '.join(queued[:5])}{' …' if len(queued) > 5 else ''}\n")
            else:
                print("   (all dossiers fresh — nothing to queue)\n")
        except Exception as e:
            log.warning("Dossier briefs failed (non-fatal): %s", e)
            print(f"   ⚠️  Dossier brief queueing failed: {e}\n")

    # ── Report
    cmd_report(args, silent=True)

    # ── Series mode (default while data/candidate_series.json exists)
    # During the 2026 primary build-up, the daily 4-episode podcast is
    # paused in favor of the 4-episode-per-candidate 专题 series. The
    # registry tracks what airs when. If no candidate is scheduled for
    # today, fall through to the legacy daily podcast.
    series_handled = False
    from scanner.series import REGISTRY_PATH as series_registry
    if series_registry.exists():
        try:
            from scanner.series import queue_today_series, queue_filing_monitor
            target = date.today()
            if getattr(args, "date", None):
                target = datetime.strptime(args.date, "%Y-%m-%d").date()
            result = queue_today_series(target_date=target)
            if result["status"] == "queued":
                print(f"📡  Series 专题 — today: {result['candidate']} "
                      f"({result['office']})")
                print(f"   Dossier queued : {result['dossier_queued']}")
                print(f"   Episodes queued: ep{', ep'.join(str(n) for n in result['episodes_queued'])}")
                series_handled = True
            else:
                print(f"📡  No series candidate scheduled for {target.isoformat()}.")
            # Always queue the daily filing-list monitor brief while the
            # registry is not finalized. Cowork drains it overnight and
            # appends any newly-filed candidates.
            try:
                from scanner.series import load_registry
                if not load_registry().get("list_finalized"):
                    queue_filing_monitor()
                    print("   ✓ Filing-list monitor brief queued.")
            except Exception as e:
                log.debug("filing monitor queue failed: %s", e)
        except Exception as e:
            log.warning("series flow failed (non-fatal): %s", e)
            print(f"   ⚠️  series queueing failed: {e}")

    # ── Podcast (legacy daily 4-ep) — only if no series candidate today
    if not series_handled:
        if getattr(args, "no_podcast", False):
            print("(Skipping podcast — --no-podcast flag set.)\n")
            return
        cmd_podcast(args)
        # Deep-dive (legacy) episodes for any candidate the listener named
        _maybe_run_deepdives(args)

    # ── TTS sweep — synthesize MP3s for any Cowork-produced scripts that
    # don't have audio yet. In Cowork mode the daily Author runs are async;
    # by the time the 7am publish job runs, last night's drain has already
    # written real .txt scripts for *yesterday*. We TTS yesterday and today
    # so the webpage picks them up. Idempotent — skips MP3s that already exist.
    if getattr(cfg, "USE_COWORK_FOR_AI", False) and cfg.OPENAI_API_KEY:
        from types import SimpleNamespace
        for d in (date.today() - timedelta(days=1), date.today()):
            try:
                cmd_tts_publish(SimpleNamespace(date=d.isoformat()))
            except Exception as e:
                log.warning("TTS sweep failed for %s: %s", d, e)


def _maybe_run_deepdives(args) -> None:
    """Auto-trigger one deep-dive episode per candidate the PM flagged
    in `listener_candidate_interest`. Bounded to 2 per publish so we
    don't blow up the daily run."""
    from scanner.database import get_latest_weekly_themes
    try:
        rollup = get_latest_weekly_themes(cfg.DB_PATH)
    except Exception as e:
        log.debug("No PM rollup for deep-dive trigger: %s", e)
        return
    if not rollup:
        return
    names = rollup.get("listener_candidate_interest") or []
    if not names:
        return

    from scanner.deepdive import generate_deep_dive
    no_audio = bool(getattr(args, "no_audio", False))
    skip_editor = bool(getattr(args, "skip_editor", False))
    openai_key = "" if no_audio else cfg.OPENAI_API_KEY

    target_date = date.today()
    if getattr(args, "date", None):
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()

    print(f"🎙️   Deep-dive trigger — listener named {len(names)} candidate(s): "
          f"{', '.join(names[:5])}")
    for name in names[:2]:  # cap per day
        try:
            result = generate_deep_dive(
                db_path=cfg.DB_PATH,
                podcasts_dir=cfg.PODCASTS_DIR,
                anthropic_key=cfg.ANTHROPIC_API_KEY,
                openai_key=openai_key,
                candidate_name=name,
                target_date=target_date,
                no_audio=no_audio,
                skip_editor=skip_editor,
            )
        except Exception as e:
            log.warning("Deep dive for %s failed (non-fatal): %s", name, e)
            print(f"   ⚠️  {name}: failed — {e}")
            continue
        if result:
            print(f"   ✓ {name}: {result['word_count']} words → {result['script_path']}")
        else:
            print(f"   (skipped {name}: no record on file yet)")
    print()


def cmd_scan(args):
    """Backward-compatible alias: run FETCH then PUBLISH in one shot.

    Most users should prefer the scheduled split (see `main.py setup`)
    so that yesterday's audience note can influence today's podcast.
    """
    cmd_fetch(args)
    cmd_publish(args)


# ──────────────────────────────────────────────────────────────────────────────
# REPORT
# ──────────────────────────────────────────────────────────────────────────────

def cmd_report(args, silent=False):
    """Generate HTML+Markdown report from DB data."""
    initialize_db(cfg.DB_PATH)

    days = getattr(args, "days", 7)
    events = get_recent_events(cfg.DB_PATH, days=days, min_relevance=0.0)

    # Collect politician summaries for the tracker section
    with get_connection(cfg.DB_PATH) as conn:
        pols = conn.execute(
            "SELECT * FROM politicians ORDER BY level, name"
        ).fetchall()
    pol_summaries = []
    for p in pols:
        ps = get_politician_summary(cfg.DB_PATH, p["name"])
        if ps.get("events"):
            pol_summaries.append(ps)

    target = date.today()
    if getattr(args, "date", None):
        target = datetime.strptime(args.date, "%Y-%m-%d").date()

    report = gen_report(events, pol_summaries, report_date=target)

    save_report(cfg.DB_PATH, target, report["html"], report["markdown"])
    html_path = save_html_report(report["html"], cfg.REPORTS_DIR, target)

    # Also save markdown
    md_path = cfg.REPORTS_DIR / f"digest_{target.strftime('%Y-%m-%d')}.md"
    md_path.write_text(report["markdown"], encoding="utf-8")

    if not silent:
        print(f"✅  Report saved:")
        print(f"   HTML:     {html_path}")
        print(f"   Markdown: {md_path}")

    print(f"\n📄  Report → {html_path}")
    print("   Open this file in your browser to read your digest.\n")


# ──────────────────────────────────────────────────────────────────────────────
# POLITICIAN LOOKUP
# ──────────────────────────────────────────────────────────────────────────────

def cmd_politician(args):
    """Print a summary of a politician's recent activity."""
    initialize_db(cfg.DB_PATH)
    name = " ".join(args.name) if isinstance(args.name, list) else args.name
    pol = get_politician_summary(cfg.DB_PATH, name)
    if not pol:
        print(f"No politician found matching '{name}' in the database.\n"
              "Run `python main.py scan` first to populate data.")
        return

    print(f"\n{'─'*60}")
    print(f"  {pol['name']}")
    print(f"  {pol.get('office','')}  ·  {pol.get('party','')}  ·  {pol.get('level','').title()}")
    print(f"{'─'*60}")
    events = pol.get("events", [])
    if not events:
        print("  No tracked events yet.")
    else:
        for ev in events[:10]:
            role = ev.get("role", "mentioned").replace("_", " ").title()
            title = ev.get("title", "")[:70]
            url = ev.get("source_url", "")
            date_s = ev.get("date", "")
            print(f"\n  [{role}]  {date_s}")
            print(f"  {title}")
            if url:
                print(f"  → {url}")
    print()


# ──────────────────────────────────────────────────────────────────────────────
# STATUS
# ──────────────────────────────────────────────────────────────────────────────

def cmd_status(args):
    """Show recent scan runs and DB stats."""
    initialize_db(cfg.DB_PATH)
    with get_connection(cfg.DB_PATH) as conn:
        runs = conn.execute(
            "SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT 10"
        ).fetchall()
        event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        pol_count = conn.execute("SELECT COUNT(*) FROM politicians").fetchone()[0]
        link_count = conn.execute("SELECT COUNT(*) FROM politician_events").fetchone()[0]

    print(f"\nDatabase: {cfg.DB_PATH}")
    print(f"  Events tracked   : {event_count}")
    print(f"  Politicians      : {pol_count}")
    print(f"  Politician links : {link_count}\n")
    print("Recent scans:")
    for run in runs:
        started = run["started_at"][:16]
        status = run["status"]
        found = run["events_found"]
        new = run["events_new"]
        print(f"  {started}  {status:8s}  found={found}  new={new}")
    print()


# ──────────────────────────────────────────────────────────────────────────────
# SERVE (mobile/Tailscale access)
# ──────────────────────────────────────────────────────────────────────────────

def cmd_serve(args):
    """Start the HTTP server to view reports from phone via Tailscale."""
    run_server(
        reports_dir=cfg.REPORTS_DIR,
        db_path=cfg.DB_PATH,
        podcasts_dir=cfg.PODCASTS_DIR,
        knowledge_dir=cfg.KNOWLEDGE_DIR,
        anthropic_key=cfg.ANTHROPIC_API_KEY,
        chat_model=cfg.CHAT_MODEL,
        host=args.host,
        port=args.port,
    )


# ──────────────────────────────────────────────────────────────────────────────
# PODCAST
# ──────────────────────────────────────────────────────────────────────────────

def cmd_podcast(args):
    """Generate 4×30-min podcast episodes from recent digest events.

    In Cowork mode (Config.USE_COWORK_FOR_AI=True), this writes placeholder
    scripts and queues author_episode briefs for Cowork to fill in overnight.
    The next day's `cmd_tts_publish` synthesizes audio from whatever Cowork
    landed.
    """
    initialize_db(cfg.DB_PATH)

    no_audio = getattr(args, "no_audio", False)
    cowork_mode = getattr(cfg, "USE_COWORK_FOR_AI", False)

    if not cowork_mode and not cfg.ANTHROPIC_API_KEY:
        print("✗  ANTHROPIC_API_KEY missing — add to .env, or set "
              "USE_COWORK_FOR_AI=1 to route AI through Cowork instead.")
        return
    if not no_audio and not cfg.OPENAI_API_KEY:
        print("⚠️   OPENAI_API_KEY missing — running with --no-audio implicitly.")
        no_audio = True

    target = date.today()
    if getattr(args, "date", None):
        target = datetime.strptime(args.date, "%Y-%m-%d").date()

    print(f"\n🎙  Generating 4 podcast episodes for {target} …")
    print("   Episodes: Federal · Maryland · County+MCPS · Week in Review\n")

    try:
        results = generate_podcast_episodes(
            db_path=cfg.DB_PATH,
            podcasts_dir=cfg.PODCASTS_DIR,
            anthropic_key=cfg.ANTHROPIC_API_KEY,
            openai_key=cfg.OPENAI_API_KEY,
            target_date=target,
            script_model=cfg.PODCAST_SCRIPT_MODEL,
            tts_model=cfg.PODCAST_TTS_MODEL,
            alex_voice=cfg.PODCAST_HOST_ALEX_VOICE,
            jordan_voice=cfg.PODCAST_HOST_JORDAN_VOICE,
            no_audio=no_audio,
            filter_incidents=cfg.PODCAST_FILTER_INDIVIDUAL_INCIDENTS,
            skip_editor=getattr(args, "skip_editor", False),
            no_defer=getattr(args, "no_defer", False),
        )
    except Exception as e:
        log.exception("Podcast generation failed")
        print(f"✗  Failed: {e}")
        save_podcast(cfg.DB_PATH, target, title=f"Daily Digest {target}",
                     script="", audio_path="",
                     status="error", error_log=str(e))
        return

    print(f"\n✅  {len(results)} episodes saved:")
    ts_ip = get_tailscale_ip()
    for ep in results:
        ep_label = f"Ep {ep['episode_num']}: {ep['episode_title']}"
        words = ep.get("word_count", 0)
        mins_approx = words // 130
        title = f"{ep_label} — {target.strftime('%A, %B %d, %Y')}"
        save_podcast(
            cfg.DB_PATH, target,
            title=title,
            script=ep.get("script", ""),
            audio_path=ep.get("audio_path", ""),
            duration_seconds=ep.get("duration_seconds", 0),
            word_count=words,
            status=ep.get("status", "done"),
        )
        if ep.get("audio_path"):
            print(f"   {ep_label}: {ep['audio_path']}  (~{mins_approx} min)")
        else:
            print(f"   {ep_label}: {ep.get('script_path','')}  (~{mins_approx} min, script only)")
        editor_notes = ep.get("editor_notes") or ""
        if editor_notes:
            tag = "revised" if ep.get("editor_changed") else "approved"
            print(f"       ✏️  Editor {tag}: {editor_notes}")

    if ts_ip and not no_audio:
        slug = target.strftime('%Y-%m-%d')
        print(f"\n📱  Listen on phone (Tailscale):")
        print(f"      http://{ts_ip}:8080/podcasts")
        for ep in results:
            fname = f"podcast_{slug}_ep{ep['episode_num']}.mp3"
            print(f"      http://{ts_ip}:8080/podcast/{fname}")
    print()


# ──────────────────────────────────────────────────────────────────────────────
# SETUP SCHEDULER
# ──────────────────────────────────────────────────────────────────────────────

def cmd_candidates(args):
    """Refresh candidate data from DB and print a summary table."""
    initialize_db(cfg.DB_PATH)
    print("\n── Candidate Tracker ─────────────────────────────────────")
    print(f"   Cycle    : 2026 elections")
    loc = ", ".join(p for p in [cfg.CITY, cfg.COUNTY, cfg.STATE] if p) or "(location not set in .env)"
    print(f"   Location : {loc}")
    print("──────────────────────────────────────────────────────────")
    print_candidates_table(cfg.DB_PATH)


def cmd_analyst(args):
    """Score every tracked politician on the consistency of their positions."""
    initialize_db(cfg.DB_PATH)
    if not cfg.ANTHROPIC_API_KEY:
        print("✗  ANTHROPIC_API_KEY missing — add to .env and retry.")
        return

    from scanner.analyst import analyze_all, analyze_one, format_score_for_prompt
    from scanner.database import get_connection

    target = (args.name or "").strip() if getattr(args, "name", None) else ""
    level = getattr(args, "level", None)
    min_events = getattr(args, "min_events", None) or 3

    if target:
        with get_connection(cfg.DB_PATH) as conn:
            row = conn.execute(
                "SELECT id, name FROM politicians WHERE name LIKE ?",
                (f"%{target}%",),
            ).fetchone()
        if not row:
            print(f"No politician matches '{target}'. Run `scan` first.")
            return
        print(f"\n📊  Analyst — scoring {row['name']}…\n")
        saved = analyze_one(
            db_path=cfg.DB_PATH,
            anthropic_key=cfg.ANTHROPIC_API_KEY,
            politician_id=row["id"],
            politician_name=row["name"],
            min_events=min_events,
        )
        if saved:
            print(format_score_for_prompt(saved))
        else:
            print("(No score produced — see logs.)")
        print()
        return

    print(f"\n📊  Analyst — scoring all politicians "
          f"(min {min_events} events"
          f"{', level=' + level if level else ''})…\n")
    rows = analyze_all(
        db_path=cfg.DB_PATH,
        anthropic_key=cfg.ANTHROPIC_API_KEY,
        level=level,
        min_events=min_events,
    )
    if not rows:
        print("(No scores produced.)\n")
        return
    for r in rows:
        print(format_score_for_prompt(r))
        print()


def cmd_backfill(args):
    """Pull historical news for tracked politicians (Google News, deeper window)."""
    initialize_db(cfg.DB_PATH)
    from scanner.sources.news_backfill import backfill_all

    locale_hint = getattr(args, "locale_hint", None) or cfg.STATE or "Maryland"
    window = getattr(args, "window", None) or "2y"
    level = getattr(args, "level", None)
    name = getattr(args, "name", None)
    max_items = getattr(args, "max_items", None) or 40

    print(f"\n📚  Historical news backfill — window={window}, "
          f"locale_hint={locale_hint}"
          f"{', level=' + level if level else ''}"
          f"{', name~=' + name if name else ''}\n")
    runs = backfill_all(
        db_path=cfg.DB_PATH,
        locale_hint=locale_hint,
        window=window,
        level=level,
        name_filter=name,
        max_items=max_items,
    )
    if not runs:
        print("(No politicians matched, or no items returned.)\n")
        return
    for r in runs:
        status = r.get("status", "?")
        print(f"  {r['politician_name']:30s}  found={r['items_found']:3d}  "
              f"new={r['items_new']:3d}  status={status}  "
              f"({r['window_start']}..{r['window_end']})")
    print()


def cmd_discover(args):
    """Discover candidates running for each configured-district contest."""
    initialize_db(cfg.DB_PATH)
    if not cfg.ANTHROPIC_API_KEY:
        print("✗  ANTHROPIC_API_KEY missing — add to .env and retry.")
        return

    from scanner.sources.candidate_discover import discover_all

    ballot_year = int(getattr(args, "year", None) or date.today().year)
    window = getattr(args, "window", None) or "1y"

    print(f"\n🗳️   Candidate discovery — ballot year {ballot_year}, window {window}\n")
    runs = discover_all(
        db_path=cfg.DB_PATH,
        anthropic_key=cfg.ANTHROPIC_API_KEY,
        ballot_year=ballot_year,
        window=window,
    )
    if not runs:
        print("(No contests derived — set your district fields in .env.)\n")
        return
    for r in runs:
        office = r.get("contest", {}).get("office", "?")
        found = r.get("candidates_found", 0)
        saved = r.get("candidates_saved", 0)
        err = r.get("error")
        if err:
            print(f"  ⚠️  {office}: error — {err}")
        else:
            print(f"  • {office}: found={found}, saved={saved}")
    print()


def cmd_deepdive(args):
    """Generate a ~30-min single-candidate deep-dive (专题) episode."""
    initialize_db(cfg.DB_PATH)
    if not cfg.ANTHROPIC_API_KEY:
        print("✗  ANTHROPIC_API_KEY missing — add to .env and retry.")
        return

    from scanner.deepdive import generate_deep_dive

    name = " ".join(args.name).strip() if getattr(args, "name", None) else ""
    if not name:
        print("✗  Please provide a candidate name.")
        return

    target_date = date.today()
    if getattr(args, "date", None):
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()

    no_audio = bool(getattr(args, "no_audio", False))
    openai_key = "" if no_audio else cfg.OPENAI_API_KEY
    print(f"\n🎙️   Deep dive — {name} ({target_date})\n")
    result = generate_deep_dive(
        db_path=cfg.DB_PATH,
        podcasts_dir=cfg.PODCASTS_DIR,
        anthropic_key=cfg.ANTHROPIC_API_KEY,
        openai_key=openai_key,
        candidate_name=name,
        target_date=target_date,
        no_audio=no_audio,
        skip_editor=bool(getattr(args, "skip_editor", False)),
    )
    if not result:
        print("(No episode produced — candidate not found or no record on file.)\n")
        return
    print(f"   ✓ {result['word_count']} words → {result['script_path']}")
    if result.get("audio_path"):
        print(f"   ✓ audio → {result['audio_path']} "
              f"(~{result['duration_seconds']//60}:{result['duration_seconds']%60:02d})")
    print()


def cmd_tts_publish(args):
    """Synthesize MP3s for any podcast scripts on disk that are missing audio.

    Runs after Cowork has drained the inbox overnight. Walks
    `podcasts/podcast_<date>_*.txt` for the target date, and for each script
    that's missing the matching `.mp3`, calls OpenAI TTS to synthesize it.

    This is the only Anthropic-free step that still needs a paid API key —
    OpenAI TTS for audio. Set OPENAI_API_KEY in .env.
    """
    if not cfg.OPENAI_API_KEY:
        print("✗  OPENAI_API_KEY missing — add to .env.")
        return

    target = date.today()
    if getattr(args, "date", None):
        target = datetime.strptime(args.date, "%Y-%m-%d").date()

    date_str = target.strftime("%Y-%m-%d")
    pattern = f"podcast_{date_str}_*.txt"
    scripts = sorted(p for p in cfg.PODCASTS_DIR.glob(pattern)
                     if not p.name.endswith(".draft.txt")
                     and not p.name.endswith(".rewrite.txt")
                     and not p.name.endswith(".editor.txt"))
    if not scripts:
        print(f"(no scripts in podcasts/ for {date_str} — nothing to TTS)")
        return

    import openai as _openai
    from scanner.podcast import _synthesize_dialogue
    client = _openai.OpenAI(api_key=cfg.OPENAI_API_KEY)

    print(f"\n🔊  TTS publish — {date_str} ({len(scripts)} script(s))")
    for script_path in scripts:
        script = script_path.read_text(encoding="utf-8")
        # Skip empty / placeholder scripts
        if len(script.strip()) < 200:
            print(f"   ⊘  {script_path.name}: too short — skipping (probably a "
                  "Cowork placeholder, run again after the drain)")
            continue
        mp3_path = script_path.with_suffix(".mp3")
        if mp3_path.exists() and mp3_path.stat().st_size > 1024:
            print(f"   ✓  {mp3_path.name} already synthesized")
            continue
        try:
            duration = _synthesize_dialogue(
                client, script, mp3_path, cfg.PODCAST_TTS_MODEL,
            )
            print(f"   ✓  {mp3_path.name} ({duration//60}:{duration%60:02d})")
        except Exception as e:
            print(f"   ✗  {script_path.name}: {e}")


def cmd_cowork_queue(args):
    """Push a full set of briefs to Cowork: enrich + PM + analyst + dossier
    + author episodes for today.

    Used as the evening 22:00–22:15 step on Windows (after fetch). Cowork
    then drains everything overnight; tomorrow morning's tts-publish renders
    the audio.
    """
    initialize_db(cfg.DB_PATH)
    if not getattr(cfg, "USE_COWORK_FOR_AI", False):
        print("Note: USE_COWORK_FOR_AI is off — this command does nothing. "
              "Enable it in .env or config.py.")
        return

    target = date.today()
    if getattr(args, "date", None):
        target = datetime.strptime(args.date, "%Y-%m-%d").date()

    print(f"\n📨  Queueing Cowork briefs for {target}…\n")

    # 1. Event enrichment — for unenriched events
    try:
        from scanner.processor import process_batch
        from scanner.database import list_unenriched_events
        try:
            unenriched = list_unenriched_events(cfg.DB_PATH, days=2)
        except Exception:
            # Fallback: any event missing summary
            from scanner.database import get_recent_events
            unenriched = [e for e in get_recent_events(cfg.DB_PATH, days=2, min_relevance=0.0)
                          if not (e.get("summary") or "").strip()]
        if unenriched:
            process_batch("", unenriched)   # cowork mode → just queues
            print(f"   ✓ enrich_events  → {len(unenriched)} events queued")
        else:
            print("   ⊘ enrich_events  → all events already enriched")
    except Exception as e:
        print(f"   ⚠ enrich_events failed: {e}")

    # 2. PM rollup
    try:
        from scanner.pm import generate_weekly_themes
        generate_weekly_themes(db_path=cfg.DB_PATH, anthropic_key="",
                                window_end=target)
        print("   ✓ weekly_themes  → queued")
    except Exception as e:
        print(f"   ⚠ weekly_themes failed: {e}")

    # 3. Analyst pass
    try:
        from scanner.analyst import analyze_all
        analyze_all(db_path=cfg.DB_PATH, anthropic_key="")
        print("   ✓ score_consistency → queued")
    except Exception as e:
        print(f"   ⚠ score_consistency failed: {e}")

    # 4. Dossiers
    try:
        from scanner.dossier import queue_dossier_briefs
        names = queue_dossier_briefs(db_path=cfg.DB_PATH,
                                      output_dir=cfg.CANDIDATE_DOSSIER_DIR)
        if names:
            print(f"   ✓ candidate_dossier → {len(names)} candidate(s) queued")
        else:
            print("   ⊘ candidate_dossier → all dossiers fresh")
    except Exception as e:
        print(f"   ⚠ candidate_dossier failed: {e}")

    # 5. Author episodes — runs the podcast pipeline which queues briefs
    try:
        from scanner.podcast import generate_podcast_episodes
        results = generate_podcast_episodes(
            db_path=cfg.DB_PATH,
            podcasts_dir=cfg.PODCASTS_DIR,
            anthropic_key="",
            openai_key="",
            target_date=target,
            no_audio=True,
            filter_incidents=cfg.PODCAST_FILTER_INDIVIDUAL_INCIDENTS,
        )
        n_queued = sum(1 for r in results if r.get("status") == "queued_cowork")
        print(f"   ✓ author_episode → {n_queued} episode(s) queued")
    except Exception as e:
        print(f"   ⚠ author_episode failed: {e}")

    # 6. Deep-dives — based on PM-flagged candidates the listener named
    try:
        from scanner.database import get_latest_weekly_themes
        rollup = get_latest_weekly_themes(cfg.DB_PATH)
        names = (rollup or {}).get("listener_candidate_interest") or []
        if names:
            from scanner.deepdive import generate_deep_dive
            for nm in names[:2]:
                generate_deep_dive(
                    db_path=cfg.DB_PATH, podcasts_dir=cfg.PODCASTS_DIR,
                    anthropic_key="", openai_key="",
                    candidate_name=nm, target_date=target,
                    no_audio=True, skip_editor=False,
                )
            print(f"   ✓ deep_dive_script → {min(2, len(names))} queued")
        else:
            print("   ⊘ deep_dive_script → no listener-flagged candidates")
    except Exception as e:
        print(f"   ⚠ deep_dive_script failed: {e}")

    print(f"\nAll briefs are in: {cfg.COWORK_INBOX_DIR}")
    print("The Cowork drain-cowork-inbox scheduled task will process them on "
          "its next run.\n")


def cmd_series(args):
    """4-episode-per-candidate series (专题) commands.

    Subcommands:
      python main.py series today          # queue today's scheduled candidate
      python main.py series queue NAME     # force-queue a named candidate
      python main.py series status         # show progress + next 14 days
      python main.py series monitor        # queue an SBE-list-diff brief
      python main.py series reconcile      # walk disk for completed episodes
    """
    initialize_db(cfg.DB_PATH)
    sub = getattr(args, "series_cmd", None)
    if sub == "today":
        target = date.today()
        if getattr(args, "date", None):
            target = datetime.strptime(args.date, "%Y-%m-%d").date()
        from scanner.series import queue_today_series, status_summary
        result = queue_today_series(target_date=target)
        if result["status"] == "no_candidate":
            print(f"(no candidate scheduled for {target.isoformat()})")
            return
        print(f"\n📡  Series queue — {result['date']} · {result['candidate']}")
        print(f"    Office     : {result.get('office','')}")
        print(f"    Dossier    : {'queued' if result['dossier_queued'] else 'already on disk'}")
        print(f"    Episodes   : queued ep{', ep'.join(str(n) for n in result['episodes_queued'])}")
        print(f"    → All briefs in: {cfg.COWORK_INBOX_DIR}")
    elif sub == "queue":
        from scanner.series import find_candidate, queue_today_series, save_registry, load_registry
        name = " ".join(args.name) if isinstance(args.name, list) else args.name
        reg = load_registry()
        cand = find_candidate(name, reg)
        if not cand:
            print(f"✗  '{name}' not in registry. Run `series monitor` to refresh, or add manually.")
            return
        target = date.today()
        if getattr(args, "date", None):
            target = datetime.strptime(args.date, "%Y-%m-%d").date()
        # Re-point the candidate to today so queue_today_series picks them up.
        cand["scheduled_date"] = target.isoformat()
        save_registry(reg)
        result = queue_today_series(target_date=target)
        print(f"\n📡  Force-queued {cand['name']} for {target.isoformat()}")
        print(f"    Episodes queued: {result.get('episodes_queued')}")
    elif sub == "status":
        from scanner.series import status_summary
        print(status_summary())
    elif sub == "monitor":
        from scanner.series import queue_filing_monitor
        p = queue_filing_monitor()
        print(f"📡  Filing-list monitor brief queued: {p}")
        print("    Cowork will re-fetch SBE pages on the next drain and update the registry.")
    elif sub == "reconcile":
        from scanner.series import reconcile_completed_episodes
        n = reconcile_completed_episodes(cfg.PODCASTS_DIR)
        print(f"✓  Marked {n} new episode(s) as done.")
    elif sub == "scout":
        from scanner.series import queue_scout_all
        names = queue_scout_all(force=getattr(args, "force", False))
        if names:
            print(f"📡  Queued dossier_scout briefs for {len(names)} candidate(s):")
            for n in names: print(f"    • {n}")
            print("    Cowork drains overnight; run `series scout-results` after to pull richness scores back into the registry.")
        else:
            print("(all candidates already have scout results — pass --force to re-scout)")
    elif sub == "scout-results":
        from scanner.series import apply_scout_results
        n = apply_scout_results()
        print(f"✓  Applied {n} scout result(s) to the registry.")
    elif sub == "reschedule":
        from scanner.series import reschedule_by_readiness
        n = reschedule_by_readiness()
        print(f"✓  Rescheduled {n} candidate(s) by tier+richness. New schedule:")
        from scanner.series import status_summary
        print(status_summary())
    else:
        print("Usage: python main.py series {today|queue NAME|status|scout|scout-results|reschedule|monitor|reconcile}")


def cmd_dossier(args):
    """Queue Cowork dossier briefs for filed candidates / listener-named names.

    Designed to be called both from the daily publish pipeline and standalone.
    The Cowork agent (Opus 4.7) drains the resulting briefs from cowork_inbox/
    on its next scheduled run and writes per-candidate dossiers under
    data/candidate_dossiers/.
    """
    initialize_db(cfg.DB_PATH)
    from scanner.dossier import queue_dossier_briefs

    only_names = None
    if getattr(args, "name", None):
        only_names = [args.name] if isinstance(args.name, str) else list(args.name)

    queued = queue_dossier_briefs(
        db_path=cfg.DB_PATH,
        output_dir=cfg.CANDIDATE_DOSSIER_DIR,
        only_names=only_names,
        force=bool(getattr(args, "force", False)),
        max_briefs=int(getattr(args, "max", 12)),
    )
    if queued:
        print(f"✓ Queued {len(queued)} dossier brief(s):")
        for n in queued:
            print(f"   • {n}")
        print(f"\nBriefs are in: {cfg.COWORK_INBOX_DIR}")
        print("The Cowork drain-cowork-inbox scheduled task will process them on its next run,")
        print("or you can click 'Run now' on it from the Scheduled sidebar.")
    else:
        print("No dossier briefs queued — all dossiers are fresh, or no candidates matched.")
        if getattr(args, "force", False):
            print("(--force was set, but no eligible candidates were found.)")


def cmd_pm(args):
    """Roll up recent daily_notes into themes/open-questions/underserved-topics."""
    initialize_db(cfg.DB_PATH)
    if not cfg.ANTHROPIC_API_KEY:
        print("✗  ANTHROPIC_API_KEY missing — add to .env and retry.")
        return

    from scanner.pm import generate_weekly_themes, format_themes_for_prompt

    end = date.today()
    if getattr(args, "date", None):
        end = datetime.strptime(args.date, "%Y-%m-%d").date()
    days = getattr(args, "days", None) or 7

    print(f"\n📊  PM rollup — window ending {end} ({days} days back)…\n")
    saved = generate_weekly_themes(
        db_path=cfg.DB_PATH,
        anthropic_key=cfg.ANTHROPIC_API_KEY,
        window_end=end,
        window_days=days,
    )
    if not saved:
        print("(No rollup produced — too few notes in window, or LLM call failed.)\n")
        return

    print(format_themes_for_prompt(saved))
    print()


def cmd_setup(args):
    """Register Windows scheduled tasks: morning publish, evening fetch,
    and (optional) always-on web server.

    The split matters: yesterday's audience note can only influence
    today's podcast if the Collector runs LAST and the Author uses the
    previous day's data. See feedback_tool_positioning_and_pipeline.
    """
    python_exe = sys.executable
    script = str(Path(__file__).resolve())
    publish_log = str(Path(__file__).parent / "publish.log")
    fetch_log = str(Path(__file__).parent / "fetch.log")
    server_log = str(Path(__file__).parent / "server.log")

    # ── Task 1: morning publish at 07:00 (PM → Analyst → report → podcast)
    publish_task = "LocalPoliticsPublish"
    publish_cmd = (
        f'schtasks /create /tn "{publish_task}" /tr '
        f'"{python_exe} {script} publish >> {publish_log} 2>&1" '
        f'/sc daily /st 07:00 /f'
    )
    print(f"[1/3] Creating morning publish task '{publish_task}'…")
    r = subprocess.run(publish_cmd, shell=True, capture_output=True, text=True)
    if r.returncode == 0:
        print(f"      ✅  Runs daily at 07:00 (uses last night's fetch) · logs → {publish_log}")
    else:
        print(f"      ✗  Failed: {r.stderr}")

    # ── Task 2: evening fetch at 22:00 (Collector primes tomorrow)
    fetch_task = "LocalPoliticsFetch"
    fetch_cmd = (
        f'schtasks /create /tn "{fetch_task}" /tr '
        f'"{python_exe} {script} fetch >> {fetch_log} 2>&1" '
        f'/sc daily /st 22:00 /f'
    )
    print(f"[2/3] Creating evening fetch task '{fetch_task}'…")
    r = subprocess.run(fetch_cmd, shell=True, capture_output=True, text=True)
    if r.returncode == 0:
        print(f"      ✅  Runs daily at 22:00 (primes tomorrow morning) · logs → {fetch_log}")
    else:
        print(f"      ✗  Failed: {r.stderr}")

    # ── Clean up the old single-shot scan task if it's still there
    subprocess.run('schtasks /delete /tn "LocalPoliticsScan" /f',
                   shell=True, capture_output=True, text=True)

    # ── Task 3: web server auto-start via Startup folder (no admin needed) ──
    if args.no_server:
        print("\n(Skipping web server auto-start — run `python main.py serve` manually.)")
        return

    startup_dir = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
    launcher_bat = startup_dir / "LocalPoliticsServer.bat"
    print(f"\n[3/3] Creating auto-start launcher at:\n      {launcher_bat}")
    script_dir = Path(__file__).parent
    bat_content = (
        f"@echo off\r\n"
        f"cd /d \"{script_dir}\"\r\n"
        f"start \"\" /B \"{python_exe}\" \"{script}\" serve --port {args.port} >> \"{server_log}\" 2>&1\r\n"
    )
    try:
        startup_dir.mkdir(parents=True, exist_ok=True)
        launcher_bat.write_text(bat_content, encoding="utf-8")
        print(f"      OK Server will auto-start at every Windows login.")
    except Exception as e:
        print(f"      Could not create startup launcher: {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(prog="python main.py",
                                     description="Local Politics Scanner")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("fetch", help="Collector only")

    publish = sub.add_parser("publish", help="Publish today's content")
    publish.add_argument("--date")
    publish.add_argument("--no-audio", action="store_true")
    publish.add_argument("--skip-editor", action="store_true")
    publish.add_argument("--no-podcast", action="store_true")
    publish.add_argument("--no-defer", action="store_true")

    scan = sub.add_parser("scan", help="Fetch then publish (legacy)")
    scan.add_argument("--with-podcast", action="store_true")
    scan.add_argument("--no-podcast", action="store_true")
    scan.add_argument("--no-audio", action="store_true")
    scan.add_argument("--skip-editor", action="store_true")
    scan.add_argument("--date")

    pod = sub.add_parser("podcast", help="Generate podcast")
    pod.add_argument("--date")
    pod.add_argument("--no-audio", action="store_true")
    pod.add_argument("--skip-editor", action="store_true")
    pod.add_argument("--no-defer", action="store_true")

    rep = sub.add_parser("report", help="Generate report")
    rep.add_argument("--days", type=int, default=7)
    rep.add_argument("--date", help="Target date YYYY-MM-DD (default: today)")

    pol = sub.add_parser("politician", help="Look up politician")
    pol.add_argument("name", nargs="+")

    sub.add_parser("status", help="Show stats")
    sub.add_parser("candidates", help="Show candidates")

    srv = sub.add_parser("serve", help="HTTP server")
    srv.add_argument("--host", default="0.0.0.0")
    srv.add_argument("--port", type=int, default=8080)

    pm = sub.add_parser("pm", help="PM rollup")
    pm.add_argument("--date")
    pm.add_argument("--days", type=int, default=7)

    an = sub.add_parser("analyst", help="Score politicians")
    an.add_argument("--name")
    an.add_argument("--level", choices=["federal","state","county","school","local"])
    an.add_argument("--min-events", type=int, default=3)

    disc = sub.add_parser("discover", help="Discover candidates")
    disc.add_argument("--year", type=int)
    disc.add_argument("--window", default="1y")

    dd = sub.add_parser("deepdive", help="Deep-dive episode")
    dd.add_argument("name", nargs="+")
    dd.add_argument("--date")
    dd.add_argument("--no-audio", action="store_true")
    dd.add_argument("--skip-editor", action="store_true")

    bf = sub.add_parser("backfill", help="Backfill news")
    bf.add_argument("--name")
    bf.add_argument("--level", choices=["federal","state","county","school","local"])
    bf.add_argument("--window", default="2y")
    bf.add_argument("--locale-hint")
    bf.add_argument("--max-items", type=int, default=40)

    setup = sub.add_parser("setup", help="Register scheduled tasks")
    setup.add_argument("--port", type=int, default=8080)
    setup.add_argument("--no-server", action="store_true")

    tts = sub.add_parser("tts-publish", help="TTS pass for already-written scripts")
    tts.add_argument("--date")

    cw = sub.add_parser("cowork-queue", help="Queue all Cowork briefs for today")
    cw.add_argument("--date")

    do = sub.add_parser("dossier", help="Queue dossier briefs")
    do.add_argument("--names", help="Comma-separated names")
    do.add_argument("--force", action="store_true")
    do.add_argument("--max", type=int, default=12)

    sr = sub.add_parser("series",
        help="4-episode-per-candidate series orchestration (专题)")
    sr_sub = sr.add_subparsers(dest="series_cmd")
    sr_today = sr_sub.add_parser("today", help="Queue today's scheduled candidate")
    sr_today.add_argument("--date", help="Override target date (YYYY-MM-DD)")
    sr_queue = sr_sub.add_parser("queue", help="Force-queue a named candidate")
    sr_queue.add_argument("name", nargs="+", help="Candidate name (partial match OK)")
    sr_queue.add_argument("--date", help="Air date (YYYY-MM-DD; default today)")
    sr_sub.add_parser("status", help="Show registry stats + next 14 days")
    sr_sub.add_parser("monitor", help="Queue a SBE-list-diff brief for the registry")
    sr_sub.add_parser("reconcile", help="Mark episodes done if their .txt is on disk")
    sr_scout = sr_sub.add_parser("scout", help="Queue fast richness recon for every candidate")
    sr_scout.add_argument("--force", action="store_true", help="Re-scout even those with results")
    sr_sub.add_parser("scout-results", help="Apply scout JSON files to the registry (richness, age, lookback)")
    sr_sub.add_parser("reschedule", help="Reorder air dates within tier by richness (rich first, thin later)")

    args = parser.parse_args()

    dispatch = {
        "fetch": cmd_fetch,
        "publish": cmd_publish,
        "scan": cmd_scan,
        "report": cmd_report,
        "politician": cmd_politician,
        "status": cmd_status,
        "serve": cmd_serve,
        "podcast": cmd_podcast,
        "setup": cmd_setup,
        "candidates": cmd_candidates,
        "pm": cmd_pm,
        "analyst": cmd_analyst,
        "backfill": cmd_backfill,
        "discover": cmd_discover,
        "deepdive": cmd_deepdive,
        "tts-publish": cmd_tts_publish,
        "cowork-queue": cmd_cowork_queue,
        "dossier": cmd_dossier,
        "series": cmd_series,
    }
    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        return
    fn(args)


if __name__ == "__main__":
    main()
