"""
weekly_review.py — every Sunday, audit the last seven days of digests and
push a Cowork dispatch with proposed changes for the listener to approve.

Usage
-----
    python weekly_review.py                  # write the audit + dispatch
    python weekly_review.py --dry-run        # print audit, no dispatch

Wiring
------
Register via the Cowork `schedule` skill so the listener gets a Sunday-morning
dispatch in their Cowork sidebar:

    /schedule weekly  --cron "0 8 * * SUN" \
        --command "python weekly_review.py" \
        --label   "Local-politics site review"

The audit writes:
  - reports/site_review_<YYYY-MM-DD>.md  (human-readable audit)
  - cowork_inbox/site_review_<YYYY-MM-DD>.json  (Cowork brief; Cowork agent
    surfaces it to the user via dispatch_to_user)
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import Config as _Cfg  # noqa: E402
from scanner.cowork_bridge import Brief, write_brief, INBOX_DIR  # noqa: E402
from scanner.notifications import scan_failed_briefs  # noqa: E402

log = logging.getLogger("weekly_review")

REPORTS_DIR = PROJECT_ROOT / "reports"
COWORK_INBOX = PROJECT_ROOT / "cowork_inbox"
DOSSIERS_DIR = PROJECT_ROOT / "data" / "candidate_dossiers"


# ──────────────────────────────────────────────────────────────────────────────
# Audit helpers
# ──────────────────────────────────────────────────────────────────────────────

def _recent_digest_paths(days: int = 7) -> List[Path]:
    today = date.today()
    paths = []
    for n in range(days):
        d = (today - timedelta(days=n)).isoformat()
        p = REPORTS_DIR / f"digest_{d}.md"
        if p.exists():
            paths.append(p)
    return sorted(paths)


def _find_stuck_spotlights(digests: List[Path]) -> List[Tuple[str, str]]:
    """Return (date, candidate_name) for digests that fell back to the
    'Dossier in progress' placeholder. We look at the HTML twin since the
    markdown digest doesn't render the spotlight panel."""
    stuck = []
    for md_path in digests:
        html_path = md_path.with_suffix(".html")
        if not html_path.exists():
            continue
        html = html_path.read_text(encoding="utf-8", errors="ignore")
        if "Dossier in progress" not in html:
            continue
        m = re.search(r'class="spot-name">([^<]+)</div>', html)
        cand = m.group(1).strip() if m else "Unknown"
        stuck.append((md_path.stem.replace("digest_", ""), cand))
    return stuck


def _find_zero_relevance_state_items(digests: List[Path]) -> int:
    """Count Maryland State Legislature items shown with Relevance: (0%).
    A non-zero number means the relevance model isn't tagging local impact."""
    total = 0
    for p in digests:
        text = p.read_text(encoding="utf-8", errors="ignore")
        # State section starts at the "## 🏛️" header, ends at the next "##".
        m = re.search(r"##\s+🏛️.*?(?=\n##\s|\Z)", text, flags=re.S)
        if not m:
            continue
        block = m.group(0)
        total += len(re.findall(r"Relevance:[^()]*\(0%\)", block))
    return total


def _find_recurring_tracker_rows(digests: List[Path]) -> List[Tuple[str, int]]:
    """A politician row repeats verbatim across many days = stale."""
    counter: Counter = Counter()
    for p in digests:
        text = p.read_text(encoding="utf-8", errors="ignore")
        # Each politician row in the markdown starts with **Name** (Party).
        for name_line in re.findall(r"\*\*(.+?)\*\*\s+\([^)]+\)", text):
            counter[name_line] += 1
    return [(name, n) for name, n in counter.items() if n >= 4]


def _find_empty_sections(digests: List[Path]) -> Dict[str, int]:
    """Levels that produced ZERO items on at least one recent day."""
    levels = ("Federal", "Maryland State Legislature", "Montgomery",
              "School Board", "Near You", "Local Services")
    misses = {lv: 0 for lv in levels}
    for p in digests:
        text = p.read_text(encoding="utf-8", errors="ignore")
        for lv in levels:
            # Match the section header; if it isn't there at all, count as miss.
            if lv not in text:
                misses[lv] += 1
    return {lv: n for lv, n in misses.items() if n > 0}


def _find_failed_dossier_briefs() -> List[str]:
    """Briefs whose Cowork agent run errored — left over .error.json files
    in the last 14 days."""
    cutoff = datetime.now() - timedelta(days=14)
    bad = []
    for p in COWORK_INBOX.glob("dossier_*.error.json"):
        try:
            if datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
                continue
            data = json.loads(p.read_text(encoding="utf-8"))
            cand = data.get("context", {}).get("candidate_name", p.stem)
            bad.append(cand)
        except Exception:
            bad.append(p.stem)
    return bad


def _find_recurring_references(db_path: Path, days: int = 7) -> int:
    """Count URLs that appeared in the digest references on ≥4 of the last
    `days` days. Requires the digest_references table from the migration."""
    if not db_path.exists():
        return 0
    try:
        con = sqlite3.connect(str(db_path))
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        row = con.execute(
            "SELECT COUNT(*) FROM digest_references "
            "WHERE last_appeared >= ? AND days_seen >= 4", (cutoff,)
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0
    finally:
        try:
            con.close()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Report + brief
# ──────────────────────────────────────────────────────────────────────────────

def build_review(today: date) -> Tuple[str, Dict]:
    digests = _recent_digest_paths()

    stuck_spots   = _find_stuck_spotlights(digests)
    zero_rel      = _find_zero_relevance_state_items(digests)
    recurring     = _find_recurring_tracker_rows(digests)
    misses        = _find_empty_sections(digests)
    bad_briefs    = _find_failed_dossier_briefs()
    ref_recur     = _find_recurring_references(_Cfg.DB_PATH)
    # Surface every brief that errored in the last week into the
    # notifications log + DB. Returns a count; we just record it.
    surfaced = scan_failed_briefs(within_hours=24 * 7)

    findings: List[Dict] = []

    if stuck_spots:
        findings.append({
            "id": "spotlight_stuck",
            "title": f"{len(stuck_spots)} candidate spotlight(s) never refreshed",
            "detail": ", ".join(f"{d}: {n}" for d, n in stuck_spots[:5]),
            "proposed_fix":
                "Run `python main.py dossier --retry-failed` and "
                "re-render the affected days with `python main.py reports refresh`.",
            "auto_applicable": True,
        })

    if zero_rel >= 5:
        findings.append({
            "id": "state_relevance_zero",
            "title": f"{zero_rel} state-leg items tagged at 0% relevance",
            "detail":
                "The Opus relevance prompt isn't recognising Rockville-local "
                "impact in state stories. Tighten the prompt in "
                "scanner/processor.py to require a 'local_impact' factor "
                "and pass FEDERAL_KEYWORDS + 20853 in the system prompt.",
            "proposed_fix":
                "Patch processor._build_relevance_prompt to inject "
                "config_local.FEDERAL_KEYWORDS and zip code; re-score "
                "the last 7 days.",
            "auto_applicable": True,
        })

    if recurring:
        names = ", ".join(f"{n}×{c}" for n, c in recurring[:5])
        findings.append({
            "id": "tracker_stale",
            "title": f"Tracker rows repeating across days: {names}",
            "detail":
                "Politician Tracker shows accumulated events, not new-since-"
                "yesterday. Already addressed by the reporter.py patch "
                "introduced last week; verify the date filter is live.",
            "proposed_fix":
                "Confirm `_politician_tracker_html` filters `events` to "
                "first_seen >= today-1d. If not, apply the diff in the "
                "improvement plan.",
            "auto_applicable": False,
        })

    if misses:
        miss_txt = ", ".join(f"{k}: missed {v} day(s)" for k, v in misses.items())
        findings.append({
            "id": "sections_empty",
            "title": "Sections went empty on some days",
            "detail": miss_txt,
            "proposed_fix":
                "Check scanner/sources/local_hearings.py for upstream HTML "
                "changes; an empty 'Near You' section usually means "
                "rockvillemd.gov's RSS category ID changed.",
            "auto_applicable": False,
        })

    if bad_briefs:
        findings.append({
            "id": "dossier_errors",
            "title": f"{len(bad_briefs)} dossier brief(s) in error state",
            "detail": ", ".join(bad_briefs[:6]),
            "proposed_fix":
                "Run `python main.py dossier --retry-failed` to requeue with "
                "the gap-filling instructions (Opus 4.7 will resolve "
                "office/party/district during step 1 instead of refusing).",
            "auto_applicable": True,
        })

    if ref_recur:
        findings.append({
            "id": "references_recurring",
            "title": f"{ref_recur} reference URLs have appeared ≥4 days running",
            "detail":
                "References look static because the same URLs keep being "
                "pulled by the scanners. Move them into a 'Seen earlier "
                "this week' collapsible section.",
            "proposed_fix":
                "Confirm `_references_section_html` is rendering the "
                "'New today' / 'Seen earlier' split. If markdown digest "
                "doesn't reflect this yet, patch is in reporter.py.",
            "auto_applicable": True,
        })

    if not findings:
        findings.append({
            "id": "all_clear",
            "title": "Nothing to fix this week — site looks healthy.",
            "detail":
                f"Reviewed {len(digests)} digests since "
                f"{(today - timedelta(days=7)).isoformat()}.",
            "proposed_fix": "No action needed.",
            "auto_applicable": False,
        })

    # Markdown report
    lines = [
        f"# Local Politics Scanner — Weekly Site Review",
        f"_Generated {today.isoformat()} · audit window {len(digests)} day(s)_",
        "",
        f"## Findings ({len(findings)})",
        "",
    ]
    for f in findings:
        lines.append(f"### {f['title']}")
        lines.append("")
        lines.append(f"**Detail.** {f['detail']}")
        lines.append("")
        lines.append(f"**Proposed fix.** {f['proposed_fix']}")
        lines.append(f"*Auto-applicable:* {'yes' if f['auto_applicable'] else 'manual review needed'}")
        lines.append("")

    md = "\n".join(lines)
    summary = {
        "findings_count": len(findings),
        "auto_applicable_count": sum(1 for f in findings if f["auto_applicable"]),
        "findings": findings,
    }
    return md, summary


def write_review_and_dispatch(today: date, *, dry_run: bool) -> Path:
    md, summary = build_review(today)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"site_review_{today.isoformat()}.md"
    report_path.write_text(md, encoding="utf-8")
    log.info("weekly_review: wrote audit to %s", report_path)

    if dry_run:
        print(md)
        return report_path

    # Build a Cowork brief asking the agent to surface this to the listener.
    brief = Brief(
        brief_id=f"site_review_{today.isoformat()}",
        type="weekly_site_review",
        output_file=str(COWORK_INBOX / f"site_review_{today.isoformat()}.applied.md"),
        instructions=(
            "You are running a weekly review of the local-politics scanner. "
            "Read the markdown audit in `reports/site_review_<date>.md`, then "
            "use `dispatch_to_user` to ask the listener to approve each "
            "proposed change inline. For findings marked auto_applicable=true, "
            "if the listener approves, run the suggested shell command and "
            "write a brief audit of what changed to `output_file`. For "
            "manual-review items, ask the listener for clarification. Use "
            "Opus 4.7 for any reasoning. NEVER apply a change without "
            "explicit user approval."
        ),
        context={
            "review_date": today.isoformat(),
            "audit_path": str(report_path),
            **summary,
        },
    )
    inbox_path = write_brief(brief)
    log.info("weekly_review: dispatched brief to %s", inbox_path)
    return report_path


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Weekly site review for the scanner.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the audit; do not dispatch via Cowork.")
    p.add_argument("--date", default=None,
                   help="Override 'today' (YYYY-MM-DD), useful for backfill.")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    today = (
        datetime.strptime(args.date, "%Y-%m-%d").date()
        if args.date else date.today()
    )
    write_review_and_dispatch(today, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
