"""
County-council meeting source (OSS plan, item 2).

ADDITIVE and OPTIONAL: this does NOT replace the working RSS/HTML fetchers in
local_hearings.py. It adds a standardized fetcher for the Montgomery County
Council's meeting calendar, which the current sources don't cover.

Platform note (validated live 2026-06)
--------------------------------------
The OSS plan assumed the council ran on Legistar. It does NOT, in practice:
  • ``montgomerycountymd.legistar.com`` still answers, but its data is frozen
    at 2023-02-01 — the council migrated off it.
  • The council's CURRENT agendas/meetings are published on **Granicus**
    (``montgomerycountymd.granicus.com``, view_id 169).
civic-scraper ships both a Legistar and a Granicus platform class, but BOTH
fail on MoCo's live data: LegistarSite reads the stale instance (0 rows), and
GranicusSite raises because it assumes RSS titles split into three " - " parts
(``committee - type - datetime``) whereas MoCo's are two
(``"Council Session - May 21, 2026"``).

So this module reads the Granicus ViewPublisher RSS directly with feedparser
(already a project dependency). Returns the same event dict shape as
local_hearings; [] on any failure or when no client is configured.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

import requests

try:
    import feedparser  # already used by scanner/sources/news.py
except Exception:  # pragma: no cover - feedparser is a hard dep elsewhere
    feedparser = None  # type: ignore

log = logging.getLogger(__name__)

GRANICUS_RSS = ("https://{client}.granicus.com/ViewPublisherRSS.php"
                "?view_id={view}&mode=agendas")


def _entry_date(entry) -> str:
    """Best-effort ISO date from a feedparser entry."""
    st = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if st:
        try:
            return datetime(*st[:3]).date().isoformat()
        except Exception:
            pass
    return datetime.utcnow().date().isoformat()


def _to_event(entry) -> Dict:
    title = (getattr(entry, "title", "") or "Council meeting").strip()
    url = (getattr(entry, "link", "") or "").strip()
    return {
        "title": f"Montgomery County Council: {title}",
        "date": _entry_date(entry),
        "source_url": url,
        "source_name": "Montgomery County Council (Granicus)",
        "summary": "Council meeting / agenda.",
        "type": "hearing",
        "level": "county",
        "categories": ["county", "council", "hearing"],
        "proximity_score": 0.4,
    }


def fetch_granicus_meetings(client: Optional[str] = None,
                            view: Optional[str] = None,
                            max_items: int = 12) -> List[Dict]:
    """Fetch recent council meetings/agendas from the Granicus ViewPublisher
    RSS. No-op ([]) when no client is configured or feedparser is missing.
    Never raises."""
    if client is None or view is None:
        try:
            from config import Config as _Cfg
            client = client if client is not None else (
                getattr(_Cfg, "CIVIC_GRANICUS_CLIENT", "") or "")
            view = view if view is not None else (
                getattr(_Cfg, "CIVIC_GRANICUS_VIEW", "") or "")
        except Exception:
            client, view = client or "", view or ""
    if not client or not view:
        return []
    if feedparser is None:
        log.info("feedparser not available — Granicus council source disabled.")
        return []
    url = GRANICUS_RSS.format(client=client, view=view)
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        parsed = feedparser.parse(r.text)
        items = [_to_event(e) for e in parsed.entries[:max_items]]
        log.info("civic: Granicus(%s/%s) — %d meeting(s)", client, view, len(items))
        return items
    except Exception as e:
        log.warning("civic: Granicus(%s/%s) failed — %s", client, view, e)
        return []


# Backward-compatible alias: main.py and the OSS plan refer to
# ``fetch_legistar_meetings``. MoCo turned out to be Granicus, so the
# implementation moved, but the public name is preserved.
def fetch_legistar_meetings(client: Optional[str] = None,
                            max_items: int = 12) -> List[Dict]:
    """Compatibility shim — see ``fetch_granicus_meetings``. The OSS plan named
    this for Legistar before we discovered MoCo Council runs on Granicus."""
    return fetch_granicus_meetings(max_items=max_items)
