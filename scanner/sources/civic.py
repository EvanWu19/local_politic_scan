"""
civic-scraper / Legistar adapter (OSS plan, item 2).

ADDITIVE and OPTIONAL: this does NOT replace the working RSS/HTML fetchers in
local_hearings.py. It adds a standardized fetcher for bodies that publish on a
Legistar/Granicus/CivicPlus stack (e.g. the Montgomery County Council), which
the current sources don't cover. Enabled only when civic-scraper is installed
AND a client is configured (Config.CIVIC_LEGISTAR_CLIENT, e.g. "montgomerycountymd").
Returns the same event dict shape as local_hearings; [] on any failure.

We avoid ripping out the Rockville CivicPlus RSS (which works) — this is for the
council/hearing bodies that have no feed today.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


def _civic_scraper():
    try:
        from civic_scraper.platforms import LegistarSite  # type: ignore
        return LegistarSite
    except Exception:
        log.info("civic-scraper not installed — Legistar source disabled "
                 "(`pip install civic-scraper` to enable).")
        return None


def _to_event(asset: Dict) -> Dict:
    title = asset.get("meeting_title") or asset.get("asset_name") or "Council meeting"
    when = (asset.get("meeting_date") or asset.get("date") or "")[:10]
    return {
        "title": f"Montgomery County Council: {title}",
        "date": when or datetime.utcnow().date().isoformat(),
        "source_url": asset.get("url") or asset.get("asset_url") or "",
        "source_name": "Montgomery County Council (Legistar)",
        "summary": asset.get("asset_type") or "Council meeting / agenda.",
        "type": "hearing",
        "level": "county",
        "categories": ["county", "council", "hearing"],
        "proximity_score": 0.4,
    }


def fetch_legistar_meetings(client: Optional[str] = None,
                            max_items: int = 12) -> List[Dict]:
    """Fetch recent meetings/agendas for a Legistar client via civic-scraper.
    No-op ([]) when the library is missing or no client is configured."""
    if client is None:
        try:
            from config import Config as _Cfg
            client = getattr(_Cfg, "CIVIC_LEGISTAR_CLIENT", "") or ""
        except Exception:
            client = ""
    if not client:
        return []
    Legistar = _civic_scraper()
    if Legistar is None:
        return []
    base = f"https://{client}.legistar.com"
    try:
        site = Legistar(base)
        assets = site.scrape()
        items: List[Dict] = []
        for a in list(assets)[:max_items]:
            data = a.__dict__ if hasattr(a, "__dict__") else dict(a)
            items.append(_to_event(data))
        log.info("civic: Legistar(%s) — %d items", client, len(items))
        return items
    except Exception as e:
        log.warning("civic: Legistar(%s) failed — %s", client, e)
        return []
