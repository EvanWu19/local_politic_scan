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
# SCAN
# ──────────────────────────────────────────────────────────────────────────────

def cmd_scan(args):
    """Run a full scan: fetch all sources → AI enrich → save → report."""
    print("\n── Local Politics Scanner ──────────────────────────────")
    print(f"   Location : {cfg.CITY}, {cfg.COUNTY}, {cfg.STATE}")
    print(f"   Date     : {date.today()}")
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

    # ── Generate report
    cmd_report(args, silent=True)
    print("✅  Scan complete.\n")

    # ── Optional: generate today's podcast
    if getattr(args, "with_podcast", False):
        cmd_podcast(args)


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


def cmd_setup(args):
    """Register Windows scheduled tasks: daily scan + (optional) always-on web server."""
    python_exe = sys.executable
    script = str(Path(__file__).resolve())
    log_file = str(Path(__file__).parent / "scan.log")
    server_log = str(Path(__file__).parent / "server.log")

    # ── Task 1: daily scan at 7 AM ──
    scan_task = "LocalPoliticsScan"
    scan_cmd = (
        f'schtasks /create /tn "{scan_task}" /tr '
        f'"{python_exe} {script} scan >> {log_file} 2>&1" '
        f'/sc daily /st 07:00 /f'
    )
    print(f"[1/2] Creating daily scan task '{scan_task}'…")
    r = subprocess.run(scan_cmd, shell=True, capture_output=True, text=True)
    if r.returncode == 0:
        print(f"      ✅  Runs daily at 07:00 · logs → {log_file}")
    else:
        print(f"      ✗  Failed: {r.stderr}")

    # ── Task 2: web server auto-start via Startup folder (no admin needed) ──
    if args.no_server:
        print("\n(Skipping web server auto-start — run `python main.py serve` manually.)")
        return

    startup_dir = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
    launcher_bat = startup_dir / "LocalPoliticsServer.bat"
    print(f"\n[2/2] Creating auto-start launcher at:\n      {launcher_bat}")

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

    print(f"\nTo remove the daily-scan task later:")
    print(f"  schtasks /delete /tn {scan_task} /f")


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Local Politics Scanner",
    )
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="Fetch all sources, enrich with AI, save + generate report")
    scan.add_argument("--with-podcast", action="store_true",
                      help="Also generate today's 2-hour podcast after the scan completes")

    pod = sub.add_parser("podcast", help="Generate a 2-hour two-host podcast from recent digest")
    pod.add_argument("--date", help="Target date YYYY-MM-DD (default: today)")
    pod.add_argument("--no-audio", action="store_true",
                     help="Write the script to .txt but skip OpenAI TTS (no audio cost)")

    rep = sub.add_parser("report", help="Generate report from existing DB data")
    rep.add_argument("--days", type=int, default=7, help="Days of history to include (default: 7)")

    pol = sub.add_parser("politician", help="Look up a politician's recent tracked activity")
    pol.add_argument("name", nargs="+", help="Politician name (can be partial)")

    sub.add_parser("status", help="Show scan history and database stats")

    sub.add_parser("candidates", help="Show candidate tracker (2026 cycle) with recent DB activity")

    srv = sub.add_parser("serve", help="Start HTTP server for phone/Tailscale access")
    srv.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    srv.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")

    setup = sub.add_parser("setup", help="Register daily-scan + web-server scheduled tasks")
    setup.add_argument("--port", type=int, default=8080, help="Server port (default: 8080)")
    setup.add_argument("--no-server", action="store_true",
                       help="Only schedule the daily scan, not the web server")

    args = parser.parse_args()

    if args.command == "scan":
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
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
