"""
RSS/Atom news feed aggregator.
Reads feeds defined in Config.NEWS_FEEDS — no API key required.

When full-text extraction is enabled (Config.FULLTEXT_EXTRACT, default on), each
item's RSS summary is upgraded to the clean article body + canonical date/author
via scanner.sources.extract (trafilatura). Extraction failures silently fall
back to the RSS summary, so the scan never breaks on a bad fetch.
"""
import feedparser
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import List, Dict, Optional

log = logging.getLogger(__name__)


def fetch_rss_feeds(feeds: List[Dict], days_back: int = 7,
                    max_per_feed: int = 20,
                    extract_fulltext: Optional[bool] = None,
                    max_extract: Optional[int] = None,
                    max_chars: Optional[int] = None) -> List[Dict]:
    """
    Fetch all configured RSS feeds.
    Returns a flat list of event dicts.

    extract_fulltext / max_extract / max_chars default to the corresponding
    Config values (FULLTEXT_EXTRACT / FULLTEXT_MAX_ARTICLES / FULLTEXT_MAX_CHARS)
    when left as None, so existing callers get the behaviour for free.
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

    _maybe_enrich_fulltext(results, extract_fulltext, max_extract, max_chars)
    return results


def _strip_html(html: str) -> str:
    """Very lightweight HTML tag stripper."""
    import re
    return re.sub(r"<[^>]+>", " ", html).strip()


def _resolve_extract_settings(extract_fulltext, max_extract, max_chars):
    """Fill in None args from Config; tolerate Config being unavailable."""
    try:
        from config import Config as _Cfg
    except Exception:
        _Cfg = None
    if extract_fulltext is None:
        extract_fulltext = bool(getattr(_Cfg, "FULLTEXT_EXTRACT", False)) if _Cfg else False
    if max_extract is None:
        max_extract = int(getattr(_Cfg, "FULLTEXT_MAX_ARTICLES", 80)) if _Cfg else 80
    if max_chars is None:
        max_chars = int(getattr(_Cfg, "FULLTEXT_MAX_CHARS", 6000)) if _Cfg else 6000
    return extract_fulltext, max_extract, max_chars


def _maybe_enrich_fulltext(results, extract_fulltext, max_extract, max_chars):
    """
    In-place upgrade of each item's raw_content with the clean article body.
    Runs concurrently (I/O-bound); extract_article never raises, so a single
    bad URL can't break the batch. No-op when disabled or trafilatura missing.
    """
    extract_fulltext, max_extract, max_chars = _resolve_extract_settings(
        extract_fulltext, max_extract, max_chars
    )
    if not extract_fulltext or not results:
        return

    try:
        from scanner.sources.extract import extract_article
    except Exception as e:
        log.warning("news: full-text extract module unavailable — %s", e)
        return

    targets = [ev for ev in results if ev.get("source_url")][:max_extract]
    if not targets:
        return

    def _job(ev):
        return ev, extract_article(ev["source_url"])

    enriched = 0
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for ev, art in pool.map(_job, targets):
                if not art or not art.get("text"):
                    continue
                ev["raw_content"] = art["text"][:max_chars]
                ev["full_text_extracted"] = True
                if art.get("author"):
                    ev["author"] = art["author"]
                if not ev.get("date") and art.get("date"):
                    ev["date"] = art["date"][:10]
                enriched += 1
    except Exception as e:
        log.warning("news: full-text enrichment pass failed — %s", e)

    log.info("news: full-text extracted %d/%d article(s)", enriched, len(targets))
