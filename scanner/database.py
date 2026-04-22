"""
SQLite database layer — schema, seeding, and all CRUD operations.
"""
import sqlite3
import json
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List, Dict, Any


def get_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize_db(db_path: Path) -> None:
    """Create all tables and indexes if they don't exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS politicians (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT NOT NULL UNIQUE,
                office    TEXT,
                party     TEXT,
                level     TEXT,       -- federal / state / county / school / local
                district  TEXT,
                website   TEXT,
                bio       TEXT,
                last_updated DATE
            );

            CREATE TABLE IF NOT EXISTS events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT NOT NULL,
                type            TEXT,   -- bill/hearing/lawsuit/ordinance/vote/news/election/budget
                level           TEXT,   -- federal/state/county/school/local
                description     TEXT,
                summary         TEXT,   -- AI plain-English summary
                date            DATE,
                source_url      TEXT UNIQUE,
                source_name     TEXT,
                relevance_score REAL DEFAULT 0,
                categories      TEXT DEFAULT '[]',  -- JSON array of tags
                bill_number     TEXT,
                status          TEXT,   -- active/passed/failed/pending/signed
                raw_content     TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS politician_events (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                politician_id  INTEGER NOT NULL,
                event_id       INTEGER NOT NULL,
                role           TEXT,   -- sponsor/cosponsor/opponent/voted_yes/voted_no/mentioned/committee
                stance         TEXT,   -- support/oppose/neutral/unknown
                notes          TEXT,
                UNIQUE(politician_id, event_id, role),
                FOREIGN KEY (politician_id) REFERENCES politicians(id),
                FOREIGN KEY (event_id)      REFERENCES events(id)
            );

            CREATE TABLE IF NOT EXISTS scan_runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at   TIMESTAMP,
                events_found  INTEGER DEFAULT 0,
                events_new    INTEGER DEFAULT 0,
                status        TEXT DEFAULT 'running',
                error_log     TEXT
            );

            CREATE TABLE IF NOT EXISTS reports (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date      DATE UNIQUE,
                html_content     TEXT,
                markdown_content TEXT,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_events_date    ON events(date);
            CREATE INDEX IF NOT EXISTS idx_events_level   ON events(level);
            CREATE INDEX IF NOT EXISTS idx_events_type    ON events(type);
            CREATE INDEX IF NOT EXISTS idx_pe_politician  ON politician_events(politician_id);
            CREATE INDEX IF NOT EXISTS idx_pe_event       ON politician_events(event_id);

            -- Podcast episodes (one per day)
            CREATE TABLE IF NOT EXISTS podcasts (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                podcast_date     DATE UNIQUE,
                title            TEXT,
                script           TEXT,
                audio_path       TEXT,
                duration_seconds INTEGER,
                word_count       INTEGER,
                status           TEXT DEFAULT 'pending',   -- pending/scripting/tts/done/failed
                error_log        TEXT,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Chat conversations (Q&A sessions with the agent)
            CREATE TABLE IF NOT EXISTS conversations (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                title         TEXT,
                context_date  DATE,                  -- which digest date this relates to
                started_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Individual chat messages
            CREATE TABLE IF NOT EXISTS messages (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id  INTEGER NOT NULL,
                role             TEXT,    -- user / assistant
                content          TEXT,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            );

            -- Knowledge notes extracted from conversations
            CREATE TABLE IF NOT EXISTS knowledge_notes (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id  INTEGER,
                title            TEXT,
                content          TEXT,          -- the extracted knowledge (Markdown)
                topics           TEXT DEFAULT '[]',   -- JSON array
                politicians      TEXT DEFAULT '[]',   -- JSON array
                markdown_path    TEXT,          -- relative path to saved .md file
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            );

            CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_notes_conv ON knowledge_notes(conversation_id);

            -- Audience-authored notes per daily digest. One row per date;
            -- read by the PM/Editor agents to inform future content.
            CREATE TABLE IF NOT EXISTS daily_notes (
                report_date  TEXT PRIMARY KEY,     -- YYYY-MM-DD
                content      TEXT NOT NULL DEFAULT '',
                updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)


def seed_politicians(db_path: Path, politicians: List[Dict]) -> None:
    """Insert known politicians if they don't exist yet."""
    with get_connection(db_path) as conn:
        for p in politicians:
            conn.execute(
                """INSERT OR IGNORE INTO politicians (name, office, party, level, district)
                   VALUES (:name, :office, :party, :level, :district)""",
                p,
            )


# ── Events ────────────────────────────────────────────────────────────────────

def upsert_event(db_path: Path, event: Dict) -> Optional[int]:
    """
    Insert an event; skip if source_url already exists.
    Returns the row id (new or existing), or None if no url.
    """
    if not event.get("source_url"):
        return None

    with get_connection(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM events WHERE source_url = ?", (event["source_url"],)
        ).fetchone()
        if existing:
            return existing["id"]

        categories = event.get("categories", [])
        if isinstance(categories, list):
            categories = json.dumps(categories)

        cursor = conn.execute(
            """INSERT INTO events
               (title, type, level, description, summary, date, source_url,
                source_name, relevance_score, categories, bill_number, status, raw_content)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.get("title", "")[:500],
                event.get("type", "news"),
                event.get("level", ""),
                event.get("description", ""),
                event.get("summary", ""),
                event.get("date"),
                event["source_url"],
                event.get("source_name", ""),
                event.get("relevance_score", 0),
                categories,
                event.get("bill_number"),
                event.get("status", ""),
                event.get("raw_content", ""),
            ),
        )
        return cursor.lastrowid


def update_event_ai(db_path: Path, event_id: int, summary: str,
                    relevance_score: float, categories: List[str]) -> None:
    """Patch an event row with AI-generated fields."""
    with get_connection(db_path) as conn:
        conn.execute(
            """UPDATE events SET summary=?, relevance_score=?, categories=?
               WHERE id=?""",
            (summary, relevance_score, json.dumps(categories), event_id),
        )


def link_politician_event(db_path: Path, politician_name: str,
                           event_id: int, role: str, stance: str = "unknown",
                           notes: str = "") -> None:
    """Create or ignore a politician↔event link."""
    with get_connection(db_path) as conn:
        pol = conn.execute(
            "SELECT id FROM politicians WHERE name = ?", (politician_name,)
        ).fetchone()
        if not pol:
            # Auto-create unknown politician
            cur = conn.execute(
                "INSERT OR IGNORE INTO politicians (name, level) VALUES (?, 'unknown')",
                (politician_name,),
            )
            pol_id = cur.lastrowid or conn.execute(
                "SELECT id FROM politicians WHERE name=?", (politician_name,)
            ).fetchone()["id"]
        else:
            pol_id = pol["id"]

        conn.execute(
            """INSERT OR IGNORE INTO politician_events
               (politician_id, event_id, role, stance, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (pol_id, event_id, role, stance, notes),
        )


# ── Queries ───────────────────────────────────────────────────────────────────

def get_recent_events(db_path: Path, days: int = 7,
                      level: Optional[str] = None,
                      min_relevance: float = 0.0) -> List[Dict]:
    """Fetch recent events, optionally filtered by level and relevance."""
    sql = """
        SELECT e.*, GROUP_CONCAT(p.name, ', ') AS politicians
        FROM events e
        LEFT JOIN politician_events pe ON pe.event_id = e.id
        LEFT JOIN politicians p ON p.id = pe.politician_id
        WHERE e.date >= date('now', ?)
          AND e.relevance_score >= ?
    """
    params: List[Any] = [f"-{days} days", min_relevance]
    if level:
        sql += " AND e.level = ?"
        params.append(level)
    sql += " GROUP BY e.id ORDER BY e.date DESC, e.relevance_score DESC"

    with get_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_politician_summary(db_path: Path, politician_name: str) -> Dict:
    """Return a politician row plus all their recent events."""
    with get_connection(db_path) as conn:
        pol = conn.execute(
            "SELECT * FROM politicians WHERE name LIKE ?",
            (f"%{politician_name}%",),
        ).fetchone()
        if not pol:
            return {}
        pol_dict = dict(pol)
        events = conn.execute(
            """SELECT e.*, pe.role, pe.stance, pe.notes
               FROM events e
               JOIN politician_events pe ON pe.event_id = e.id
               WHERE pe.politician_id = ?
               ORDER BY e.date DESC LIMIT 20""",
            (pol_dict["id"],),
        ).fetchall()
        pol_dict["events"] = [dict(e) for e in events]
    return pol_dict


def start_scan_run(db_path: Path) -> int:
    with get_connection(db_path) as conn:
        cur = conn.execute("INSERT INTO scan_runs DEFAULT VALUES")
        return cur.lastrowid


def finish_scan_run(db_path: Path, run_id: int, events_found: int,
                    events_new: int, status: str = "ok", error: str = "") -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """UPDATE scan_runs
               SET finished_at=CURRENT_TIMESTAMP, events_found=?, events_new=?, status=?, error_log=?
               WHERE id=?""",
            (events_found, events_new, status, error, run_id),
        )


def save_report(db_path: Path, report_date: date,
                html: str, markdown: str) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO reports (report_date, html_content, markdown_content)
               VALUES (?, ?, ?)""",
            (str(report_date), html, markdown),
        )


# ── Podcasts ──────────────────────────────────────────────────────────────────

def save_podcast(db_path: Path, podcast_date: date, title: str,
                 script: str, audio_path: str,
                 duration_seconds: int = 0, word_count: int = 0,
                 status: str = "done", error_log: str = "") -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO podcasts
               (podcast_date, title, script, audio_path, duration_seconds,
                word_count, status, error_log)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(podcast_date), title, script, audio_path,
             duration_seconds, word_count, status, error_log),
        )


def get_podcast(db_path: Path, podcast_date: date) -> Optional[Dict]:
    with get_connection(db_path) as conn:
        r = conn.execute(
            "SELECT * FROM podcasts WHERE podcast_date = ?", (str(podcast_date),)
        ).fetchone()
        return dict(r) if r else None


def list_podcasts(db_path: Path, limit: int = 30) -> List[Dict]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM podcasts ORDER BY podcast_date DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Conversations & messages ──────────────────────────────────────────────────

def create_conversation(db_path: Path, title: str = "",
                         context_date: Optional[date] = None) -> int:
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO conversations (title, context_date) VALUES (?, ?)",
            (title or "New chat", str(context_date) if context_date else None),
        )
        return cur.lastrowid


def add_message(db_path: Path, conversation_id: int, role: str, content: str) -> int:
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)",
            (conversation_id, role, content),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (conversation_id,),
        )
        return cur.lastrowid


def get_conversation(db_path: Path, conversation_id: int) -> Dict:
    with get_connection(db_path) as conn:
        conv = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if not conv:
            return {}
        msgs = conn.execute(
            "SELECT role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
    conv_d = dict(conv)
    conv_d["messages"] = [dict(m) for m in msgs]
    return conv_d


def list_conversations(db_path: Path, limit: int = 50) -> List[Dict]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT c.*, (SELECT COUNT(*) FROM messages WHERE conversation_id = c.id) AS msg_count
               FROM conversations c ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_conversation_title(db_path: Path, conversation_id: int, title: str) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE conversations SET title = ? WHERE id = ?",
            (title, conversation_id),
        )


# ── Knowledge notes ───────────────────────────────────────────────────────────

def save_knowledge_note(db_path: Path, conversation_id: Optional[int],
                         title: str, content: str,
                         topics: List[str], politicians: List[str],
                         markdown_path: str = "") -> int:
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO knowledge_notes
               (conversation_id, title, content, topics, politicians, markdown_path)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (conversation_id, title, content,
             json.dumps(topics), json.dumps(politicians), markdown_path),
        )
        return cur.lastrowid


def list_knowledge_notes(db_path: Path, limit: int = 100) -> List[Dict]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM knowledge_notes ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def search_knowledge_notes(db_path: Path, query: str, limit: int = 20) -> List[Dict]:
    """Simple LIKE-based search across title + content + topics."""
    q = f"%{query}%"
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM knowledge_notes
               WHERE title LIKE ? OR content LIKE ? OR topics LIKE ?
               ORDER BY created_at DESC LIMIT ?""",
            (q, q, q, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def save_daily_note(db_path: Path, report_date: str, content: str) -> None:
    """UPSERT the audience's note for a given digest date."""
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO daily_notes (report_date, content, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(report_date) DO UPDATE SET
                 content    = excluded.content,
                 updated_at = CURRENT_TIMESTAMP""",
            (report_date, content),
        )


def get_daily_note(db_path: Path, report_date: str) -> Optional[Dict]:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM daily_notes WHERE report_date = ?", (report_date,)
        ).fetchone()
    return dict(row) if row else None


def list_daily_notes(db_path: Path, limit: int = 60) -> List[Dict]:
    """Most-recent first. Used by PM/Editor agents to review audience feedback."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM daily_notes ORDER BY report_date DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
