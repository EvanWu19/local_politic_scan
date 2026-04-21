"""
State legislature source.

Primary: OpenStates API (works for every U.S. state — the jurisdiction is
driven by Config.STATE_CODE, e.g. "md", "ca", "tx").
  API key: https://openstates.org/accounts/signup/

Fallback (Maryland only, when the OpenStates key is missing): scrape the
Maryland General Assembly site. For other states the fallback returns an
empty list — set OPENSTATES_API_KEY in .env to get full coverage.

Hearings (`fetch_state_hearings`) is Maryland-specific and returns an
empty list for other states; PRs to add other jurisdictions welcome.
"""
import requests
import logging
from datetime import datetime
from typing import List, Dict, Optional
from bs4 import BeautifulSoup

from config import Config as _Cfg

log = logging.getLogger(__name__)

OPENSTATES_BASE = "https://v3.openstates.org"
MGA_BASE = "https://mgaleg.maryland.gov"

_UA = {"User-Agent": "Mozilla/5.0 (local politics scanner)"}


def _openstates_get(path: str, api_key: str, params: dict) -> Optional[dict]:
    headers = {"X-API-KEY": api_key}
    try:
        r = requests.get(f"{OPENSTATES_BASE}{path}", params=params,
                         headers=headers, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("OpenStates request failed: %s", e)
        return None


def fetch_state_bills(api_key: str, days_back: int = 7,
                      max_items: int = 30,
                      state_code: Optional[str] = None) -> List[Dict]:
    """
    Fetch recent state bills from OpenStates for the configured state.

    `state_code` defaults to Config.STATE_CODE (2-letter, lowercase).
    Falls back to the MGA scraper only when state is Maryland and no key.
    """
    sc = (state_code or _Cfg.STATE_CODE or "").lower()
    state_label = _Cfg.STATE or sc.upper() or "state"

    if not api_key:
        if sc == "md":
            log.warning("No OPENSTATES_API_KEY — falling back to MGA scraper")
            return _scrape_mga_bills(max_items)
        log.warning(
            "No OPENSTATES_API_KEY and state=%r has no fallback scraper; "
            "skipping state bills.", sc,
        )
        return []

    if not sc:
        log.warning("STATE_CODE not set in .env; skipping state bills.")
        return []

    results: List[Dict] = []
    data = _openstates_get(
        "/bills",
        api_key,
        {
            "jurisdiction": sc,
            "sort": "updated_desc",
            "per_page": max_items,
            "include": ["abstracts", "sponsorships", "actions"],
        },
    )
    if not data:
        if sc == "md":
            return _scrape_mga_bills(max_items)
        return []

    for bill in data.get("results", []):
        updated_str = bill.get("updated_at", "")[:10]
        try:
            updated = datetime.strptime(updated_str, "%Y-%m-%d").date()
        except Exception:
            updated = None

        sponsors = [s.get("name", "") for s in bill.get("sponsorships", []) if s.get("name")]
        abstracts = bill.get("abstracts", [])
        abstract_text = abstracts[0].get("abstract", "") if abstracts else ""

        latest_actions = bill.get("actions", [])
        latest_action = latest_actions[-1].get("description", "") if latest_actions else ""

        bill_id = bill.get("identifier", "")
        session = bill.get("legislative_session", "")
        openstates_url = f"https://openstates.org/{sc}/bills/{session}/{bill_id}/"

        results.append({
            "title": bill.get("title", f"{sc.upper()} Bill {bill_id}"),
            "type": "bill",
            "level": "state",
            "date": str(updated) if updated else "",
            "source_url": openstates_url,
            "source_name": f"OpenStates / {state_label}",
            "bill_number": f"{sc.upper()} {bill_id}",
            "status": latest_action,
            "description": abstract_text or f"Sponsors: {', '.join(sponsors)}. Latest: {latest_action}",
            "raw_content": f"{bill.get('title', '')} {abstract_text}",
            "categories": ["state"],
            "sponsors": sponsors,
        })

    log.info("%s: fetched %d bills via OpenStates", state_label, len(results))
    return results


def _scrape_mga_bills(max_items: int = 20) -> List[Dict]:
    """Fallback: scrape Maryland General Assembly for recent House bills."""
    results: List[Dict] = []
    url = f"{MGA_BASE}/mgawebsite/Legislation/Index/house"
    try:
        r = requests.get(url, timeout=15, headers=_UA)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        for row in soup.select("table tbody tr")[:max_items]:
            cell = row.find("td")
            if not cell:
                continue
            dl = cell.find("dl")
            if not dl:
                continue

            dts = dl.find_all("dt")
            dds = dl.find_all("dd")
            fields = {dt.get_text(strip=True): dd for dt, dd in zip(dts, dds)}

            bill_dd = fields.get("Bill/Chapter (Cross/Chapter)")
            if not bill_dd:
                continue
            link = bill_dd.find("a")
            if not link:
                continue
            bill_id = link.get_text(strip=True)
            bill_url = f"{MGA_BASE}{link.get('href', '')}"

            title_dd = fields.get("Title")
            title = title_dd.get_text(strip=True) if title_dd else ""

            sponsor_dd = fields.get("Sponsor")
            sponsor = sponsor_dd.get_text(strip=True) if sponsor_dd else ""

            if not title:
                continue

            results.append({
                "title": title[:350],
                "type": "bill",
                "level": "state",
                "date": "",
                "source_url": bill_url,
                "source_name": "Maryland General Assembly",
                "bill_number": f"MD {bill_id}",
                "status": "active",
                "description": f"Sponsor: {sponsor}" if sponsor else "",
                "raw_content": f"{bill_id} {title} {sponsor}",
                "categories": ["state"],
                "sponsors": [sponsor] if sponsor else [],
            })
    except Exception as e:
        log.warning("MGA scraper failed: %s", e)

    log.info("Maryland: scraped %d bills from MGA", len(results))
    return results


def fetch_state_hearings(api_key: str = "", max_items: int = 15,
                         state_code: Optional[str] = None) -> List[Dict]:
    """
    Fetch upcoming state legislative hearings.

    Currently implemented for Maryland only (MGA committees page). Returns
    an empty list for other states — OpenStates doesn't expose a unified
    hearings endpoint at the time of writing.
    """
    sc = (state_code or _Cfg.STATE_CODE or "").lower()
    if sc != "md":
        log.info("No hearings scraper for state=%r; skipping.", sc)
        return []

    results: List[Dict] = []
    url = f"{MGA_BASE}/mgawebsite/Committees/Index"
    try:
        r = requests.get(url, timeout=15, headers=_UA)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        for item in soup.select(".hearing-item, .agenda-item, tr")[:max_items]:
            text = item.get_text(strip=True)
            link = item.find("a")
            if not text or len(text) < 10:
                continue
            href = f"{MGA_BASE}{link.get('href', '')}" if link else url

            results.append({
                "title": text[:200],
                "type": "hearing",
                "level": "state",
                "date": "",
                "source_url": href,
                "source_name": "Maryland General Assembly Committees",
                "categories": ["state", "hearing"],
                "raw_content": text,
            })
    except Exception as e:
        log.warning("MD hearings scraper failed: %s", e)

    return results
