"""
Ballot helper — renders the listener's confirmed ballot candidates into
a compact prompt block the Author injects into every podcast episode.

The block answers the listener's core question ("who are my choices?")
up front so the show can frame each race as "Candidate A vs Candidate B"
rather than telling the listener to go look it up themselves.

The block is derived from `politicians` rows with `ballot_year` set —
populated by `scanner.sources.candidate_discover` + the seed list.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


def build_ballot_block(db_path: Path, ballot_year: int,
                       max_per_contest: int = 6) -> str:
    """
    Render a compact, prompt-friendly block listing every known candidate
    by contest. Returns an empty string if nothing is on file for the
    given ballot year.

    Output shape:

        YOUR BALLOT (2026) — confirmed/likely candidates for your districts.
        The listener's choices are BETWEEN these people; never tell them
        to go figure out who their candidate is.

          • Montgomery County Council District 3
              - Sidney Katz (D) — candidate
              - Jane Doe (R) — candidate
          • MD House of Delegates District 15
              - Some Name (D) — candidate
              ...
    """
    from scanner.database import list_ballot_candidates
    rows = list_ballot_candidates(db_path, ballot_year=ballot_year)
    if not rows:
        return ""

    grouped: Dict[str, List[Dict]] = {}
    for r in rows:
        office = (r.get("office") or "(unknown office)").strip()
        grouped.setdefault(office, []).append(r)

    lines: List[str] = [
        f"YOUR BALLOT ({ballot_year}) — confirmed/likely candidates for the "
        "listener's districts. The listener's choice is BETWEEN these named "
        "people; never tell the listener to go figure out who their candidate "
        "is. If a contest has fewer than two known candidates, say so out "
        "loud (\"only one filer so far\") instead of fudging."
    ]
    for office in sorted(grouped.keys()):
        lines.append(f"  • {office}")
        people = grouped[office][:max_per_contest]
        for p in people:
            name = (p.get("name") or "").strip() or "(unnamed)"
            party = (p.get("party") or "unknown").strip() or "unknown"
            status = (p.get("candidate_status") or "candidate").strip() or "candidate"
            suffix = "" if status == "candidate" else f" [{status}]"
            lines.append(f"      - {name} ({party}){suffix}")
        extra = len(grouped[office]) - len(people)
        if extra > 0:
            lines.append(f"      - …and {extra} more")
    return "\n".join(lines)


def candidate_names_for_match(db_path: Path,
                              ballot_year: Optional[int] = None) -> List[str]:
    """
    Return a lowercase-stripped list of candidate names to match against
    listener note text. Used by PM to extract `listener_candidate_interest`.
    """
    from scanner.database import list_ballot_candidates
    rows = list_ballot_candidates(db_path, ballot_year=ballot_year)
    return [(r.get("name") or "").strip() for r in rows
            if (r.get("name") or "").strip()]
