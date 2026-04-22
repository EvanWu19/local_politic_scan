"""
Historical-news backfill — fetches deeper news coverage for tracked
politicians beyond the rolling RSS window the daily scan uses.

Source: Google News RSS search per-politician. The Google News URL
supports a `when:<N>{d,m,y}` operator that lets us pull older items
(rolling RSS only sees the last few days). No API key required.

Each backfill run:
  • Reads the politician roster (filtered by --level / --name)
  • For each politician, builds a search like:
      "<name>" Maryland politics  → when:2y
  • Parses the resulting RSS, dedupes against `events.source_url`
  • Inserts new items into `events` as type=news with source_name
    "Historical backfill: <politician>" and links them via
    `politician_events` with role="mentioned"
  • Logs the attempt in `historical_news_runs`

Heavy enrichment (AI summary, relevance) is intentionally NOT done here
— that's the daily scan's job. The backfill just plants raw rows so
the next regular scan + Analyst pass have data to work with.
"""
from __future__ import annotations

import logging
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import feedparser

log = logging.getLogger(__name__)

DEFAULT_WINDOW_LABEL = "2y"   # Google News when: token
DEFAULT_LOCALE_HINT = "Maryland"
MAX_ITEMS_PER_POLITICIAN = 40


def backfill_all(db_path: Path, locale_hint: str = DEFAULT_LOCALE_HINT,
                  window: str = DEFAULT_WINDOW_LABEL,
                  level: Optional[str] = None,
                  name_filter: Optional[str] = None,
                  max_items: int = MAX_ITEMS_PER_POLITICIAN,
                  min_event_link_role: str = "mentioned") -> List[Dict]:
    """
    Run the backfill for every politician matching the filters.
    Returns the list of run-log rows that were saved.
    """
    from scanner.database import list_politicians

    pols = list_politicians(db_path, level=level)
    if name_filter:
        needle = name_filter.lower()
        pols = [p for p in pols if needle in (p.get("name") or "").lower()]
    if not pols:
        log.info("Backfill: no politicians match filters")
        return []

    runs: List[Dict] = []
    for p in pols:
        try:
            run = backfill_one(
                db_path=db_path,
                politician_id=p["id"],
                politician_name=p["name"],
                locale_hint=locale_hint,
                window=window,
                max_items=max_items,
                link_role=min_event_link_role,
            )
        except Exception as e:
            log.exception("Backfill failed for %s", p.get("name"))
            from scanner.database import save_historical_news_run
            save_historical_news_run(
                db_path=db_path,
                politician_id=p["id"],
                politician_name=p["name"],
                window_start="", window_end="",
                items_found=0, items_new=0,
                status="error", error=str(e),
            )
            continue
        if run:
            runs.append(run)
    return runs


def backfill_one(db_path: Path, politician_id: int, politician_name: str,
                  locale_hint: str = DEFAULT_LOCALE_HINT,
                  window: str = DEFAULT_WINDOW_LABEL,
                  max_items: int = MAX_ITEMS_PER_POLITICIAN,
                  link_role: str = "mentioned") -> Optional[Dict]:
    """
    Fetch historical news for a single politician, dedupe + insert,
    and write a row to historical_news_runs. Returns the run-log dict.
    """
    from scanner.database import (
        upsert_event, link_politician_event, save_historical_news_run,
        get_last_historical_news_run, get_connection,
    )

    items = _fetch_google_news(politician_name, locale_hint, window, max_items)
    items_found = len(items)
    if items_found == 0:
        save_historical_news_run(
            db_path=db_path, politician_id=politician_id,
            politician_name=politician_name,
            window_start="", window_end="",
            items_found=0, items_new=0, status="empty",
        )
        return get_last_historical_news_run(db_path, politician_id)

    dates = [it["date"] for it in items if it.get("date")]
    window_start = min(dates) if dates else ""
    window_end = max(dates) if dates else ""

    items_new = 0
    for it in items:
        url = it["link"]
        # upsert_event returns the existing id for dupes — probe first
        # so the run-log "items_new" reflects truly inserted rows.
        with get_connection(db_path) as conn:
            existed = conn.execute(
                "SELECT 1 FROM events WHERE source_url = ?", (url,)
            ).fetchone() is not None
        ev = {
            "title": it["title"][:400],
            "type": "news",
            "level": "historical",
            "date": it.get("date") or "",
            "source_url": url,
            "source_name": f"Historical backfill: {politician_name}",
            "description": it.get("summary", "")[:500],
            "raw_content": it.get("summary", "")[:1500],
            "categories": ["historical", "news"],
        }
        event_id = upsert_event(db_path, ev)
        if event_id is None:
            continue
        # link_politician_event INSERT-OR-IGNOREs on the unique
        # (politician, event, role) tuple — safe to call repeatedly
        link_politician_event(
            db_path, politician_name, event_id,
            role=link_role, stance="unknown",
        )
        if not existed:
            items_new += 1

    save_historical_news_run(
        db_path=db_path, politician_id=politician_id,
        politician_name=politician_name,
        window_start=window_start, window_end=window_end,
        items_found=items_found, items_new=items_new,
        status="ok",
    )
    log.info("Backfill: %s -> %d found, %d new (%s..%s)",
             politician_name, items_found, items_new, window_start, window_end)
    return get_last_historical_news_run(db_path, politician_id)


# ──────────────────────────────────────────────────────────────────────────────
# Google News RSS
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_google_news(name: str, locale_hint: str, window: str,
                        max_items: int) -> List[Dict]:
    url = _build_google_news_url(name, locale_hint, window)
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        log.warning("feedparser.parse failed for %s: %s", name, e)
        return []
    if feed.bozo and not feed.entries:
        log.warning("Google News empty/bozo for %s: %s",
                    name, getattr(feed, "bozo_exception", ""))
        return []

    out: List[Dict] = []
    for entry in feed.entries[:max_items]:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        summary = (entry.get("summary") or
                   entry.get("description") or "").strip()
        pub = ""
        pp = entry.get("published_parsed")
        if pp:
            try:
                pub = datetime(*pp[:6]).strftime("%Y-%m-%d")
            except Exception:
                pub = ""
        out.append({
            "title": title,
            "link": link,
            "summary": _strip_html(summary),
            "date": pub,
        })
    return out


def _build_google_news_url(name: str, locale_hint: str, window: str) -> str:
    """
    Build the Google News RSS search URL.

    Window must look like '2y', '6m', '90d'. Anything else is ignored.
    """
    parts = [f"\"{name}\""]
    if locale_hint:
        parts.append(locale_hint)
    parts.append("politics")
    if window and window[-1] in ("d", "m", "y") and window[:-1].isdigit():
        parts.append(f"when:{window}")
    q = " ".join(parts)
    qs = urllib.parse.urlencode({
        "q": q,
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    })
    return f"https://news.google.com/rss/search?{qs}"


def _strip_html(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", " ", html or "").strip()
