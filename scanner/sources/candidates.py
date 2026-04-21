"""
Candidate tracker (2026 election cycle — Maryland / Montgomery County seed).

Seeds known candidates, joins against DB events, and provides content
for podcast Episode 4 ("Week in Review / Candidate Tracker"). Edit the
CANDIDATES list below (or override in config_local.py) for your own races.
"""
from pathlib import Path
from typing import List, Dict, Optional

# ── Seed data ─────────────────────────────────────────────────────────────────
# 2026 cycle starter data for Maryland / Montgomery County races.
# Replace/extend for your own locale.
CANDIDATES: List[Dict] = [
    # Federal
    {
        "name": "Angela Alsobrooks",
        "office": "U.S. Senate MD",
        "party": "D",
        "level": "federal",
        "district": "MD",
        "ballotpedia": "https://ballotpedia.org/Angela_Alsobrooks",
    },
    {
        "name": "Larry Hogan",
        "office": "U.S. Senate MD",
        "party": "R",
        "level": "federal",
        "district": "MD",
        "ballotpedia": "https://ballotpedia.org/Larry_Hogan",
    },
    # Maryland State
    {
        "name": "Wes Moore",
        "office": "Governor of Maryland",
        "party": "D",
        "level": "state",
        "district": "MD",
        "ballotpedia": "https://ballotpedia.org/Wes_Moore",
    },
    # Montgomery County (2026 ballot)
    {
        "name": "Marc Elrich",
        "office": "Montgomery County Executive",
        "party": "D",
        "level": "county",
        "district": "Montgomery",
        "ballotpedia": "https://ballotpedia.org/Marc_Elrich",
    },
    # State House delegates — example districts (replace for your area)
    {
        "name": "TBD — MD House District 15A",
        "office": "MD House of Delegates, District 15A",
        "party": "?",
        "level": "state",
        "district": "15A",
        "ballotpedia": "",
        "placeholder": True,
    },
    {
        "name": "TBD — MD House District 15B",
        "office": "MD House of Delegates, District 15B",
        "party": "?",
        "level": "state",
        "district": "15B",
        "ballotpedia": "",
        "placeholder": True,
    },
    # County Council — example district (replace for your area)
    {
        "name": "Sidney Katz",
        "office": "Montgomery County Council, District 3",
        "party": "D",
        "level": "county",
        "district": "3",
        "ballotpedia": "https://ballotpedia.org/Sidney_Katz",
    },
]


def get_candidates(include_placeholders: bool = False) -> List[Dict]:
    """Return the candidate list, optionally filtering out TBD placeholders."""
    if include_placeholders:
        return CANDIDATES
    return [c for c in CANDIDATES if not c.get("placeholder")]


def refresh_candidates(db_path: Path) -> List[Dict]:
    """
    Enrich each candidate with recent events from the DB.
    Returns list of candidate dicts with 'recent_events' and 'event_count' added.
    """
    from scanner.database import get_connection

    results: List[Dict] = []
    with get_connection(db_path) as conn:
        for candidate in get_candidates():
            name = candidate["name"]
            # Match on first + last name tokens to handle partial DB entries
            parts = name.split()
            if len(parts) >= 2:
                search = f"%{parts[0]}%{parts[-1]}%"
            else:
                search = f"%{name}%"

            rows = conn.execute(
                """
                SELECT e.title, e.date, e.source_url, pe.role, pe.stance,
                       e.summary, e.relevance_score
                FROM politician_events pe
                JOIN events e ON pe.event_id = e.id
                JOIN politicians p ON pe.politician_id = p.id
                WHERE p.name LIKE ?
                ORDER BY e.date DESC
                LIMIT 10
                """,
                (search,),
            ).fetchall()

            recent = [dict(r) for r in rows]
            results.append({
                **candidate,
                "recent_events": recent,
                "event_count": len(recent),
            })

    return results


def get_candidate_episode_content(db_path: Path) -> str:
    """
    Build a content string summarizing candidate activity for use in
    podcast Episode 4 or the Claude analysis system prompt.
    """
    candidates = refresh_candidates(db_path)
    lines = ["=== Candidate Tracker — Where they stand ===\n"]

    for c in candidates:
        lines.append(f"\n{c['name']} ({c['party']}) — {c['office']}")
        events = c.get("recent_events", [])
        if events:
            for ev in events[:3]:
                role = (ev.get("role") or "mentioned").replace("_", " ").title()
                title = (ev.get("title") or "")[:80]
                date_s = ev.get("date") or ""
                lines.append(f"  [{role}] {date_s}: {title}")
        else:
            lines.append("  No tracked events in database yet.")

    placeholders = [c for c in CANDIDATES if c.get("placeholder")]
    if placeholders:
        lines.append(
            "\n⚠️  Placeholder candidates — update once 2026 filing is confirmed:"
        )
        for c in placeholders:
            lines.append(f"  - {c['office']}")

    return "\n".join(lines)


def print_candidates_table(db_path: Path) -> None:
    """Print a formatted summary table of all candidates and their DB activity."""
    candidates = refresh_candidates(db_path)

    col_w = [28, 35, 8, 40]
    header = (
        f"{'Candidate':<{col_w[0]}} "
        f"{'Office':<{col_w[1]}} "
        f"{'Events':<{col_w[2]}} "
        f"{'Latest Activity'}"
    )
    print(f"\n{header}")
    print("─" * (sum(col_w) + 3))

    for c in candidates:
        name = c["name"]
        office = c["office"]
        count = c["event_count"]
        if c.get("placeholder"):
            latest = "(placeholder — needs manual update)"
        elif c["recent_events"]:
            latest = (c["recent_events"][0].get("title") or "")[:col_w[3]]
        else:
            latest = "No events tracked yet"

        print(
            f"{name:<{col_w[0]}} "
            f"{office:<{col_w[1]}} "
            f"{count:<{col_w[2]}} "
            f"{latest}"
        )

    print()
    placeholders = [c for c in CANDIDATES if c.get("placeholder")]
    if placeholders:
        print(
            f"⚠️  {len(placeholders)} placeholder(s) need updating "
            f"after 2026 filing deadlines.\n"
            f"   Edit scanner/sources/candidates.py → CANDIDATES list.\n"
        )
