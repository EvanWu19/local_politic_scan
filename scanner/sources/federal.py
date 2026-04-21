"""
Federal source: Congress.gov API.
Only fetches bills/actions matching Config.FEDERAL_KEYWORDS (set in .env).
API key: https://api.congress.gov/sign-up/
"""
import requests
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

log = logging.getLogger(__name__)

CONGRESS_BASE = "https://api.congress.gov/v3"


def _get(path: str, api_key: str, params: dict) -> Optional[dict]:
    params["api_key"] = api_key
    params["format"] = "json"
    try:
        r = requests.get(f"{CONGRESS_BASE}{path}", params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("Congress.gov request failed: %s", e)
        return None


def fetch_bills(api_key: str, keywords: List[str],
                days_back: int = 7, max_per_keyword: int = 5) -> List[Dict]:
    """
    Search Congress.gov for recent bills matching each keyword.
    Returns deduplicated list of event dicts.
    """
    if not api_key:
        log.warning("No CONGRESS_API_KEY set — skipping federal bills")
        return []

    seen_urls: set = set()
    results: List[Dict] = []

    # Deduplicate keywords to avoid redundant calls
    search_terms = list(dict.fromkeys(keywords))

    for term in search_terms[:12]:  # cap API calls
        data = _get(
            "/bill",
            api_key,
            {
                "query": term,
                "limit": max_per_keyword,
                "sort": "updateDate+desc",
            },
        )
        if not data:
            continue

        for bill in data.get("bills", []):
            url = bill.get("url", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            # Parse update date
            updated_str = bill.get("updateDate", "")
            try:
                updated = datetime.strptime(updated_str[:10], "%Y-%m-%d").date()
            except Exception:
                updated = None

            # Skip if too old
            if updated and (datetime.now().date() - updated).days > days_back * 3:
                continue

            congress = bill.get("congress", "")
            bill_type = bill.get("type", "")
            bill_num = bill.get("number", "")
            number_str = f"{bill_type}{bill_num} ({congress}th Congress)" if congress else f"{bill_type}{bill_num}"

            latest_action = bill.get("latestAction", {})
            sponsors = bill.get("sponsors", [])
            sponsor_names = [s.get("fullName", s.get("lastName", "")) for s in sponsors]

            results.append({
                "title": bill.get("title", f"Federal Bill {number_str}"),
                "type": "bill",
                "level": "federal",
                "date": str(updated) if updated else "",
                "source_url": url,
                "source_name": "Congress.gov",
                "bill_number": number_str,
                "status": latest_action.get("text", ""),
                "description": (
                    f"Sponsor(s): {', '.join(sponsor_names)}. "
                    f"Latest action: {latest_action.get('text', 'N/A')}"
                ),
                "raw_content": bill.get("title", ""),
                "categories": ["federal"],
                "sponsors": sponsor_names,
                "_search_term": term,
            })

    log.info("Federal: fetched %d bills", len(results))
    return results


def fetch_member_votes(api_key: str, bioguide_id: str,
                       max_items: int = 10) -> List[Dict]:
    """Fetch recent votes for a specific Congress member (by bioguide ID)."""
    if not api_key:
        return []
    data = _get(
        f"/member/{bioguide_id}/votes",
        api_key,
        {"limit": max_items},
    )
    if not data:
        return []
    return data.get("votes", [])
