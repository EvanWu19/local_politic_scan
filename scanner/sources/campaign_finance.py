"""
Campaign-finance enrichment for candidate dossiers (OSS plan, item 6).

Two sources, both optional and fault-tolerant:
  • Maryland SBE campaign-finance CSV — covers Delegate/Council/local races the
    FEC does not. Download from
    https://campaignfinance.maryland.gov (Reports → Data Download) and point
    Config.SBE_FINANCE_CSV at the file; we parse + name-match locally.
  • openFEC API — federal candidates (US House/Senate/President). Needs
    FEC_API_KEY (api.data.gov; DEMO_KEY works at low volume).

`finance_summary(name, ...)` returns a normalised dict or None; never raises.
`format_finance_block(summary)` renders a Markdown block for a dossier brief.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

import requests

log = logging.getLogger(__name__)

FEC_BASE = "https://api.open.fec.gov/v1"

# Column-name aliases seen across SBE export variants (lowercased, stripped).
_SBE_COLS = {
    "committee": ("committee name", "committee", "filer name", "name"),
    "candidate": ("candidate name", "candidate", "name"),
    "raised": ("total contributions", "contributions", "total raised", "receipts"),
    "spent": ("total expenditures", "expenditures", "total spent", "disbursements"),
    "cash": ("cash on hand", "ending balance", "balance"),
    "office": ("office", "office sought"),
    "period": ("filing period", "report period", "period", "reporting period"),
}


def _money(v: str) -> Optional[float]:
    if v is None:
        return None
    s = re.sub(r"[^0-9.\-]", "", str(v))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


def _pick(row: Dict[str, str], keys) -> str:
    low = {(k or "").strip().lower(): v for k, v in row.items()}
    for k in keys:
        if k in low and (low[k] or "").strip():
            return low[k].strip()
    return ""


def parse_sbe_csv(text: str) -> List[Dict]:
    """Parse SBE campaign-finance CSV text into normalised rows. Tolerant of
    column-name variation; unknown columns are ignored."""
    rows: List[Dict] = []
    try:
        reader = csv.DictReader(io.StringIO(text))
        for raw in reader:
            committee = _pick(raw, _SBE_COLS["committee"])
            candidate = _pick(raw, _SBE_COLS["candidate"]) or committee
            if not (committee or candidate):
                continue
            rows.append({
                "committee": committee,
                "candidate": candidate,
                "office": _pick(raw, _SBE_COLS["office"]),
                "period": _pick(raw, _SBE_COLS["period"]),
                "total_raised": _money(_pick(raw, _SBE_COLS["raised"])),
                "total_spent": _money(_pick(raw, _SBE_COLS["spent"])),
                "cash_on_hand": _money(_pick(raw, _SBE_COLS["cash"])),
                "source": "Maryland SBE",
            })
    except Exception as e:
        log.warning("parse_sbe_csv failed: %s", e)
    return rows


def _name_key(s: str) -> str:
    return " ".join(sorted(re.sub(r"[^a-z ]", " ", (s or "").lower()).split()))


def match_sbe_candidate(name: str, rows: List[Dict]) -> Optional[Dict]:
    """Find the SBE row whose candidate/committee best matches `name`
    (token-set match on the person's name)."""
    toks = set(re.sub(r"[^a-z ]", " ", (name or "").lower()).split())
    toks = {t for t in toks if len(t) > 1}
    if not toks:
        return None
    best, best_score = None, 0
    for r in rows:
        hay = set(re.sub(r"[^a-z ]", " ", f"{r.get('candidate','')} {r.get('committee','')}".lower()).split())
        score = len(toks & hay)
        if score > best_score:
            best, best_score = r, score
    # require at least 2 overlapping name tokens (first + last)
    return best if best_score >= 2 else None


def fetch_sbe_finance(name: str, csv_path: Optional[str] = None) -> Optional[Dict]:
    """Look up a candidate in the local SBE CSV (Config.SBE_FINANCE_CSV by default)."""
    path = csv_path
    if path is None:
        try:
            from config import Config as _Cfg
            path = getattr(_Cfg, "SBE_FINANCE_CSV", "") or ""
        except Exception:
            path = ""
    if not path or not Path(path).exists():
        return None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.warning("fetch_sbe_finance read failed: %s", e)
        return None
    return match_sbe_candidate(name, parse_sbe_csv(text))


def fetch_fec_finance(name: str, api_key: str, cycle: int = 2026) -> Optional[Dict]:
    """Look up a federal candidate's totals via openFEC. Returns None on any failure."""
    if not api_key or not name:
        return None
    try:
        s = requests.get(f"{FEC_BASE}/candidates/search/",
                         params={"q": name, "api_key": api_key, "per_page": 1,
                                 "election_year": cycle}, timeout=15)
        s.raise_for_status()
        results = s.json().get("results", [])
        if not results:
            return None
        cid = results[0].get("candidate_id")
        name_out = results[0].get("name", name)
        t = requests.get(f"{FEC_BASE}/candidate/{cid}/totals/",
                        params={"api_key": api_key, "cycle": cycle, "per_page": 1},
                        timeout=15)
        t.raise_for_status()
        tr = t.json().get("results", [])
        tot = tr[0] if tr else {}
        return {
            "committee": name_out,
            "candidate": name_out,
            "office": results[0].get("office_full", ""),
            "period": f"{cycle} cycle",
            "total_raised": tot.get("receipts"),
            "total_spent": tot.get("disbursements"),
            "cash_on_hand": tot.get("last_cash_on_hand_end_period"),
            "source": "openFEC",
        }
    except Exception as e:
        log.warning("fetch_fec_finance failed: %s", e)
        return None


def _fmt(v: Optional[float]) -> str:
    return f"${v:,.0f}" if isinstance(v, (int, float)) else "n/a"


def format_finance_block(summary: Optional[Dict]) -> str:
    """Render a Markdown block for inclusion in a dossier brief. '' if no data."""
    if not summary:
        return ""
    return (
        f"\n**Campaign finance ({summary.get('source','')}, "
        f"{summary.get('period','') or 'latest'}):** "
        f"raised {_fmt(summary.get('total_raised'))}, "
        f"spent {_fmt(summary.get('total_spent'))}, "
        f"cash on hand {_fmt(summary.get('cash_on_hand'))}"
        f"{(' · ' + summary['office']) if summary.get('office') else ''}.\n"
    )


def finance_summary(name: str, *, federal: bool = False,
                    fec_api_key: str = "", sbe_csv: Optional[str] = None) -> Optional[Dict]:
    """One-call lookup for the dossier pipeline: SBE first (covers MD local
    races), then openFEC for federal candidates. Returns a normalised summary
    dict or None. Never raises."""
    try:
        local = fetch_sbe_finance(name, sbe_csv)
        if local:
            return local
        if federal and fec_api_key:
            return fetch_fec_finance(name, fec_api_key)
    except Exception as e:
        log.warning("finance_summary(%s) failed: %s", name, e)
    return None
