"""
RSS/Atom news feed aggregator.
Reads feeds defined in Config.NEWS_FEEDS — no API key required.
"""
import feedparser
import logging
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import List, Dict

log = logging.getLogger(__name__)


def fetch_rss_feeds(feeds: List[Dict], days_back: int = 7,
                    max_per_feed: int = 20) -> List[Dict]:
    """
    Fetch all configured RSS feeds.
    Returns a flat list of event dicts.
    """
    results: List[Dict] = []

    for feed_cfg in feeds:
        name = feed_cfg.get("name", "Unknown")
        url = feed_cfg.get("url", "")
        level = feed_cfg.get("level", "county")
        if not url:
            continue

        try:
            feed = feedparser.parse(url)
        except Exception as e:
            log.warning("Failed to parse feed '%s': %s", name, e)
            continue

        if feed.bozo and not feed.entries:
            log.warning("Feed '%s' returned no entries (bozo=%s)", name, feed.bozo_exception)
            continue

        count = 0
        for entry in feed.entries[:max_per_feed]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", entry.get("description", "")).strip()
            content_list = entry.get("content", [])
            content_text = content_list[0].get("value", "") if content_list else ""
            raw = content_text or summary

            # Parse publish date
            pub_date = ""
            published_parsed = entry.get("published_parsed")
            if published_parsed:
                try:
                    dt = datetime(*published_parsed[:6])
                    days_old = (datetime.now() - dt).days
                    if days_old > days_back * 4:  # generous window for news
                        continue
                    pub_date = dt.strftime("%Y-%m-%d")
                except Exception:
                    pass

            if not title or not link:
                continue

            results.append({
                "title": title[:400],
                "type": "news",
                "level": level,
                "date": pub_date,
                "source_url": link,
                "source_name": name,
                "description": summary[:500] if summary else "",
                "raw_content": _strip_html(raw)[:1500],
                "categories": [level],
            })
            count += 1

        log.debug("Feed '%s': %d items", name, count)

    log.info("RSS feeds: total %d items across %d feeds", len(results), len(feeds))
    return results


def _strip_html(html: str) -> str:
    """Very lightweight HTML tag stripper."""
    import re
    return re.sub(r"<[^>]+>", " ", html).strip()
