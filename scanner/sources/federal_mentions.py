"""
Federal "mentions" source — GovInfo Congressional Record (OSS plan, item 5).

scanner/sources/federal.py already tracks federal *bills* via Congress.gov, but
it misses the "Rep. Raskin mentioned Montgomery County on the House floor" type
signal. This module queries GovInfo's search API over the Congressional Record
(collection CREC) for the listener's local terms and surfaces hits as events at
level ``federal_mentions`` (rendered in its own digest section).

Contract: never raises to the caller; returns [] on any failure or when no
GOVINFO_API_KEY is set. GovInfo accepts api.data.gov keys; ``DEMO_KEY`` works at
low volume for testing.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import requests

log = logging.getLogger(__name__)

GOVINFO_SEARCH = "https://api.govinfo.gov/search"
GOVINFO_DETAILS = "https://www.govinfo.gov/app/details"


def _build_query(terms: List[str], start: str, end: str) -> str:
    """OR the local terms together, scoped to the Congressional Record + dates."""
    ors = " OR ".join(f'"{t}"' for t in terms if t)
    return f"collection:CREC AND publishdate:range({start},{end}) AND ({ors})"


def parse_search_results(payload: Dict, terms: List[str], max_items: int) -> List[Dict]:
    """Turn a GovInfo /search JSON payload into our event dicts.
    Separated from network I/O so it can be unit-tested against a fixture."""
    out: List[Dict] = []
    termset = [t.lower() for t in terms if t]
    for r in (payload or {}).get("results", [])[:max_items]:
        title = (r.get("title") or "").strip()
        pkg = (r.get("packageId") or "").strip()
        gran = (r.get("granuleId") or "").strip()
        issued = (r.get("dateIssued") or r.get("dateIngested") or "")[:10]
        if pkg and gran:
            url = f"{GOVINFO_DETAILS}/{pkg}/{gran}"
        elif pkg:
            url = f"{GOVINFO_DETAILS}/{pkg}"
        else:
            url = (r.get("download", {}) or {}).get("pdfLink", "") or "https://www.govinfo.gov/"
        blob = f"{title} {pkg}".lower()
        matched = next((t for t in terms if t.lower() in blob), "")
        out.append({
            "title": f"Congressional Record: {title or pkg}",
            "type": "news",
            "level": "federal_mentions",
            "date": issued,
            "source_url": url,
            "source_name": "GovInfo · Congressional Record",
            "description": (
                f"Federal floor/record entry mentioning "
                f"{matched or 'a local term'}."
            ),
            "raw_content": title,
            "categories": ["federal", "mention"],
            "_matched_term": matched,
        })
    return out


def fetch_federal_mentions(api_key: str, terms: List[str],
                           days_back: int = 7, max_items: int = 10) -> List[Dict]:
    """Search the Congressional Record for the listener's local terms."""
    if not api_key:
        log.info("No GOVINFO_API_KEY set — skipping federal mentions")
        return []
    if not terms:
        return []
    end = date.today()
    start = end - timedelta(days=max(1, days_back))
    body = {
        "query": _build_query(terms, start.isoformat() + "T00:00:00Z",
                              end.isoformat() + "T23:59:59Z"),
        "pageSize": max_items,
        "offsetMark": "*",
        "sorts": [{"field": "publishdate", "sortOrder": "DESC"}],
        "historical": False,
        # GovInfo /search rejects "granule" (400: valid values are
        # package/default). "default" returns the most granular matching level
        # — for CREC that's the individual speech granule, which carries the
        # granuleId we need to build a per-speech /app/details/{pkg}/{gran} URL.
        "resultLevel": "default",
    }
    try:
        r = requests.post(f"{GOVINFO_SEARCH}?api_key={api_key}", json=body, timeout=20)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        log.warning("GovInfo search failed: %s", e)
        return []
    items = parse_search_results(payload, terms, max_items)
    log.info("Federal mentions: %d Congressional Record hit(s)", len(items))
    return items
