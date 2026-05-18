"""
Dossier source importer — pulls every citation URL out of a dossier .md file
and stores it permanently in the `candidate_sources` table.

Why this exists
---------------
Dossier markdown files live in `cowork_outbox/` and `data/candidate_dossiers/`
and are essentially scratch deliverables. Every citation in them — the
Wikipedia link, the Maryland Matters article, the campaign-website "About"
page — is research evidence that should survive being deleted, archived, or
overwritten by a fresh dossier run. This importer is the long-term capture
point: it runs after every dossier finalises and accumulates the (politician,
URL) pairs into a permanent DB record so the renderer can show a complete
sources list across all runs, not just whatever is in the file on disk today.

Two dossier formats in the wild
-------------------------------
1. **Inline citations** (most common):

       The candidate's filing is confirmed on Ballotpedia
       [src: https://ballotpedia.org/J.D._Kumar] [src: https://elections.maryland.gov/...]

   Pattern: `[src: <URL>]`. URL stops at `]` or whitespace.

2. **Trailing markdown bibliographies** (Friedson style):

       ... Council President in 2023–2024.
       ([andrewfriedson.com — About](https://andrewfriedson.com/meet-andrew/),
        [Maryland Matters — campaign launch](https://marylandmatters.org/...))

   Pattern: `[<link text>](<URL>)` — link text becomes the title.

We extract both shapes. Same URL repeated across formats dedups on
UNIQUE(politician_name, url) in the DB layer.

Public surface
--------------
- `import_dossier_sources(db_path, dossier_path, politician_name, dossier_date)`
    — process one .md file, upsert its citations
- `import_all_dossiers(db_path, dirs=None, verbose=False)`
    — sweep every dossier file under known directories
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Citation patterns. Both are non-greedy and stop at the obvious terminator.
_INLINE_SRC_RE   = re.compile(r"\[src:\s*(https?://[^\]\s]+)\s*\]")
_MD_LINK_RE      = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")

# How much surrounding text to capture as `raw_excerpt`.
_EXCERPT_BEFORE  = 200
_EXCERPT_AFTER   = 60

# Domain → source_type. First matching rule wins.
_TYPE_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"(^|\.)wikipedia\.org$",                re.I), "biography"),
    (re.compile(r"(^|\.)ballotpedia\.org$",              re.I), "biography"),
    (re.compile(r"(^|\.)congress\.gov$|history\.house\.gov$", re.I), "voting_record"),
    (re.compile(r"votesmart\.org$|govtrack\.us$",        re.I), "voting_record"),
    # .gov domains — public records
    (re.compile(r"\.gov$",                                re.I), "official"),
    (re.compile(r"\.gov\.\w+$",                          re.I), "official"),
    # Press / local journalism
    (re.compile(r"marylandmatters\.org$|bethesdamagazine\.com$|"
                r"wtop\.com$|washingtonpost\.com$|"
                r"baltimoresun\.com$|wypr\.org$|wamu\.org$|"
                r"montgomeryperspective\.com$|moco360\.media$",
                re.I), "press"),
    # Campaign sites — heuristic: candidate name in domain, or /meet-, /about
    (re.compile(r"vote[\w-]*\.com$|forcouncil|forcongress|"
                r"campaign$|electjohn|electjane", re.I), "campaign"),
]
# Path-level campaign markers — checked after domain rules fail.
_CAMPAIGN_PATH_RE = re.compile(r"/(meet|about|biography|issues|platform)(/|$|-)", re.I)


def classify_source(url: str) -> str:
    """Map a URL to a `source_type`. Falls back to 'other'."""
    try:
        host = urlparse(url).hostname or ""
        path = urlparse(url).path or ""
    except Exception:
        return "other"
    for rx, kind in _TYPE_RULES:
        if rx.search(host):
            return kind
    if _CAMPAIGN_PATH_RE.search(path):
        return "campaign"
    return "other"


def _fallback_title(url: str) -> str:
    """Last-resort title when the dossier didn't give one: `domain — /path`."""
    try:
        u = urlparse(url)
        host = (u.hostname or "").replace("www.", "")
        path = (u.path or "/").rstrip("/")
        if path in ("", "/"):
            return host
        return f"{host}{path}"
    except Exception:
        return url


def _excerpt(text: str, start: int, end: int) -> str:
    """Slice ~200 chars before + ~60 after a citation, trimmed to nearest
    sentence boundary if reasonable. Whitespace normalised."""
    lo = max(0, start - _EXCERPT_BEFORE)
    hi = min(len(text), end + _EXCERPT_AFTER)
    chunk = text[lo:hi]
    # Trim to nearest leading sentence boundary if we cut mid-sentence
    if lo > 0:
        for sep in (". ", "? ", "! ", "\n"):
            i = chunk.find(sep)
            if 0 <= i < 80:        # only trim if boundary is close to the start
                chunk = chunk[i + len(sep):]
                break
    return re.sub(r"\s+", " ", chunk).strip()


def _slug_to_name_map(registry: Optional[Dict] = None) -> Dict[str, str]:
    """Build {slug: canonical_name} from the series registry. Same slug logic
    as `scanner.series._slug` so filename slugs round-trip."""
    if registry is None:
        try:
            from scanner.series import load_registry
            registry = load_registry()
        except Exception:
            return {}
    try:
        from scanner.series import _slug
    except Exception:
        return {}
    out: Dict[str, str] = {}
    for c in (registry or {}).get("candidates", []):
        name = (c.get("name") or "").strip()
        if not name:
            continue
        s = _slug(name)
        out[s] = name
        # Common slug variant: drop dots ("J. D. Kumar" → "j-d-kumar" vs "jd-kumar")
        compact = re.sub(r"[^\w-]", "", s)
        if compact != s:
            out[compact] = name
        # Strip leading hyphens
        if compact.startswith("-"):
            out[compact.lstrip("-")] = name
    return out


def _resolve_name_from_filename(path: Path,
                                 slug_map: Dict[str, str]
                                 ) -> Tuple[Optional[str], Optional[str]]:
    """Filename → (canonical_name, dossier_date_iso). Two filename shapes:
      • cowork_outbox/dossier_<YYYY-MM-DD>_<slug>.md   (date-prefixed)
      • data/candidate_dossiers/<slug>.md              (no date)
    Returns (None, None) if the slug doesn't resolve in the registry — caller
    should skip the file with a warning.
    """
    m = re.fullmatch(r"dossier_(\d{4}-\d{2}-\d{2})_(.+)", path.stem)
    if m:
        dossier_date = m.group(1)
        slug = m.group(2)
    else:
        dossier_date = None
        slug = path.stem
    if slug in ("2",):  # known stray
        return None, None
    name = slug_map.get(slug)
    if not name:
        # Try the hyphen-collapsed variant too (e.g. "jd-kumar" vs "j-d-kumar")
        compact = re.sub(r"[^\w-]", "", slug)
        name = slug_map.get(compact)
    return name, dossier_date


# ──────────────────────────────────────────────────────────────────────────────

def import_dossier_sources(db_path: Path,
                             dossier_path: Path,
                             politician_name: str,
                             dossier_date: Optional[str] = None) -> int:
    """Pull every citation URL out of `dossier_path` and upsert to
    `candidate_sources`. Returns the number of distinct URLs imported.

    Non-fatal — logs and returns 0 on any error so callers (including
    `mark_done` in the cowork bridge) can ignore the result and continue.
    """
    if not dossier_path.exists():
        log.warning("dossier_importer: file missing: %s", dossier_path)
        return 0
    if not politician_name:
        log.warning("dossier_importer: no politician_name for %s", dossier_path)
        return 0

    try:
        text = dossier_path.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("dossier_importer: read failed for %s — %s", dossier_path, e)
        return 0

    # First-pass: walk inline [src: URL] citations
    found: Dict[str, Dict] = {}   # url → {title, raw_excerpt}
    for m in _INLINE_SRC_RE.finditer(text):
        u = m.group(1).rstrip(".,;:")
        if u not in found:
            found[u] = {
                "title":       _fallback_title(u),
                "raw_excerpt": _excerpt(text, m.start(), m.end()),
            }

    # Second-pass: markdown-link parentheticals — these supply a real title
    for m in _MD_LINK_RE.finditer(text):
        link_text, u = m.group(1).strip(), m.group(2).rstrip(".,;:")
        # Skip image syntax (![alt](url)) and obviously non-citation links
        if m.start() > 0 and text[m.start() - 1] == "!":
            continue
        if u not in found:
            found[u] = {"title": link_text, "raw_excerpt": _excerpt(text, m.start(), m.end())}
        else:
            # We already saw this URL via [src:]. Upgrade its title if the
            # markdown form gives us something better than the domain fallback.
            if found[u]["title"] == _fallback_title(u):
                found[u]["title"] = link_text

    if not found:
        return 0

    from scanner.database import upsert_candidate_source
    today_iso = date.today().isoformat()
    n = 0
    for url, meta in found.items():
        try:
            upsert_candidate_source(
                db_path,
                politician_name=politician_name,
                url=url,
                title=meta["title"],
                summary=meta["raw_excerpt"][:280],
                source_type=classify_source(url),
                date_collected=today_iso,
                dossier_date=dossier_date,
                raw_excerpt=meta["raw_excerpt"],
            )
            n += 1
        except Exception as e:
            log.warning("dossier_importer: upsert failed for %s — %s", url, e)
    log.info("dossier_importer: imported %d source(s) for %s from %s",
             n, politician_name, dossier_path.name)
    return n


def import_all_dossiers(db_path: Path,
                          dirs: Optional[List[Path]] = None,
                          verbose: bool = False) -> Dict[str, int]:
    """Sweep every dossier .md file under `dirs` (default: cowork_outbox/ +
    data/candidate_dossiers/) and import each one.

    Returns a stats dict: {files_seen, files_imported, files_skipped, urls_total}.
    Idempotent thanks to UNIQUE(politician_name, url) — safe to run repeatedly.
    """
    from config import Config as _Cfg
    if dirs is None:
        dirs = [
            _Cfg.BASE_DIR / "cowork_outbox" if hasattr(_Cfg, "BASE_DIR") else Path("cowork_outbox"),
            _Cfg.BASE_DIR / "data" / "candidate_dossiers" if hasattr(_Cfg, "BASE_DIR") else Path("data/candidate_dossiers"),
        ]

    slug_map = _slug_to_name_map()
    stats = {"files_seen": 0, "files_imported": 0,
             "files_skipped": 0, "urls_total": 0}

    for d in dirs:
        if not d.exists():
            continue
        for f in sorted(d.glob("dossier_*.md")) + sorted(d.glob("*.md")):
            if f.name.startswith("_"):
                continue
            # OneDrive cloud-only files raise WinError 1920 on stat(). Catch
            # so one stuck file doesn't kill the batch — log and skip it.
            try:
                is_file = f.is_file()
            except OSError as e:
                log.warning("dossier_importer: skip unreadable %s — %s", f.name, e)
                stats["files_skipped"] += 1
                continue
            if not is_file:
                continue
            stats["files_seen"] += 1
            name, ddate = _resolve_name_from_filename(f, slug_map)
            if not name:
                if verbose:
                    log.info("  skip (unresolved slug): %s", f.name)
                stats["files_skipped"] += 1
                continue
            n = import_dossier_sources(db_path, f, name, ddate)
            if n:
                stats["files_imported"] += 1
                stats["urls_total"] += n
            else:
                stats["files_skipped"] += 1
            if verbose:
                log.info("  %-50s → %s  (%d urls)", f.name, name, n)

    return stats
