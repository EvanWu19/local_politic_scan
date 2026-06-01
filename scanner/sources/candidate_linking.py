"""
Candidate ↔ bill/event linking (OSS plan, item 4 value-add).

state.py already pulls bill sponsorships from OpenStates and federal.py pulls
federal sponsors; this module flags any event whose sponsor list (or text)
names a candidate the listener can actually vote for, so the pipeline can
prioritise it and auto-fire a candidate spotlight. Pure logic — no network,
fully unit-testable. Mirrors the name-match convention processor.py already
uses (`_matched_candidate`).
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z ]", " ", (s or "").lower())


def _name_tokens(name: str) -> List[str]:
    # significant tokens (drop initials / one-char) for last-name matching
    return [t for t in _norm(name).split() if len(t) > 1]


def _matches(candidate: str, hay: str) -> bool:
    """True if the candidate's full name, or their (first+last) tokens, appear
    in the haystack. Requires last name + one more token to avoid matching a
    common surname alone."""
    cand_l = _norm(candidate)
    hay_l = _norm(hay)
    if cand_l and cand_l in hay_l:
        return True
    toks = _name_tokens(candidate)
    if len(toks) >= 2:
        first, last = toks[0], toks[-1]
        return last in hay_l.split() and first in hay_l.split()
    return False


def tag_events_with_candidates(events: List[Dict],
                               candidate_names: List[str]) -> int:
    """In-place: set ev['_matched_candidate'] and ev['spotlight_candidate']=True
    on any event whose sponsors/title/description/raw_content names a tracked
    candidate. Returns the number of events tagged."""
    names = [n for n in (candidate_names or []) if n and n.strip()]
    if not names or not events:
        return 0
    tagged = 0
    for ev in events:
        if ev.get("_matched_candidate"):
            continue
        sponsors = ev.get("sponsors") or []
        hay = " ".join([
            " ".join(str(s) for s in sponsors),
            str(ev.get("title", "")),
            str(ev.get("description", "")),
            str(ev.get("raw_content", "")),
        ])
        for name in names:
            if _matches(name, hay):
                ev["_matched_candidate"] = name
                ev["spotlight_candidate"] = True
                tagged += 1
                break
    if tagged:
        log.info("candidate_linking: tagged %d/%d event(s) to tracked candidates",
                 tagged, len(events))
    return tagged
