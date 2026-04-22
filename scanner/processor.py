"""
AI processor: uses Claude API to enrich raw events.
For each event it:
  - Writes a plain-English 2-sentence summary (no jargon)
  - Scores relevance (0–1) for the configured resident
  - Identifies topical categories
  - Extracts mentioned politicians and their roles/stances

Uses prompt caching for the system prompt to save tokens on daily runs.
"""
import json
import logging
import re
from typing import List, Dict, Tuple

import anthropic

from config import Config as _Cfg

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"   # Fast + cheap for daily batch processing

_LOCALE = ", ".join(p for p in [_Cfg.CITY, _Cfg.COUNTY, _Cfg.STATE] if p) or "the configured locale"
_FED_KW = ", ".join(_Cfg.FEDERAL_KEYWORDS[:12]) if _Cfg.FEDERAL_KEYWORDS else "budget, healthcare, education, housing, transportation"
_DISTRICTS = _Cfg.districts_profile()
_DISTRICT_BLOCK = f"\n\nThe resident's voting districts are:\n{_DISTRICTS}\nGive HIGHER relevance scores to items touching the offices, legislators, or contests in those specific districts.\n" if _DISTRICTS else ""

# ── System prompt (cached) ────────────────────────────────────────────────────
SYSTEM_PROMPT = f"""You are a nonpartisan political analyst helping a resident of
{_LOCALE} understand local, state, and federal politics.{_DISTRICT_BLOCK}
The resident is new to voting and wants clear, jargon-free explanations.
Their key interests are:
  - FEDERAL: topics matching these keywords — {_FED_KW}
  - STATE: state legislature bills and laws (all topics)
  - COUNTY: county council ordinances, public hearings, budget
  - SCHOOL: school board decisions, curriculum, funding
  - LOCAL: police policy, fire/rescue, healthcare/hospitals

When analyzing events, you always:
1. Write 2 plain-English sentences that explain what this means for an average resident.
   Avoid political jargon. Explain WHY it matters.
2. Score relevance 0.0–1.0 for THIS resident (higher = more directly affects their daily life).
3. Identify categories from: [tax, education, visa, health, police, fire,
   housing, budget, election, environment, transportation, business, trade, other]
4. List any politicians mentioned by full name, and their role/stance in this event.

Always respond in strict JSON — no prose outside the JSON block."""


def _make_client(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key)


def process_batch(api_key: str, events: List[Dict],
                  batch_size: int = 8) -> List[Dict]:
    """
    Enrich a list of raw events with AI-generated fields.
    Processes in batches to stay within token limits.
    Returns enriched events (modifies in-place and returns them).
    """
    if not api_key:
        log.warning("No ANTHROPIC_API_KEY — skipping AI enrichment")
        return events

    client = _make_client(api_key)
    enriched = []

    for i in range(0, len(events), batch_size):
        batch = events[i: i + batch_size]
        enriched.extend(_process_batch(client, batch))

    return enriched


def _process_batch(client: anthropic.Anthropic, events: List[Dict]) -> List[Dict]:
    """Process a single batch of up to batch_size events."""
    # Build a compact representation of each event for the prompt
    items = []
    for idx, ev in enumerate(events):
        text = f"{ev.get('title', '')}. {ev.get('description', '')} {ev.get('raw_content', '')}"
        items.append({
            "index": idx,
            "level": ev.get("level", ""),
            "title": ev.get("title", "")[:200],
            "text": text[:800],
        })

    user_content = (
        "Analyze the following political events and return a JSON array.\n"
        "Each element must have: index, summary, relevance_score, categories, politicians.\n"
        "politicians is an array of {name, role, stance} objects.\n\n"
        f"Events:\n{json.dumps(items, ensure_ascii=False, indent=2)}"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},  # Cache the long system prompt
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text
        results = _parse_json_response(raw)
    except Exception as e:
        log.error("Claude API error: %s", e)
        return events  # Return unenriched on error

    # Merge AI results back into event dicts
    for item in results:
        idx = item.get("index")
        if idx is None or idx >= len(events):
            continue
        ev = events[idx]
        ev["summary"] = item.get("summary", "")
        ev["relevance_score"] = float(item.get("relevance_score", 0))
        ev["categories"] = item.get("categories", ev.get("categories", []))
        # Store politicians for later linking
        ev["_politicians"] = item.get("politicians", [])

    return events


def _parse_json_response(text: str) -> List[Dict]:
    """Extract and parse a JSON array from Claude's response."""
    text = text.strip()
    # Claude sometimes wraps in ```json ... ```
    if "```" in text:
        start = text.find("```")
        end = text.rfind("```")
        text = text[start:end].lstrip("`").lstrip("json").strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "results" in data:
            return data["results"]
    except json.JSONDecodeError as e:
        log.warning("JSON parse error: %s\nRaw: %.300s", e, text)
    return []


_INCIDENT_PATTERNS = re.compile(
    r"\b(arrested|charged|sentenced|found guilty|convicted|killed|died|"
    r"shooting at|shot at|stabbed at|robbery at|robbery on|crash on|"
    r"crash at|accident on|accident at|fire at|fire on)\b",
    re.IGNORECASE,
)

_AGGREGATE_PATTERNS = re.compile(
    r"\b(crime rate|crime statistic|police report|annual report|trend|"
    r"increase in|decrease in|data show|statistics|percent|monthly report|"
    r"quarterly report|year-over-year|compared to last year)\b",
    re.IGNORECASE,
)


def is_individual_incident(event: Dict) -> bool:
    """
    Return True if this event is an individual crime/accident incident (not a trend/policy).
    Used to filter podcast content toward systemic issues.
    """
    title = (event.get("title") or "").strip()
    if not title:
        return False
    if _AGGREGATE_PATTERNS.search(title):
        return False
    return bool(_INCIDENT_PATTERNS.search(title))


def score_federal_relevance(event: Dict, keywords: List[str]) -> float:
    """
    Quick pre-filter score for federal events (before spending Claude tokens).
    Returns 0.0 if the title/description doesn't mention any keyword.
    """
    text = f"{event.get('title', '')} {event.get('description', '')}".lower()
    matches = sum(1 for kw in keywords if kw.lower() in text)
    if matches == 0:
        return 0.0
    return min(0.4 + matches * 0.15, 1.0)
