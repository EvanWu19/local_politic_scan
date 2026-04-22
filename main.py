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
from datetime import date, datetime
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

    # ── Report
    cmd_report(args, silent=True)

    # ── Podcast
    if getattr(args, "no_podcast", False):
        print("(Skipping podcast — --no-podcast flag set.)\n")
        return
    cmd_podcast(args)

    # ── Deep-dive (专题) episodes for any candidate the listener named
    _maybe_run_deepdives(args)


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

    report = gen_report(events, pol_summaries)

    today = date.today()
    save_report(cfg.DB_PATH, today, report["html"], report["markdown"])
    html_path = save_html_report(report["html"], cfg.REPORTS_DIR, today)

    # Also save markdown
    md_path = cfg.REPORTS_DIR / f"digest_{today.strftime('%Y-%m-%d')}.md"
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
    """Generate 4×30-min podcast episodes from recent digest events."""
    initialize_db(cfg.DB_PATH)

    no_audio = getattr(args, "no_audio", False)
    if not cfg.ANTHROPIC_API_KEY:
        print("✗  ANTHROPIC_API_KEY missing — add to .env and retry.")
        return
    if not no_audio and not cfg.OPENAI_API_KEY:
        print("✗  OPENAI_API_KEY missing — add to .env or use --no-audio.")
        return

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

    # VBS wrapper makes the cmd window stay hidden; bat calls python directly
    script_dir = Path(__file__).parent
    bat_content = (
        f'@echo off\r\n'
        f'cd /d "{script_dir}"\r\n'
        f'start "" /B "{python_exe}" "{script}" serve --port {args.port} '
        f'>> "{server_log}" 2>&1\r\n'
    )
    try:
        startup_dir.mkdir(parents=True, exist_ok=True)
        launcher_bat.write_text(bat_content, encoding="utf-8")
        print(f"      ✅  Server will auto-start at every Windows login.")
        print(f"          Logs: {server_log}")
        print(f"          To remove: delete that .bat file")

        ts_ip = get_tailscale_ip()
        hostname = os.environ.get("COMPUTERNAME", "this-pc")
        print("\n📱  Access reports from your phone via Tailscale:")
        if ts_ip:
            print(f"      http://{ts_ip}:{args.port}/        ← bookmark this on your phone")
        else:
            print(f"      http://<your-tailscale-ip>:{args.port}/")
            print("      (run `tailscale ip -4` in PowerShell to find the IP)")
        print(f"      http://{hostname}:{args.port}/       (Tailscale MagicDNS alias)")
        print("\n      To start the server NOW (without rebooting):")
        print(f"      .venv\\Scripts\\python main.py serve")
    except Exception as e:
        print(f"      ✗  Could not create startup launcher: {e}")
        print(f"      You can still run the server manually:")
        print(f"        .venv\\Scripts\\python main.py serve")

    print(f"\nTo remove the scheduled tasks later:")
    print(f"  schtasks /delete /tn {publish_task} /f")
    print(f"  schtasks /delete /tn {fetch_task} /f")


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Local Politics Scanner",
    )
    sub = parser.add_subparsers(dest="command")

    fetch = sub.add_parser("fetch",
        help="Collector only: fetch all sources, enrich with AI, save to DB (no report/podcast)")

    publish = sub.add_parser("publish",
        help="Publish today's content from existing DB data: PM → Analyst → report → podcast")
    publish.add_argument("--date", help="Target date YYYY-MM-DD for podcast (default: today)")
    publish.add_argument("--no-audio", action="store_true",
                         help="Skip OpenAI TTS (write script only)")
    publish.add_argument("--skip-editor", action="store_true",
                         help="Skip the Editor pass (draft straight to TTS)")
    publish.add_argument("--no-podcast", action="store_true",
                         help="Only generate the report, skip the podcast")

    scan = sub.add_parser("scan",
        help="(Legacy alias) Fetch then immediately publish in one shot — "
             "prefer scheduled `fetch` + `publish` for day-over-day feedback loop")
    scan.add_argument("--with-podcast", action="store_true",
                      help="Also generate today's podcast (legacy flag — on by default in `publish`)")
    scan.add_argument("--no-podcast", action="store_true",
                      help="Skip the podcast when running in one-shot mode")
    scan.add_argument("--no-audio", action="store_true",
                      help="Generate scripts but skip OpenAI TTS")
    scan.add_argument("--skip-editor", action="store_true",
                      help="Skip the Editor pass (draft straight to TTS)")
    scan.add_argument("--date", help="Target date YYYY-MM-DD (default: today)")

    pod = sub.add_parser("podcast", help="Generate a 2-hour two-host podcast from recent digest")
    pod.add_argument("--date", help="Target date YYYY-MM-DD (default: today)")
    pod.add_argument("--no-audio", action="store_true",
                     help="Write the script to .txt but skip OpenAI TTS (no audio cost)")
    pod.add_argument("--skip-editor", action="store_true",
                     help="Skip the Editor pass (go straight from draft to TTS)")

    rep = sub.add_parser("report", help="Generate report from existing DB data")
    rep.add_argument("--days", type=int, default=7, help="Days of history to include (default: 7)")

    pol = sub.add_parser("politician", help="Look up a politician's recent tracked activity")
    pol.add_argument("name", nargs="+", help="Politician name (can be partial)")

    sub.add_parser("status", help="Show scan history and database stats")

    sub.add_parser("candidates", help="Show candidate tracker (2026 cycle) with recent DB activity")

    srv = sub.add_parser("serve", help="Start HTTP server for phone/Tailscale access")
    srv.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    srv.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")

    pm = sub.add_parser("pm", help="Roll up recent audience daily_notes into themes")
    pm.add_argument("--date", help="Window end date YYYY-MM-DD (default: today)")
    pm.add_argument("--days", type=int, default=7, help="Window length in days (default: 7)")

    an = sub.add_parser("analyst", help="Score politicians' position consistency from tracked events")
    an.add_argument("--name", help="Score just this politician (LIKE match)")
    an.add_argument("--level", choices=["federal", "state", "county", "school", "local"],
                    help="Limit to politicians at this level")
    an.add_argument("--min-events", type=int, default=3,
                    help="Skip politicians with fewer linked events (default: 3)")

    disc = sub.add_parser("discover",
        help="Discover ballot candidates from Google News per configured district")
    disc.add_argument("--year", type=int, help="Ballot year (default: current year)")
    disc.add_argument("--window", default="1y",
                       help="Google News time window: '90d', '6m', '1y', '2y' (default: 1y)")

    dd = sub.add_parser("deepdive",
        help="Generate a ~30-min deep-dive (专题) episode for one candidate")
    dd.add_argument("name", nargs="+", help="Candidate name (can be partial)")
    dd.add_argument("--date", help="Target date YYYY-MM-DD (default: today)")
    dd.add_argument("--no-audio", action="store_true",
                     help="Write the script only; skip OpenAI TTS")
    dd.add_argument("--skip-editor", action="store_true",
                     help="Skip the Editor pass")

    bf = sub.add_parser("backfill", help="Fetch historical news per politician (Google News deep search)")
    bf.add_argument("--name", help="Backfill just this politician (LIKE match)")
    bf.add_argument("--level", choices=["federal", "state", "county", "school", "local"],
                    help="Limit to politicians at this level")
    bf.add_argument("--window", default="2y",
                    help="Google News time window: e.g. '90d', '6m', '2y' (default: 2y)")
    bf.add_argument("--locale-hint", help="Extra search keyword (default: USER_STATE)")
    bf.add_argument("--max-items", type=int, default=40,
                    help="Max items per politician (default: 40)")

    setup = sub.add_parser("setup", help="Register daily-scan + web-server scheduled tasks")
    setup.add_argument("--port", type=int, default=8080, help="Server port (default: 8080)")
    setup.add_argument("--no-server", action="store_true",
                       help="Only schedule the daily scan, not the web server")

    args = parser.parse_args()

    if args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "publish":
        cmd_publish(args)
    elif args.command == "scan":
        cmd_scan(args)
    elif args.command == "report":
        cmd_report(args)
    elif args.command == "politician":
        cmd_politician(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "podcast":
        cmd_podcast(args)
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "candidates":
        cmd_candidates(args)
    elif args.command == "pm":
        cmd_pm(args)
    elif args.command == "analyst":
        cmd_analyst(args)
    elif args.command == "backfill":
        cmd_backfill(args)
    elif args.command == "discover":
        cmd_discover(args)
    elif args.command == "deepdive":
        cmd_deepdive(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
