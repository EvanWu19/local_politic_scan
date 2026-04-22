"""
Candidate discovery — actively identifies who is on the listener's
ballot for each district configured in .env, so the Author can frame
every race as "your choice is between X and Y" rather than asking the
listener to figure out their own candidates.

For each ballot contest (derived from Config.districts_profile() + the
state/county context), we:
  1. Build a Google News RSS query scoped to that contest
     (e.g. `"Maryland" "District 15" "House of Delegates" 2026 candidate`)
  2. Fetch headlines+snippets (no API key required)
  3. Hand the corpus to Claude (haiku — cost-sensitive) with a strict
     JSON extraction prompt that returns candidate records
  4. UPSERT each record into `politicians` via `upsert_candidate()`
     with `ballot_year` set and `discovered_via="ai_discovery"`

We deliberately keep this bounded — one Claude call per contest, one
RSS fetch per contest — and run on-demand (not every scan), because
candidate rosters change slowly.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import anthropic
import feedparser

from config import Config as _Cfg

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_HEADLINES_PER_CONTEST = 30
GOOGLE_NEWS_WINDOW = "1y"

DISCOVERY_SYSTEM = """You are a research assistant helping a first-time voter
identify who will be on their ballot for specific contests.

You will receive:
  • A single ballot contest (office, jurisdiction, district, ballot year)
  • Up to 30 recent news headlines + snippets that mention that contest

Your job: extract every named candidate who appears to be running (declared,
filed, nominated, or confirmed on the ballot) for THIS specific contest.

RULES:
  1. Only return people who are clearly running for the contest described —
     not incumbents being written about for unrelated reasons, not donors,
     not endorsers, not pundits.
  2. If the news is ambiguous about whether someone is actually running
     (vs. "rumored to consider"), mark candidate_status as "potential"
     instead of "candidate".
  3. Normalize party to one of: D, R, I, G, L, Nonpartisan, unknown.
     Use "unknown" if you truly cannot tell.
  4. Prefer full legal names as they appear most often in the headlines.
  5. Do NOT invent people. If the headlines don't mention anyone running
     for this contest, return an empty list.

OUTPUT FORMAT — strict JSON, one object, no preamble, no markdown fences:

{
  "candidates": [
    {
      "name": "<full name>",
      "party": "D|R|I|G|L|Nonpartisan|unknown",
      "candidate_status": "candidate|potential|withdrawn",
      "evidence": "<one-sentence quote or paraphrase from the headlines>"
    }
  ]
}

If nothing qualifies, return {"candidates": []}.
"""


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def discover_all(db_path: Path, anthropic_key: str,
                 ballot_year: int = 2026,
                 model: str = DEFAULT_MODEL,
                 max_headlines: int = MAX_HEADLINES_PER_CONTEST,
                 window: str = GOOGLE_NEWS_WINDOW) -> List[Dict]:
    """
    Run discovery for every contest derived from the configured districts.
    Returns a list of {contest, candidates_found, candidates_saved} dicts.
    """
    if not anthropic_key:
        log.warning("Candidate discovery: no anthropic_key, skipping")
        return []

    contests = _build_contests(ballot_year)
    if not contests:
        log.info("Candidate discovery: no contests derivable from config; "
                 "set district fields in .env")
        return []

    client = anthropic.Anthropic(api_key=anthropic_key)
    runs: List[Dict] = []
    for contest in contests:
        try:
            run = _discover_contest(
                db_path=db_path, client=client, model=model,
                contest=contest, ballot_year=ballot_year,
                max_headlines=max_headlines, window=window,
            )
        except Exception as e:
            log.exception("Discovery failed for %s", contest.get("office"))
            run = {
                "contest": contest,
                "candidates_found": 0,
                "candidates_saved": 0,
                "error": str(e),
            }
        runs.append(run)
        log.info("Discovery %s: %d found / %d saved",
                 contest.get("office"),
                 run.get("candidates_found", 0),
                 run.get("candidates_saved", 0))
    return runs


# ──────────────────────────────────────────────────────────────────────────────
# Contest derivation from config
# ──────────────────────────────────────────────────────────────────────────────

def _build_contests(ballot_year: int) -> List[Dict]:
    """
    Produce one contest dict per ballot race the listener votes on.
    Skips fields that are unconfigured in .env. Each dict has keys:
      office, jurisdiction, district, level, query_terms
    """
    state = _Cfg.STATE or ""
    state_code = (_Cfg.STATE_CODE or "").upper()
    county_full = _Cfg.COUNTY or ""
    # "Montgomery County" → "Montgomery" for labels we append "County X" to
    county = re.sub(r"\s+County$", "", county_full, flags=re.IGNORECASE) or county_full
    contests: List[Dict] = []

    def add(office: str, level: str, district: str, query_terms: List[str]):
        contests.append({
            "office": office,
            "jurisdiction": state if level in ("federal", "state") else county or state,
            "district": district or "",
            "level": level,
            "query_terms": query_terms,
        })

    # Federal
    if _Cfg.US_HOUSE_DISTRICT:
        district_token = _Cfg.US_HOUSE_DISTRICT
        # Accept either "8" or already-formatted "MD-8"; normalize to {code}-{n}
        if "-" not in district_token and state_code:
            district_token = f"{state_code}-{district_token}"
        add(
            office=f"U.S. House {state_code or state} District {district_token}",
            level="federal",
            district=district_token,
            query_terms=[
                f'"{state}"', f'"{district_token}"',
                '"U.S. House"', "candidate",
            ],
        )
    # State Senate
    if _Cfg.STATE_SENATE_DISTRICT:
        add(
            office=f"{state} State Senate District {_Cfg.STATE_SENATE_DISTRICT}",
            level="state",
            district=_Cfg.STATE_SENATE_DISTRICT,
            query_terms=[
                f'"{state}"', f'"District {_Cfg.STATE_SENATE_DISTRICT}"',
                '"State Senate"', "candidate",
            ],
        )
    # State House (MD: House of Delegates)
    if _Cfg.STATE_HOUSE_DISTRICT:
        delegate_label = "House of Delegates" if state.lower() == "maryland" else "State House"
        add(
            office=f"{state} {delegate_label} District {_Cfg.STATE_HOUSE_DISTRICT}",
            level="state",
            district=_Cfg.STATE_HOUSE_DISTRICT,
            query_terms=[
                f'"{state}"', f'"District {_Cfg.STATE_HOUSE_DISTRICT}"',
                f'"{delegate_label}"', "candidate",
            ],
        )
    # County Council
    council_district = _Cfg.COUNCILMANIC_DISTRICT or _Cfg.COUNTY_COUNCIL_DISTRICT
    if council_district and county:
        add(
            office=f"{county} County Council District {council_district}",
            level="county",
            district=council_district,
            query_terms=[
                f'"{county_full}"', f'"District {council_district}"',
                '"County Council"', "candidate",
            ],
        )
    # Circuit Court
    if _Cfg.CIRCUIT_COURT_DISTRICT:
        add(
            office=f"{state} Circuit Court District {_Cfg.CIRCUIT_COURT_DISTRICT}",
            level="state",
            district=_Cfg.CIRCUIT_COURT_DISTRICT,
            query_terms=[
                f'"{state}"', f'"Circuit {_Cfg.CIRCUIT_COURT_DISTRICT}"',
                '"Circuit Court"', "judge", "candidate",
            ],
        )
    # Board of Education
    if _Cfg.SCHOOL_DISTRICT and county:
        add(
            office=f"{county} Board of Education",
            level="school",
            district=_Cfg.SCHOOL_DISTRICT,
            query_terms=[
                f'"{county_full}"', '"Board of Education"', "school board", "candidate",
            ],
        )
    # County-wide offices (County Executive etc.) — only add if we have a county
    if county:
        add(
            office=f"{county} County Executive",
            level="county",
            district=county,
            query_terms=[
                f'"{county_full}"', '"County Executive"', "candidate",
            ],
        )
    # Governor / statewide (every 4 years; we leave this to the prompt window
    # to filter by actually returning no results if not applicable)
    if state:
        add(
            office=f"Governor of {state}",
            level="state",
            district=state_code or state,
            query_terms=[
                f'"{state}"', "Governor", "candidate", str(ballot_year),
            ],
        )
    return contests


# ──────────────────────────────────────────────────────────────────────────────
# Per-contest run
# ──────────────────────────────────────────────────────────────────────────────

def _discover_contest(db_path: Path, client: anthropic.Anthropic, model: str,
                      contest: Dict, ballot_year: int,
                      max_headlines: int, window: str) -> Dict:
    headlines = _fetch_contest_headlines(
        contest=contest, window=window, max_items=max_headlines,
        ballot_year=ballot_year,
    )
    if not headlines:
        return {
            "contest": contest,
            "candidates_found": 0,
            "candidates_saved": 0,
        }

    user_prompt = _build_discovery_prompt(contest, ballot_year, headlines)
    resp = client.messages.create(
        model=model,
        max_tokens=1500,
        system=DISCOVERY_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = resp.content[0].text.strip()
    candidates = _parse_candidates(raw)

    from scanner.database import upsert_candidate
    saved = 0
    for c in candidates:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        upsert_candidate(
            db_path=db_path,
            name=name,
            office=contest["office"],
            party=_normalize_party(c.get("party") or "unknown"),
            level=contest["level"],
            district=contest.get("district") or "",
            ballot_year=ballot_year,
            candidate_status=(c.get("candidate_status") or "candidate").strip(),
            discovered_via="ai_discovery",
        )
        saved += 1
    return {
        "contest": contest,
        "candidates_found": len(candidates),
        "candidates_saved": saved,
    }


def _build_discovery_prompt(contest: Dict, ballot_year: int,
                            headlines: List[Dict]) -> str:
    parts = [
        f"BALLOT CONTEST: {contest['office']}",
        f"JURISDICTION: {contest.get('jurisdiction', '')}",
        f"DISTRICT: {contest.get('district', '')}",
        f"BALLOT YEAR: {ballot_year}",
        "",
        f"HEADLINES ({len(headlines)} items, most recent first):",
    ]
    for h in headlines:
        date_s = h.get("date") or "undated"
        snippet = (h.get("summary") or "").strip()
        if snippet and len(snippet) > 240:
            snippet = snippet[:240] + "…"
        parts.append(f"- [{date_s}] {h['title']}")
        if snippet:
            parts.append(f"    {snippet}")
    parts += [
        "",
        "Extract the candidates running for this specific contest. Return JSON.",
    ]
    return "\n".join(parts)


# ──────────────────────────────────────────────────────────────────────────────
# Google News RSS (contest-scoped search)
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_contest_headlines(contest: Dict, window: str,
                             max_items: int, ballot_year: int) -> List[Dict]:
    url = _build_contest_url(contest, window, ballot_year)
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        log.warning("Contest feed parse failed (%s): %s",
                    contest.get("office"), e)
        return []
    if feed.bozo and not feed.entries:
        return []

    out: List[Dict] = []
    for entry in feed.entries[:max_items]:
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        summary = (entry.get("summary") or entry.get("description") or "").strip()
        pub = ""
        pp = entry.get("published_parsed")
        if pp:
            try:
                pub = datetime(*pp[:6]).strftime("%Y-%m-%d")
            except Exception:
                pub = ""
        out.append({
            "title": title,
            "summary": _strip_html(summary),
            "date": pub,
        })
    return out


def _build_contest_url(contest: Dict, window: str, ballot_year: int) -> str:
    terms = list(contest.get("query_terms") or [])
    if str(ballot_year) not in " ".join(terms):
        terms.append(str(ballot_year))
    if window and window[-1] in ("d", "m", "y") and window[:-1].isdigit():
        terms.append(f"when:{window}")
    qs = urllib.parse.urlencode({
        "q": " ".join(terms),
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    })
    return f"https://news.google.com/rss/search?{qs}"


# ──────────────────────────────────────────────────────────────────────────────
# Parsing + normalization
# ──────────────────────────────────────────────────────────────────────────────

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?", re.IGNORECASE)
_JSON_TAIL_FENCE_RE = re.compile(r"\n?```\s*$")


def _parse_candidates(raw: str) -> List[Dict]:
    if not raw:
        return []
    text = _JSON_FENCE_RE.sub("", raw.strip())
    text = _JSON_TAIL_FENCE_RE.sub("", text).strip()
    try:
        obj = json.loads(text)
    except Exception:
        log.warning("Discovery output was not JSON; raw=%r", raw[:200])
        return []
    cands = obj.get("candidates") if isinstance(obj, dict) else None
    if not isinstance(cands, list):
        return []
    return [c for c in cands if isinstance(c, dict)]


_PARTY_MAP = {
    "d": "D", "dem": "D", "democrat": "D", "democratic": "D",
    "r": "R", "rep": "R", "republican": "R",
    "i": "I", "ind": "I", "independent": "I",
    "g": "G", "green": "G",
    "l": "L", "libertarian": "L",
    "n": "Nonpartisan", "np": "Nonpartisan", "nonpartisan": "Nonpartisan",
}


def _normalize_party(p: str) -> str:
    if not p:
        return "unknown"
    key = p.strip().lower().rstrip(".")
    return _PARTY_MAP.get(key, p.strip() or "unknown")


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "").strip()
