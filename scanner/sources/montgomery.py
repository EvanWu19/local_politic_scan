"""
Montgomery County, Maryland source: county government press releases,
police/fire news, MCPS board of education.

All URLs verified live against the real Montgomery County websites.
"""
import re
import requests
import logging
from datetime import datetime
from typing import List, Dict, Optional
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (local politics scanner)"}

# Base for the county portal press-list pages
_PORTAL = "https://www2.montgomerycountymd.gov/mcgportalapps"


def _get(url: str) -> Optional[BeautifulSoup]:
    try:
        r = requests.get(url, timeout=15, headers=HEADERS)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        return None


def _parse_portal_table(soup: BeautifulSoup, base_url: str,
                         source_name: str, level: str,
                         categories: List[str], max_items: int) -> List[Dict]:
    """
    Parse the standard Montgomery County portal press-list table.
    Rows have two <td>: date | title-with-link
    """
    results = []
    for tr in soup.select("table tr")[:max_items + 5]:
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        date_str = tds[0].get_text(strip=True)
        link = tds[1].find("a")
        if not link:
            continue
        title = link.get_text(strip=True)
        href = link.get("href", "")
        if not href:
            continue
        # Relative → absolute
        if not href.startswith("http"):
            href = f"{base_url}/{href.lstrip('/')}"
        if not title or len(title) < 8:
            continue

        results.append({
            "title": title[:350],
            "type": _classify_type(title),
            "level": level,
            "date": _parse_date(date_str),
            "source_url": href,
            "source_name": source_name,
            "categories": categories,
            "raw_content": title,
        })
        if len(results) >= max_items:
            break
    return results


def fetch_county_council(max_items: int = 20) -> List[Dict]:
    """
    Scrape Montgomery County Council press releases.
    URL: https://www2.montgomerycountymd.gov/mcgportalapps/press_List.aspx?id=01
    """
    url = f"{_PORTAL}/press_List.aspx?id=01"
    soup = _get(url)
    if not soup:
        return []
    results = _parse_portal_table(
        soup,
        base_url=_PORTAL,
        source_name="Montgomery County Council",
        level="county",
        categories=["county"],
        max_items=max_items,
    )
    log.info("County council: %d press releases", len(results))
    return results


def fetch_county_executive(max_items: int = 15) -> List[Dict]:
    """
    Scrape Montgomery County Executive press releases (all departments).
    URL: https://www2.montgomerycountymd.gov/mcgportalapps/press_List.aspx?id=all
    """
    url = f"{_PORTAL}/press_List.aspx?id=all"
    soup = _get(url)
    if not soup:
        return []
    results = _parse_portal_table(
        soup,
        base_url=_PORTAL,
        source_name="Montgomery County Executive",
        level="county",
        categories=["county"],
        max_items=max_items,
    )
    log.info("County executive: %d press releases", len(results))
    return results


def fetch_county_hearings(max_items: int = 15) -> List[Dict]:
    """
    Derive upcoming public hearings by filtering council press releases
    for hearing-related titles (the calendar page is JavaScript-rendered).
    """
    all_items = fetch_county_council(max_items=40)
    hearing_keywords = ["public hearing", "hearing on", "committee meeting",
                        "council meets", "council meeting", "testify", "testimony"]
    hearings = [
        {**item, "type": "hearing", "categories": ["county", "hearing"]}
        for item in all_items
        if any(kw in item["title"].lower() for kw in hearing_keywords)
    ][:max_items]
    log.info("County hearings: %d items", len(hearings))
    return hearings


def fetch_local_services(max_items: int = 15) -> List[Dict]:
    """
    Scrape Montgomery County Police and Fire & Rescue press releases.
    Police: https://www2.montgomerycountymd.gov/mcgportalapps/press_List_Pol.aspx?id=47
    Fire:   no separate portal — inferred from all-dept list
    """
    results = []

    # Police press releases (dedicated portal)
    police_url = f"{_PORTAL}/press_List_Pol.aspx?id=47"
    soup = _get(police_url)
    if soup:
        rows = _parse_portal_table(
            soup,
            base_url=_PORTAL,
            source_name="Montgomery County Police",
            level="local",
            categories=["local", "police"],
            max_items=max_items // 2,
        )
        results.extend(rows)

    # Fire & Rescue — filter all-dept list for fire/rescue/ems titles
    all_url = f"{_PORTAL}/press_List.aspx?id=all"
    soup2 = _get(all_url)
    if soup2:
        all_items = _parse_portal_table(
            soup2,
            base_url=_PORTAL,
            source_name="Montgomery County Fire & Rescue",
            level="local",
            categories=["local", "fire"],
            max_items=60,
        )
        fire_kw = ["fire", "rescue", "ems", "ambulance", "hazmat", "emergency services",
                   "health", "hhs", "hospital"]
        fire_items = [
            {**item, "source_name": _infer_dept(item["title"]),
             "categories": ["local", _infer_cat(item["title"])]}
            for item in all_items
            if any(kw in item["title"].lower() for kw in fire_kw)
        ][: max_items // 2]
        results.extend(fire_items)

    log.info("Local services: %d items", len(results))
    return results


def fetch_mcps_board(max_items: int = 15) -> List[Dict]:
    """
    Scrape MCPS Board of Education news from montgomeryschoolsmd.org/news/.
    The board meetings page returned 404; news articles mention board actions.
    """
    url = "https://www.montgomeryschoolsmd.org/news/"
    soup = _get(url)
    if not soup:
        return []

    results = []
    for article in soup.select("article")[:max_items]:
        # Title is usually in an <a> or <h2>/<h3> inside the article
        link = article.find("a", href=True)
        title_el = article.find(["h2", "h3", "h4"]) or link
        if not title_el:
            continue

        title = title_el.get_text(strip=True)
        # Strip "Posted On ..." suffix that MCPS appends to article text
        title = re.sub(r"\s*Posted On.*$", "", title).strip()

        href = link.get("href", "") if link else ""
        if href and not href.startswith("http"):
            href = f"https://www.montgomeryschoolsmd.org{href}"

        # Date is sometimes in a <time> or a paragraph with "Posted On"
        date_str = ""
        time_el = article.find("time")
        if time_el:
            date_str = time_el.get("datetime", time_el.get_text(strip=True))
        else:
            text = article.get_text(" ", strip=True)
            m = re.search(r"Posted On\s+(\w+ \d+,?\s*\d{4})", text)
            if m:
                date_str = m.group(1)

        if not title or len(title) < 8:
            continue

        results.append({
            "title": title[:350],
            "type": _classify_type(title),
            "level": "school",
            "date": _parse_date(date_str),
            "source_url": href or url,
            "source_name": "MCPS News",
            "categories": ["school", "education"],
            "raw_content": article.get_text(" ", strip=True)[:800],
        })

    log.info("MCPS: %d items", len(results))
    return results


# ── Helpers ────────────────────────────────────────────────────────────────────

def _classify_type(title: str) -> str:
    t = title.lower()
    if any(x in t for x in ["public hearing", "hearing on", "testify"]):
        return "hearing"
    if any(x in t for x in ["bill", "legislation", "ordinance", "zoning text amendment", "zta"]):
        return "bill"
    if any(x in t for x in ["budget", "fiscal", "spending"]):
        return "budget"
    if any(x in t for x in ["election", "ballot", "vote", "candidate"]):
        return "election"
    if any(x in t for x in ["lawsuit", "court", "settlement", "judge"]):
        return "lawsuit"
    return "news"


def _infer_dept(title: str) -> str:
    t = title.lower()
    if any(x in t for x in ["fire", "rescue", "ems"]):
        return "Montgomery County Fire & Rescue"
    if any(x in t for x in ["health", "hhs", "hospital", "medical"]):
        return "Montgomery County Health & Human Services"
    return "Montgomery County"


def _infer_cat(title: str) -> str:
    t = title.lower()
    if any(x in t for x in ["fire", "rescue", "ems"]):
        return "fire"
    if any(x in t for x in ["health", "hospital", "medical", "hhs"]):
        return "health"
    return "local"


def _parse_date(text: str) -> str:
    """Try to parse a human date string into YYYY-MM-DD."""
    clean = re.sub(r"\s+", " ", text.strip())[:25]
    formats = [
        "%m/%d/%y", "%m/%d/%Y",
        "%B %d, %Y", "%b %d, %Y",
        "%Y-%m-%d", "%d %B %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(clean, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""
