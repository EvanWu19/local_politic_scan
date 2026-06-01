"""
Hyperlocal hearing fetchers — Rockville City, MCPS Board of Education,
M-NCPPC (Park & Planning), and WSSC.

Why this exists
---------------
The pre-existing sources stop at Montgomery County press releases. The
listener (Rockville 20853, precinct 08-008) has explicitly asked for the
zoning / planning hearings near their house, MCPS Board agendas (Wootton
cluster especially), and Park & Planning items. None of those are in the
county RSS or Maryland Matters feeds.

These fetchers all return the same dict shape consumed by the rest of the
pipeline:

    {
        "title":       "...",
        "date":        "YYYY-MM-DD",
        "source_url":  "https://...",
        "source_name": "Rockville City Council",
        "summary":     "...",
        "type":        "hearing",
        "level":       "local" | "school",
        "categories":  ["local", "rockville", "hearing"],
        "proximity_score": 1.0,   # 1.0 = same ZIP, 0.5 = same county
    }

Robustness
----------
Every fetcher catches and logs its own errors and returns `[]` on failure;
the daily scan must never break because one upstream calendar page changed
its HTML.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import feedparser
import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Local Politics Scanner; +https://github.com/local-politics) "
        "AppleWebKit/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

ROCKVILLE_LOCAL_KEYWORDS = (
    "rockville", "20850", "20851", "20852", "20853", "20855",
    "king farm", "twinbrook", "fallsgrove", "aspen hill",
    "wootton", "richard montgomery", "thomas s wootton",
    "fallsmead", "potomac woods", "redgate", "norbeck",
)


def _proximity_score(text: str) -> float:
    """Return 1.0 if the text mentions the listener's immediate area, 0.7
    if Rockville-broadly, 0.4 if Montgomery County, 0.0 otherwise."""
    t = (text or "").lower()
    if any(k in t for k in ("20853", "king farm", "aspen hill", "wootton",
                              "norbeck", "rockville")):
        if "20853" in t or "aspen hill" in t or "wootton" in t or "norbeck" in t:
            return 1.0
        return 0.7
    if "montgomery county" in t or "mococounty" in t:
        return 0.4
    return 0.0


def _get(url: str, timeout: int = 15) -> Optional[BeautifulSoup]:
    try:
        r = requests.get(url, timeout=timeout, headers=HEADERS)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.warning("local_hearings: GET %s failed — %s", url, e)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Rockville City — Mayor & Council and Planning Commission
# ──────────────────────────────────────────────────────────────────────────────

# AgendaCenter pages expose RSS at /api/RSS/AgendaCenterRss/Category/<id>
# (see https://www.rockvillemd.gov/AgendaCenter source). Category 1 =
# Mayor & Council; Category 7 = Planning Commission. If the site renames
# the category IDs the fetcher logs and returns [].
ROCKVILLE_RSS = {
    "Rockville Mayor & Council":     "https://www.rockvillemd.gov/RSSFeed.aspx?ModID=49&CID=14",
    "Rockville Planning Commission": "https://www.rockvillemd.gov/RSSFeed.aspx?ModID=49&CID=37",
}


def _fetch_rockville_feed(name: str, url: str, max_items: int) -> List[Dict]:
    items: List[Dict] = []
    try:
        feed = feedparser.parse(url, request_headers=HEADERS)
    except Exception as e:
        log.warning("local_hearings: rockville feed %s failed — %s", name, e)
        return items

    for entry in feed.entries[:max_items]:
        title = (entry.get("title") or "").strip()
        link  = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        # Pull a date — Rockville agendas embed it in the title or in
        # published_parsed if present.
        published = ""
        if getattr(entry, "published_parsed", None):
            published = datetime(*entry.published_parsed[:6]).date().isoformat()
        else:
            m = re.search(r"(\d{4}-\d{2}-\d{2})", title) or re.search(
                r"([A-Za-z]+ \d{1,2},?\s+\d{4})", title
            )
            if m:
                try:
                    published = datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
                except Exception:
                    pass
        summary = (entry.get("summary") or "")[:600].strip()
        items.append({
            "title": title,
            "date": published or datetime.utcnow().date().isoformat(),
            "source_url": link,
            "source_name": name,
            "summary": summary or f"Agenda / hearing from {name}.",
            "type": "hearing",
            "level": "local",
            "categories": ["local", "rockville", "hearing"],
            "proximity_score": _proximity_score(f"{title} {summary} rockville"),
        })
    log.info("local_hearings: %s — %d items", name, len(items))
    return items


def fetch_rockville_council(max_items: int = 12) -> List[Dict]:
    return _fetch_rockville_feed(
        "Rockville Mayor & Council",
        ROCKVILLE_RSS["Rockville Mayor & Council"],
        max_items,
    )


def fetch_rockville_planning(max_items: int = 12) -> List[Dict]:
    return _fetch_rockville_feed(
        "Rockville Planning Commission",
        ROCKVILLE_RSS["Rockville Planning Commission"],
        max_items,
    )


# ──────────────────────────────────────────────────────────────────────────────
# MCPS Board of Education — BoardDocs Public
# ──────────────────────────────────────────────────────────────────────────────

# BoardDocs exposes a structured XML feed of active/upcoming meetings at
# Board.nsf/XML-ActiveMeetings (same view City-Bureau's city-scrapers hits).
# We parse that first and fall back to scraping the HTML listing page only if
# the feed is unavailable or empty. This kills our most fragile parser
# (OSS plan, item 1).
MCPS_LISTING = "https://go.boarddocs.com/mabe/mcpsmd/Board.nsf/Public"
MCPS_XML = "https://go.boarddocs.com/mabe/mcpsmd/Board.nsf/XML-ActiveMeetings"
MCPS_BOARDDOCS_BASE = "https://go.boarddocs.com/mabe/mcpsmd/Board.nsf"


def _parse_boarddocs_date(text: str) -> str:
    """Best-effort date parse from BoardDocs strings. Returns ISO or ''."""
    text = (text or "").strip()
    if not text:
        return ""
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if m:
        return m.group(0)
    m = re.search(r"([A-Za-z]+\s+\d{1,2},?\s+\d{4})", text)
    if m:
        token = m.group(1).replace(",", "")
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(token, fmt).date().isoformat()
            except Exception:
                continue
    return ""


def _parse_boarddocs_xml(xml_bytes: bytes, max_items: int) -> List[Dict]:
    """
    Parse a BoardDocs XML-ActiveMeetings payload into our event dicts.

    Real MCPS schema (verified live 2026-05):

        <meetings>
          <meeting id="DUFH324673AD" bodyname="Main Governing Board" order="755">
            <name>Special Board Business Meeting Agenda</name>
            <start><date format="yyyy-mm-dd">2026-05-27</date>
                   <english><date>May 27, 2026</date></english></start>
            <description>MONTGOMERY COUNTY BOARD OF EDUCATION ...</description>
            <link>https://go.boarddocs.com/.../goto?open&id=DUFH324673AD</link>
            <category>...<agendaitems>...</agendaitems></category>
          </meeting>
          ...
        </meetings>

    Two things the feed does that the caller must handle:
      * It returns the ENTIRE meeting history (754 meetings back to 2013), NOT
        just active/upcoming ones, and it is NOT sorted by date — recent/upcoming
        meetings sit at the END. So we must sort by date descending and keep the
        most recent `max_items`, never the first N.
      * The unique id lives in the `id` attribute (there is no `unique` attr or
        <unique> child), and each meeting carries a ready-made <link>.

    Schema-tolerant fallbacks are retained (id via `unique`, title via
    <description>, date embedded in the name) so a minor schema rename degrades
    rather than breaks. Separated from network I/O so it can be unit-tested
    against a fixture.
    """
    from lxml import etree

    parsed: List[Dict] = []
    root = etree.fromstring(xml_bytes)
    meetings = [el for el in root.iter() if etree.QName(el).localname == "meeting"]

    for m in meetings:
        def _child_text(*names: str) -> str:
            """First direct-child element (by local name) with non-empty text."""
            for n in names:
                for el in m:
                    if etree.QName(el).localname == n and el.text and el.text.strip():
                        return el.text.strip()
            return ""

        def _date_text() -> str:
            """The yyyy-mm-dd date under <start>; fall back to the name."""
            for start in m:
                if etree.QName(start).localname != "start":
                    continue
                for el in start.iter():
                    if etree.QName(el).localname == "date" and el.text and el.text.strip():
                        return el.text.strip()
            return ""

        uid = (m.get("id") or m.get("unique") or _child_text("unique")).strip()
        name = _child_text("name", "description") or "Board meeting"
        published = _parse_boarddocs_date(_date_text() or name)
        url = _child_text("link") or (
            f"{MCPS_BOARDDOCS_BASE}/goto?open&id={uid}" if uid else MCPS_LISTING
        )
        parsed.append({
            "title": f"MCPS Board of Education: {name}",
            "date": published or datetime.utcnow().date().isoformat(),
            "source_url": url,
            "source_name": "MCPS Board of Education",
            "summary": "Montgomery County Public Schools Board of Education meeting.",
            "type": "hearing",
            "level": "school",
            "categories": ["school", "mcps", "hearing"],
            "proximity_score": 0.7,  # MCPS-wide; Wootton-specific items boost via processor
        })

    # Most recent / upcoming first, then keep only max_items.
    parsed.sort(key=lambda x: x.get("date") or "", reverse=True)
    return parsed[:max_items]


def fetch_mcps_boarddocs(max_items: int = 10) -> List[Dict]:
    # Structured XML feed first; HTML scrape only as a fallback.
    # The feed is the full meeting history (~4.7 MB, ~25 s to serve), so the
    # timeout must be generous or every run silently falls back to HTML.
    try:
        r = requests.get(MCPS_XML, timeout=60, headers=HEADERS)
        r.raise_for_status()
        items = _parse_boarddocs_xml(r.content, max_items)
        if items:
            log.info("local_hearings: MCPS Board (XML) — %d items", len(items))
            return items
        log.warning("local_hearings: MCPS XML feed parsed 0 meetings; "
                    "falling back to HTML scrape")
    except Exception as e:
        log.warning("local_hearings: MCPS XML feed failed (%s); "
                    "falling back to HTML scrape", e)
    return _fetch_mcps_boarddocs_html(max_items)


def _fetch_mcps_boarddocs_html(max_items: int = 10) -> List[Dict]:
    soup = _get(MCPS_LISTING)
    if not soup:
        return []
    items: List[Dict] = []
    seen: set = set()
    for a in soup.select("a[href*='Meeting']"):
        href = a.get("href", "")
        title = a.get_text(" ", strip=True)
        if not title or href in seen or len(title) < 6:
            continue
        seen.add(href)
        url = href if href.startswith("http") else f"https://go.boarddocs.com{href}"
        # Try to extract date from the title (BoardDocs format: "May 14, 2026 Business Meeting").
        published = _parse_boarddocs_date(title)
        items.append({
            "title": f"MCPS Board of Education: {title}",
            "date": published or datetime.utcnow().date().isoformat(),
            "source_url": url,
            "source_name": "MCPS Board of Education",
            "summary": "Montgomery County Public Schools Board of Education meeting.",
            "type": "hearing",
            "level": "school",
            "categories": ["school", "mcps", "hearing"],
            "proximity_score": 0.7,  # MCPS-wide; Wootton-specific items boost via processor
        })
        if len(items) >= max_items:
            break
    log.info("local_hearings: MCPS Board (HTML) — %d items", len(items))
    return items


# ──────────────────────────────────────────────────────────────────────────────
# Maryland-National Capital Park & Planning Commission (M-NCPPC)
# ──────────────────────────────────────────────────────────────────────────────

MNCPPC_AGENDA = "https://montgomeryplanningboard.org/agenda/"


def fetch_mncppc_hearings(max_items: int = 10) -> List[Dict]:
    soup = _get(MNCPPC_AGENDA)
    if not soup:
        return []
    items: List[Dict] = []
    # The page lists upcoming meetings as <article> or <li> blocks with date.
    for blk in soup.select("article, li.agenda-item, div.agenda-card")[:max_items]:
        title_el = blk.find(["h2", "h3", "a"])
        if not title_el:
            continue
        title = title_el.get_text(" ", strip=True)
        a = blk.find("a", href=True)
        url = a["href"] if a else MNCPPC_AGENDA
        if not url.startswith("http"):
            url = "https://montgomeryplanningboard.org" + url
        date_el = blk.find(class_=re.compile(r"date|when", re.I))
        published = ""
        if date_el:
            m = re.search(r"([A-Za-z]+\s+\d{1,2},?\s+\d{4})", date_el.get_text())
            if m:
                try:
                    published = datetime.strptime(
                        m.group(1).replace(",", ""), "%B %d %Y"
                    ).date().isoformat()
                except Exception:
                    pass
        summary = blk.get_text(" ", strip=True)[:500]
        items.append({
            "title": f"M-NCPPC: {title}",
            "date": published or datetime.utcnow().date().isoformat(),
            "source_url": url,
            "source_name": "Montgomery Planning Board",
            "summary": summary,
            "type": "hearing",
            "level": "local",
            "categories": ["local", "planning", "hearing"],
            "proximity_score": _proximity_score(f"{title} {summary}"),
        })
    log.info("local_hearings: M-NCPPC — %d items", len(items))
    return items


# ──────────────────────────────────────────────────────────────────────────────
# WSSC public meetings (water & sewer commission)
# ──────────────────────────────────────────────────────────────────────────────

WSSC_MEETINGS = "https://www.wsscwater.com/about-us/public-meetings"


def fetch_wssc_hearings(max_items: int = 8) -> List[Dict]:
    soup = _get(WSSC_MEETINGS)
    if not soup:
        return []
    items: List[Dict] = []
    for row in soup.select("table tr, li, .meeting-item")[:max_items * 3]:
        text = row.get_text(" ", strip=True)
        if not text or len(text) < 10:
            continue
        # Heuristic: must include a date-looking token.
        m = re.search(r"([A-Za-z]+\s+\d{1,2},?\s+\d{4})", text)
        if not m:
            continue
        try:
            published = datetime.strptime(
                m.group(1).replace(",", ""), "%B %d %Y"
            ).date().isoformat()
        except Exception:
            continue
        a = row.find("a", href=True)
        url = a["href"] if a else WSSC_MEETINGS
        if not url.startswith("http"):
            url = "https://www.wsscwater.com" + url
        items.append({
            "title": f"WSSC: {text[:120]}",
            "date": published,
            "source_url": url,
            "source_name": "WSSC Water",
            "summary": text[:500],
            "type": "hearing",
            "level": "local",
            "categories": ["local", "utility", "hearing"],
            "proximity_score": 0.5,
        })
        if len(items) >= max_items:
            break
    log.info("local_hearings: WSSC — %d items", len(items))
    return items


# ──────────────────────────────────────────────────────────────────────────────
# Public bundle
# ──────────────────────────────────────────────────────────────────────────────

def fetch_all_local_hearings(max_per_source: int = 10) -> List[Dict]:
    """Run every fetcher; failures in one do not stop the others."""
    bundle: List[Dict] = []
    for fn in (
        fetch_rockville_council,
        fetch_rockville_planning,
        fetch_mcps_boarddocs,
        fetch_mncppc_hearings,
        fetch_wssc_hearings,
    ):
        try:
            bundle.extend(fn(max_per_source))
        except Exception as e:
            log.exception("local_hearings: %s failed — %s", fn.__name__, e)
    # Sort by date descending (most recent / upcoming first).
    bundle.sort(key=lambda x: x.get("date") or "", reverse=True)
    return bundle


if __name__ == "__main__":  # quick smoke test
    logging.basicConfig(level=logging.INFO)
    for item in fetch_all_local_hearings():
        d = item.get("date", "")
        src = item.get("source_name", "")
        title = (item.get("title") or "")[:70]
        print("[%s] %s — %s" % (d, src, title))
