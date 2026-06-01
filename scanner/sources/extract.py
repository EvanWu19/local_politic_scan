"""
Full-text article extraction (trafilatura) with on-disk caching.

Why this exists
---------------
The RSS aggregator (``news.py``) only sees feed summaries, which carry cookie
banners, "related links" navigation, and truncated bodies. That junk reaches
the relevance scorer and the podcast TTS prompt, and the missing canonical
date/author weakens dedupe. This module fetches the real article and returns
clean body text + metadata (OSS plan, item 3).

Design contract
---------------
* NEVER raises to the caller — every failure path returns ``None`` and logs.
* ``trafilatura`` is an OPTIONAL dependency. If it isn't installed the module
  degrades to a no-op (returns ``None``) and logs the reason once.
* Results are cached on disk keyed by URL so daily re-runs and weekly
  re-scores don't re-fetch. A *negative* result is cached as ``{}`` so a dead
  URL isn't hammered every run. Entries older than ``CACHE_TTL_DAYS`` expire.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

# A realistic desktop-browser User-Agent. The previous self-identifying bot UA
# got 403'd by some publishers (e.g. Bethesda Magazine) that 200 fine for a
# normal browser. Validated live 2026-05-31.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

CACHE_TTL_DAYS = 14

# Lazily resolved trafilatura module (or None if unavailable).
# Guarded by a lock because enrichment runs the extractor from a
# ThreadPoolExecutor: without it, one thread could flip _TRAFILATURA_CHECKED
# before its `import trafilatura` completed, so sibling threads would observe
# CHECKED=True / _TRAFILATURA=None and silently disable extraction for the rest
# of the batch (no fetch, no cache write).
_TRAFILATURA = None
_TRAFILATURA_CHECKED = False
_TRAFILATURA_LOCK = threading.Lock()


def _trafilatura():
    global _TRAFILATURA, _TRAFILATURA_CHECKED
    if _TRAFILATURA_CHECKED:
        return _TRAFILATURA
    with _TRAFILATURA_LOCK:
        if not _TRAFILATURA_CHECKED:
            try:
                import trafilatura  # type: ignore

                _TRAFILATURA = trafilatura
            except Exception:
                log.warning(
                    "trafilatura not installed — full-text extraction disabled. "
                    "`pip install trafilatura` to enable; falling back to RSS summaries."
                )
                _TRAFILATURA = None
            # Set the flag LAST, after _TRAFILATURA is assigned, so a concurrent
            # reader never sees CHECKED=True with the module still unresolved.
            _TRAFILATURA_CHECKED = True
    return _TRAFILATURA


# ── Google News link resolution ────────────────────────────────────────────
# Many feeds hand us interstitial links like
#   https://news.google.com/rss/articles/<base64>?oc=5
# trafilatura just sees Google's consent/redirect shell on those, so the real
# article is never extracted (and the opaque URL also weakens relevance scoring
# and the candidate-name filter downstream). We resolve them to the publisher
# URL before fetching.
#
# Resolution order (first hit wins; best-effort, returns the original on miss):
#   1. The ``googlenewsdecoder`` library, which calls Google's ``batchexecute``
#      endpoint. Required for the modern format (post-2022) where the path
#      segment is an OPAQUE TOKEN, not the URL — validated live 2026-05-31 at
#      ~11/13 feed items vs 0/13 for the base64/redirect heuristics below.
#   2. Decoding the base64 path segment (works only for the legacy format that
#      embedded the target URL verbatim).
#   3. Following HTTP redirects.
# Steps 2–3 are retained as zero-dependency fallbacks for when the library is
# absent or its endpoint rate-limits.
_GNEWS_HOSTS = ("news.google.com",)

# Lazily resolved googlenewsdecoder module (or None). Lock-guarded for the same
# reason as trafilatura: _resolve_url runs inside the enrichment ThreadPoolExecutor.
_GNEWS_DECODER = None
_GNEWS_DECODER_CHECKED = False
_GNEWS_DECODER_LOCK = threading.Lock()


def _gnews_decoder():
    """Return the googlenewsdecoder ``gnewsdecoder`` callable, or None."""
    global _GNEWS_DECODER, _GNEWS_DECODER_CHECKED
    if _GNEWS_DECODER_CHECKED:
        return _GNEWS_DECODER
    with _GNEWS_DECODER_LOCK:
        if not _GNEWS_DECODER_CHECKED:
            try:
                from googlenewsdecoder import gnewsdecoder  # type: ignore

                _GNEWS_DECODER = gnewsdecoder
            except Exception:
                log.info(
                    "googlenewsdecoder not installed — Google News links fall back "
                    "to base64/redirect heuristics (low hit rate on the modern "
                    "format). `pip install googlenewsdecoder` to enable."
                )
                _GNEWS_DECODER = None
            _GNEWS_DECODER_CHECKED = True
    return _GNEWS_DECODER


def _looks_like_google_news(url: str) -> bool:
    try:
        return any(h in urlparse(url).netloc.lower() for h in _GNEWS_HOSTS)
    except Exception:
        return False


def _decode_google_news_url(url: str) -> Optional[str]:
    """Pull the real article URL out of a Google News base64 path segment.
    Returns the decoded http(s) URL, or None if it isn't recoverable."""
    try:
        seg = urlparse(url).path.rstrip("/").split("/")[-1]
        if not seg:
            return None
        raw = base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))
    except Exception:
        return None
    # The decoded protobuf-ish blob usually contains the target URL verbatim.
    m = re.search(rb"https?://[\x21-\x7e]+", raw)
    if not m:
        return None
    # Trim trailing protobuf field bytes that can run past the URL.
    cand = re.split(rb"[^\x21-\x7e]", m.group(0))[0]
    try:
        out = cand.decode("ascii")
    except Exception:
        return None
    if out.startswith("http") and "." in out and not _looks_like_google_news(out):
        return out
    return None


def _resolve_url(url: str, *, timeout: int = 12) -> str:
    """Resolve a Google News interstitial link to the real publisher URL.
    Non-Google URLs are returned unchanged."""
    if not _looks_like_google_news(url):
        return url

    # 1. googlenewsdecoder (handles the modern opaque-token format).
    decoder = _gnews_decoder()
    if decoder is not None:
        try:
            res = decoder(url, interval=0)
            decoded = res.get("decoded_url") if isinstance(res, dict) else None
            if res and res.get("status") and decoded and not _looks_like_google_news(decoded):
                return decoded
        except Exception as e:
            log.info("extract: googlenewsdecoder failed for %s — %s", url, e)

    # 2. Legacy base64 path segment.
    decoded = _decode_google_news_url(url)
    if decoded:
        return decoded

    # 3. HTTP redirect.
    try:
        r = requests.get(url, timeout=timeout, headers=_HEADERS, allow_redirects=True)
        if r.url and not _looks_like_google_news(r.url):
            return r.url
    except Exception:
        pass
    return url


def _cache_dir() -> Path:
    d = Path(__file__).resolve().parent.parent.parent / "data" / "cache" / "articles"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _cache_path(url: str) -> Path:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return _cache_dir() / f"{h}.json"


def _read_cache(url: str) -> Optional[Dict]:
    p = _cache_path(url)
    try:
        if not p.exists():
            return None
        if (time.time() - p.stat().st_mtime) > CACHE_TTL_DAYS * 86400:
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(url: str, data: Dict) -> None:
    try:
        _cache_path(url).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:  # cache is best-effort
        log.debug("extract: cache write failed for %s — %s", url, e)


def extract_from_html(html: str, url: str = "", *, min_chars: int = 250) -> Optional[Dict]:
    """
    Run trafilatura over already-fetched HTML. Separated from network I/O so it
    can be unit-tested against fixtures. Returns the result dict or None.
    """
    traf = _trafilatura()
    if traf is None or not html:
        return None
    try:
        raw = traf.extract(
            html,
            include_comments=False,
            include_tables=False,
            with_metadata=True,
            output_format="json",
            url=url or None,
        )
        if not raw:
            return None
        meta = json.loads(raw)
        text = (meta.get("text") or "").strip()
        if len(text) < min_chars:
            return None
        return {
            "text": text,
            "title": (meta.get("title") or "").strip(),
            "date": (meta.get("date") or "").strip(),
            "author": (meta.get("author") or "").strip(),
            "url": url or (meta.get("source") or "").strip(),
        }
    except Exception as e:
        log.info("extract: parse failed %s — %s", url, e)
        return None


def extract_article(
    url: str,
    *,
    timeout: int = 12,
    min_chars: int = 250,
    use_cache: bool = True,
) -> Optional[Dict]:
    """
    Fetch ``url`` and extract clean article text + metadata.

    Returns ``{"text", "title", "date", "author", "url"}`` or ``None`` on any
    failure, when trafilatura is unavailable, or when the body is too short to
    be worth more than the RSS summary.
    """
    if not url or not url.startswith("http"):
        return None

    if use_cache:
        cached = _read_cache(url)
        if cached is not None:
            # Empty dict == cached negative result.
            return cached or None

    if _trafilatura() is None:
        return None

    # Resolve Google News interstitial links to the real publisher URL first.
    fetch_url = _resolve_url(url, timeout=timeout)

    try:
        r = requests.get(fetch_url, timeout=timeout, headers=_HEADERS)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        log.info("extract: fetch failed %s — %s", fetch_url, e)
        if use_cache:
            _write_cache(url, {})
        return None

    result = extract_from_html(html, fetch_url, min_chars=min_chars)
    if use_cache:
        _write_cache(url, result or {})
    return result


if __name__ == "__main__":  # quick manual check: python -m scanner.sources.extract <url>
    import sys

    logging.basicConfig(level=logging.INFO)
    target = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    out = extract_article(target, use_cache=False)
    if out:
        print(f"title : {out['title']}")
        print(f"date  : {out['date']}")
        print(f"author: {out['author']}")
        print(f"chars : {len(out['text'])}")
        print(out["text"][:400])
    else:
        print("No extraction (failure, too short, or trafilatura missing).")
