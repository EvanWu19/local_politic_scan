"""
Maryland state source: OpenStates API + Maryland General Assembly scraper.
Fetches current MD bills and hearing schedules.
OpenStates API key: https://openstates.org/accounts/signup/
"""
import requests
import logging
from datetime import datetime
from typing import List, Dict, Optional
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

OPENSTATES_BASE = "https://v3.openstates.org"
MGA_BASE = "https://mgaleg.maryland.gov"


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


def fetch_md_bills(api_key: str, days_back: int = 7,
                   max_items: int = 30) -> List[Dict]:
    """
    Fetch recent Maryland state bills from OpenStates.
    All MD bills are fetched (unlike federal, no keyword filter needed).
    """
    if not api_key:
        log.warning("No OPENSTATES_API_KEY — falling back to MGA scraper")
        return _scrape_mga_bills(max_items)

    results: List[Dict] = []
    data = _openstates_get(
        "/bills",
        api_key,
        {
            "jurisdiction": "md",
            "sort": "updated_desc",
            "per_page": max_items,
            "include": ["abstracts", "sponsorships", "actions"],
        },
    )
    if not data:
        return _scrape_mga_bills(max_items)

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
        openstates_url = f"https://openstates.org/md/bills/{session}/{bill_id}/"

        results.append({
            "title": bill.get("title", f"MD Bill {bill_id}"),
            "type": "bill",
            "level": "state",
            "date": str(updated) if updated else "",
            "source_url": openstates_url,
            "source_name": "OpenStates / Maryland General Assembly",
            "bill_number": f"MD {bill_id}",
            "status": latest_action,
            "description": abstract_text or f"Sponsors: {', '.join(sponsors)}. Latest: {latest_action}",
            "raw_content": f"{bill.get('title', '')} {abstract_text}",
            "categories": ["state"],
            "sponsors": sponsors,
        })

    log.info("Maryland: fetched %d bills via OpenStates", len(results))
    return results


def _scrape_mga_bills(max_items: int = 20) -> List[Dict]:
    """
    Fallback: scrape Maryland General Assembly for recent House bills.
    Each <tbody tr> has a single <td> with a <dl class='row'> inside containing
    bill number, title, and sponsor as <dt>/<dd> pairs.
    """
    results: List[Dict] = []
    MGA_BASE = "https://mgaleg.maryland.gov"
    url = f"{MGA_BASE}/mgawebsite/Legislation/Index/house"
    try:
        r = requests.get(url, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0 (local politics scanner)"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        for row in soup.select("table tbody tr")[:max_items]:
            cell = row.find("td")
            if not cell:
                continue
            dl = cell.find("dl")
            if not dl:
                continue

            # Build a dict from dt→dd pairs
            dts = dl.find_all("dt")
            dds = dl.find_all("dd")
            fields = {dt.get_text(strip=True): dd for dt, dd in zip(dts, dds)}

            # Bill number + URL
            bill_dd = fields.get("Bill/Chapter (Cross/Chapter)")
            if not bill_dd:
                continue
            link = bill_dd.find("a")
            if not link:
                continue
            bill_id = link.get_text(strip=True)
            bill_url = f"{MGA_BASE}{link.get('href', '')}"

            # Title
            title_dd = fields.get("Title")
            title = title_dd.get_text(strip=True) if title_dd else ""

            # Sponsor
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


def fetch_md_hearings(api_key: str = "", max_items: int = 15) -> List[Dict]:
    """Fetch upcoming Maryland legislative hearings."""
    results: List[Dict] = []
    url = "https://mgaleg.maryland.gov/mgawebsite/Committees/Index"
    try:
        r = requests.get(url, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0 (local politics scanner)"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        # Grab committee hearing schedule entries
        for item in soup.select(".hearing-item, .agenda-item, tr")[:max_items]:
            text = item.get_text(strip=True)
            link = item.find("a")
            if not text or len(text) < 10:
                continue
            href = f"https://mgaleg.maryland.gov{link.get('href', '')}" if link else url

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
