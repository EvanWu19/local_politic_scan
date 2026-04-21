"""
Q&A chat agent for follow-up questions after listening to the daily podcast.

Responsibilities:
  1. Answer the user's question with recent digest data as context
  2. Save each message to SQLite (conversations + messages tables)
  3. When appropriate, extract a crisp "knowledge note" from the exchange and
     save it as a Markdown file in knowledge/YYYY-MM/<slug>.md, indexed in DB

Knowledge notes are the long-term memory — small atomic facts the user learned,
tagged by topic and politician, searchable later when making voting decisions.
"""
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import anthropic

log = logging.getLogger(__name__)

from config import Config as _Cfg

_LOCALE = ", ".join(p for p in [_Cfg.CITY, _Cfg.COUNTY, _Cfg.STATE] if p) or "their locale"

# ── System prompt (cached) ────────────────────────────────────────────────────
CHAT_SYSTEM = f"""You are the user's personal political knowledge coach.

The user lives in {_LOCALE}. They are new to voting and use a daily digest of
local/state/federal political news to prepare for future elections. After
listening to the daily podcast, they ask you follow-up questions to deepen
understanding.

YOUR JOB:
  1. Answer questions clearly and concisely (2–5 paragraphs; use lists for 3+ items)
  2. Cite specific events/bills/politicians from the digest context when relevant —
     include the source_url as a link when you reference a specific story
  3. Explain jargon and acronyms on first use
  4. Be nonpartisan — describe positions fairly
  5. Connect abstract policy to concrete impact on the resident's daily life
     (taxes, commute, schools, safety, healthcare, etc.)
  6. If asked "what should I do?" — suggest concrete actions: attend hearing,
     email rep, register/update voter registration, etc.
  7. If the context doesn't have the answer, say so plainly and suggest where
     to look (specific government site, office, etc.)

VOICE:
  - Warm, clear, respectful of the user learning something new
  - No "As an AI..." hedging
  - Use Markdown formatting (headings, **bold**, bullet lists) — this renders
    in the chat UI"""


# ── Knowledge-extractor system prompt ─────────────────────────────────────────
EXTRACT_SYSTEM = (
    "You extract reusable knowledge notes from user↔assistant political "
    f"conversations. The user is a local voter in {_LOCALE} learning about politics "
    "for future elections. Extract notes that will help them make informed voting "
    "decisions later.\n\n"
    "When given a recent exchange, return STRICT JSON:\n"
    "{\n"
    '  "worth_saving": true|false,\n'
    '  "title": "<short noun-phrase title, under 70 chars>",\n'
    '  "content": "<2-6 sentence Markdown summary of what was learned>",\n'
    '  "topics": ["<tag>", ...],\n'
    '  "politicians": ["<full name>", ...]\n'
    "}\n\n"
    "RULES:\n"
    "  - Set worth_saving=false for casual chit-chat, greetings, or trivially restated\n"
    "    facts already in the digest. Only save genuine synthesis / insight / new facts.\n"
    '  - Topics use lowercase slugs (e.g. "tax", "school-budget", "police", "housing",\n'
    '    "zoning", "election-prep", "healthcare", "transportation").\n'
    "  - Content must be self-contained — readable 6 months later without the full chat.\n"
    "  - Output JSON only, no prose around it."
)


# ──────────────────────────────────────────────────────────────────────────────
# Core chat flow
# ──────────────────────────────────────────────────────────────────────────────

def handle_message(db_path: Path, knowledge_dir: Path,
                   anthropic_key: str, user_message: str,
                   conversation_id: Optional[int] = None,
                   chat_model: str = "claude-sonnet-4-5-20250929",
                   context_days: int = 3,
                   max_context_events: int = 30) -> Dict:
    """
    Process one user message. Returns {conversation_id, reply, note_saved}.
    Creates a new conversation if conversation_id is None.
    """
    from scanner.database import (
        create_conversation, add_message, get_conversation,
        update_conversation_title, get_recent_events,
    )

    client = anthropic.Anthropic(api_key=anthropic_key)

    # ── 1. Ensure conversation exists ────────────────────────────────────────
    if conversation_id is None:
        conversation_id = create_conversation(
            db_path, title="New chat", context_date=date.today()
        )

    conv = get_conversation(db_path, conversation_id)

    # ── 2. Build context from digest ─────────────────────────────────────────
    events = get_recent_events(db_path, days=context_days, min_relevance=0.0)
    # Sort by relevance, cap
    events = sorted(events, key=lambda e: -e.get("relevance_score", 0))[:max_context_events]
    context_text = _format_events_for_context(events)

    # ── 3. Append user message ───────────────────────────────────────────────
    add_message(db_path, conversation_id, "user", user_message)

    # ── 4. Build message history for Claude ──────────────────────────────────
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in conv.get("messages", [])
    ]
    # Add the new user message we just stored
    history.append({"role": "user", "content": user_message})

    # ── 5. Call Claude ───────────────────────────────────────────────────────
    try:
        resp = client.messages.create(
            model=chat_model,
            max_tokens=1500,
            system=[
                {"type": "text", "text": CHAT_SYSTEM,
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text",
                 "text": f"## Today's digest context (top {len(events)} stories)\n\n{context_text}",
                 "cache_control": {"type": "ephemeral"}},
            ],
            messages=history,
        )
        reply = resp.content[0].text
    except Exception as e:
        log.exception("Chat API error")
        reply = f"Sorry, I hit an error: `{e}`. Try again in a moment."

    # ── 6. Save assistant reply ──────────────────────────────────────────────
    add_message(db_path, conversation_id, "assistant", reply)

    # ── 7. If first exchange, generate a conversation title ─────────────────
    if len(history) <= 2 and conv.get("title", "").strip().lower() in ("", "new chat"):
        title = _generate_title(client, chat_model, user_message, reply)
        if title:
            update_conversation_title(db_path, conversation_id, title)

    # ── 8. Attempt knowledge-note extraction on this exchange ───────────────
    note_info = _maybe_save_knowledge_note(
        client, chat_model, db_path, knowledge_dir,
        conversation_id, user_message, reply,
    )

    return {
        "conversation_id": conversation_id,
        "reply": reply,
        "note_saved": bool(note_info),
        "note_title": note_info["title"] if note_info else None,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Knowledge-note extraction
# ──────────────────────────────────────────────────────────────────────────────

def _maybe_save_knowledge_note(client: anthropic.Anthropic, model: str,
                                db_path: Path, knowledge_dir: Path,
                                conversation_id: int,
                                user_msg: str, assistant_msg: str) -> Optional[Dict]:
    """Ask Claude if this exchange deserves a knowledge note; save if yes."""
    from scanner.database import save_knowledge_note

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=600,
            system=[{"type": "text", "text": EXTRACT_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{
                "role": "user",
                "content": f"USER:\n{user_msg}\n\nASSISTANT:\n{assistant_msg}",
            }],
        )
        raw = resp.content[0].text.strip()
        data = _extract_json(raw)
        if not data or not data.get("worth_saving"):
            return None

        title = data.get("title", "Untitled note")[:100]
        content = data.get("content", "")
        topics = data.get("topics", [])
        politicians = data.get("politicians", [])

        # Save as Markdown file in knowledge/YYYY-MM/<slug>.md
        slug = _slugify(title)
        today = date.today()
        md_dir = knowledge_dir / today.strftime("%Y-%m")
        md_dir.mkdir(parents=True, exist_ok=True)
        md_path = md_dir / f"{today.isoformat()}_{slug}.md"

        # Frontmatter + content
        md_body = (
            "---\n"
            f"title: {title}\n"
            f"date: {today.isoformat()}\n"
            f"conversation_id: {conversation_id}\n"
            f"topics: {json.dumps(topics)}\n"
            f"politicians: {json.dumps(politicians)}\n"
            "---\n\n"
            f"# {title}\n\n"
            f"{content}\n\n"
            "---\n\n"
            "## Source exchange\n\n"
            f"**Q:** {user_msg}\n\n"
            f"**A:** {assistant_msg}\n"
        )
        md_path.write_text(md_body, encoding="utf-8")

        # Index in DB
        rel_path = str(md_path.relative_to(knowledge_dir.parent))
        save_knowledge_note(
            db_path, conversation_id, title, content,
            topics, politicians, markdown_path=rel_path,
        )
        log.info("Saved knowledge note: %s → %s", title, md_path)
        return {"title": title, "path": str(md_path)}
    except Exception as e:
        log.warning("Knowledge note extraction failed: %s", e)
        return None


def _generate_title(client: anthropic.Anthropic, model: str,
                     user_msg: str, assistant_msg: str) -> str:
    """Produce a 3-6 word title for the conversation."""
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=40,
            system="Generate a concise 3-6 word title for this political Q&A exchange. Output only the title.",
            messages=[{"role": "user",
                       "content": f"Q: {user_msg[:300]}\nA: {assistant_msg[:300]}"}],
        )
        title = resp.content[0].text.strip().strip('"').strip("'")
        return title[:80]
    except Exception as e:
        log.debug("Title generation failed: %s", e)
        return ""


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _format_events_for_context(events: List[Dict]) -> str:
    """Format events as compact Markdown for Claude's context window."""
    lines = []
    for ev in events:
        title = ev.get("title", "")
        summary = ev.get("summary") or ev.get("description", "")
        level = ev.get("level", "")
        ev_type = ev.get("type", "")
        url = ev.get("source_url", "")
        pols = ev.get("politicians", "")
        relevance = ev.get("relevance_score", 0)
        lines.append(
            f"- **[{level}/{ev_type}]** {title}\n"
            f"  {summary[:280] if summary else ''}\n"
            f"  _Relevance: {relevance:.0%} · Politicians: {pols or 'none'} · URL: {url}_"
        )
    return "\n".join(lines)


def _extract_json(text: str) -> Optional[Dict]:
    """Parse a JSON object that may be wrapped in ```json ... ```."""
    text = text.strip()
    if "```" in text:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _slugify(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:60] or "note"
