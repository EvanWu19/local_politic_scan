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

            -- PM-agent rollup of audience daily_notes into recurring themes,
            -- open questions, and underserved topics. Editor and Author read
            -- the most recent row to plan/revise upcoming episodes.
            CREATE TABLE IF NOT EXISTS weekly_themes (
                week_start         TEXT PRIMARY KEY,  -- ISO date of first day in window
                week_end           TEXT NOT NULL,
                themes             TEXT NOT NULL DEFAULT '[]',   -- JSON: [{title, why}]
                open_questions     TEXT NOT NULL DEFAULT '[]',   -- JSON: [string]
                underserved_topics TEXT NOT NULL DEFAULT '[]',   -- JSON: [string]
                summary            TEXT NOT NULL DEFAULT '',     -- one-paragraph human summary
                note_count         INTEGER NOT NULL DEFAULT 0,
                avoid_list         TEXT NOT NULL DEFAULT '[]',   -- JSON: [string] framings/taglines to avoid next episode
                generated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Data-Analyst output: per-politician consistency assessment.
            -- One row per (politician, generated_at). The latest row per
            -- politician is what reporter/podcast surface to the listener.
            CREATE TABLE IF NOT EXISTS consistency_scores (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                politician_id      INTEGER NOT NULL,
                politician_name    TEXT NOT NULL,
                window_start       TEXT NOT NULL,         -- ISO date
                window_end         TEXT NOT NULL,
                event_count        INTEGER NOT NULL DEFAULT 0,
                score              REAL,                  -- 0.0 (volatile) .. 1.0 (rock-solid)
                verdict            TEXT,                  -- one-word: consistent/mixed/inconsistent/insufficient
                summary            TEXT,                  -- one-paragraph plain-English assessment
                stable_positions   TEXT NOT NULL DEFAULT '[]',  -- JSON: [{topic, position, evidence_event_ids}]
                shifts             TEXT NOT NULL DEFAULT '[]',  -- JSON: [{topic, from, to, when, evidence_event_ids}]
                generated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (politician_id) REFERENCES politicians(id)
            );
            CREATE INDEX IF NOT EXISTS idx_cs_politician ON consistency_scores(politician_id, generated_at DESC);

            -- One row per historical-news backfill *attempt* per politician.
            -- Tracks coverage so we can avoid re-querying the same window.
            CREATE TABLE IF NOT EXISTS historical_news_runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                politician_id   INTEGER NOT NULL,
                politician_name TEXT NOT NULL,
                window_start    TEXT NOT NULL,    -- ISO date
                window_end      TEXT NOT NULL,
                items_found     INTEGER NOT NULL DEFAULT 0,
                items_new       INTEGER NOT NULL DEFAULT 0,
                status          TEXT NOT NULL DEFAULT 'ok',
                error_log       TEXT,
                ran_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (politician_id) REFERENCES politicians(id)
            );
            CREATE INDEX IF NOT EXISTS idx_hnr_politician ON historical_news_runs(politician_id, ran_at DESC);

            -- Multi-day overflow: events that didn't fit in today's podcast budget
            -- and are queued for a later episode. The podcast generator reads
            -- these on every run and force-includes any whose defer_until <= today
            -- (or whose defer_count has hit the cap). After the episode is built,
            -- consumed-deferred entries are cleared and any new overflow is queued
            -- for tomorrow.
            CREATE TABLE IF NOT EXISTS deferred_events (
                event_id          INTEGER PRIMARY KEY,
                defer_until       TEXT NOT NULL,         -- ISO date (YYYY-MM-DD)
                defer_count       INTEGER NOT NULL DEFAULT 0,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_deferred_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (event_id) REFERENCES events(id)
            );
            CREATE INDEX IF NOT EXISTS idx_deferred_until ON deferred_events(defer_until);
        """)

        # Column migrations for existing DBs (ALTER TABLE ADD COLUMN is a no-op
        # if the column already exists, but SQLite raises — swallow that).
        for sql in [
            "ALTER TABLE weekly_themes ADD COLUMN avoid_list TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE weekly_themes ADD COLUMN listener_candidate_interest TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE politicians ADD COLUMN ballot_year INTEGER",
            "ALTER TABLE politicians ADD COLUMN candidate_status TEXT",
            "ALTER TABLE politicians ADD COLUMN discovered_via TEXT",
        ]:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise


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


def save_weekly_themes(db_path: Path, week_start: str, week_end: str,
                        themes: List[Dict], open_questions: List[str],
                        underserved_topics: List[str], summary: str,
                        note_count: int,
                        avoid_list: Optional[List[str]] = None,
                        listener_candidate_interest: Optional[List[str]] = None) -> None:
    """UPSERT a PM-rollup record. Keyed by week_start (one row per window)."""
    import json as _json
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO weekly_themes
                 (week_start, week_end, themes, open_questions,
                  underserved_topics, summary, note_count, avoid_list,
                  listener_candidate_interest, generated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(week_start) DO UPDATE SET
                 week_end                    = excluded.week_end,
                 themes                      = excluded.themes,
                 open_questions              = excluded.open_questions,
                 underserved_topics          = excluded.underserved_topics,
                 summary                     = excluded.summary,
                 note_count                  = excluded.note_count,
                 avoid_list                  = excluded.avoid_list,
                 listener_candidate_interest = excluded.listener_candidate_interest,
                 generated_at                = CURRENT_TIMESTAMP""",
            (week_start, week_end,
             _json.dumps(themes, ensure_ascii=False),
             _json.dumps(open_questions, ensure_ascii=False),
             _json.dumps(underserved_topics, ensure_ascii=False),
             summary, note_count,
             _json.dumps(avoid_list or [], ensure_ascii=False),
             _json.dumps(listener_candidate_interest or [], ensure_ascii=False)),
        )


def _hydrate_weekly_themes_row(d: Dict) -> Dict:
    import json as _json
    for k in ("themes", "open_questions", "underserved_topics", "avoid_list",
              "listener_candidate_interest"):
        try:
            d[k] = _json.loads(d.get(k) or "[]")
        except Exception:
            d[k] = []
    return d


def get_latest_weekly_themes(db_path: Path) -> Optional[Dict]:
    """Return the most recently generated PM rollup, or None."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM weekly_themes ORDER BY week_end DESC, generated_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return _hydrate_weekly_themes_row(dict(row))


def list_weekly_themes(db_path: Path, limit: int = 12) -> List[Dict]:
    """Most-recent rollups first. Used by future trend/PM dashboards."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM weekly_themes ORDER BY week_end DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_hydrate_weekly_themes_row(dict(row)) for row in rows]


# ── Data-Analyst: consistency scores ──────────────────────────────────────────

def list_politicians(db_path: Path, level: Optional[str] = None,
                     min_events: int = 0) -> List[Dict]:
    """All politicians; optionally filter by level and require ≥N tracked events."""
    sql = (
        "SELECT p.*, COUNT(pe.id) AS event_count FROM politicians p "
        "LEFT JOIN politician_events pe ON pe.politician_id = p.id "
    )
    params: List = []
    if level:
        sql += " WHERE p.level = ? "
        params.append(level)
    sql += " GROUP BY p.id "
    if min_events > 0:
        sql += " HAVING event_count >= ? "
        params.append(min_events)
    sql += " ORDER BY p.level, p.name"
    with get_connection(db_path) as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def upsert_candidate(db_path: Path, name: str, office: str, party: str,
                     level: str, district: str, ballot_year: int,
                     candidate_status: str = "candidate",
                     discovered_via: str = "ai_discovery") -> int:
    """
    UPSERT a candidate into politicians. Matches on UNIQUE(name) — if the
    name already exists we fill in the missing ballot metadata rather
    than overwriting good data (e.g. a seeded incumbent who's now
    running again keeps their office but gains ballot_year).

    Returns the politician id.
    """
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, office, party, level, district, ballot_year "
            "FROM politicians WHERE name = ?", (name,),
        ).fetchone()
        if row:
            d = dict(row)
            conn.execute(
                """UPDATE politicians SET
                     office           = COALESCE(NULLIF(?, ''), office),
                     party            = COALESCE(NULLIF(?, ''), party),
                     level            = COALESCE(NULLIF(?, ''), level),
                     district         = COALESCE(NULLIF(?, ''), district),
                     ballot_year      = COALESCE(?, ballot_year),
                     candidate_status = COALESCE(NULLIF(?, ''), candidate_status),
                     discovered_via   = COALESCE(NULLIF(?, ''), discovered_via),
                     last_updated     = CURRENT_DATE
                   WHERE id = ?""",
                (office, party, level, district, ballot_year,
                 candidate_status, discovered_via, d["id"]),
            )
            return d["id"]
        cur = conn.execute(
            """INSERT INTO politicians
                 (name, office, party, level, district, ballot_year,
                  candidate_status, discovered_via, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_DATE)""",
            (name, office, party, level, district, ballot_year,
             candidate_status, discovered_via),
        )
        return cur.lastrowid


def list_ballot_candidates(db_path: Path, ballot_year: Optional[int] = None,
                           level: Optional[str] = None) -> List[Dict]:
    """
    Return politicians on the listener's ballot (ballot_year IS NOT NULL).
    Sorted by office then party then name so rendered output is stable.
    """
    sql = ("SELECT * FROM politicians "
           "WHERE ballot_year IS NOT NULL ")
    params: List = []
    if ballot_year is not None:
        sql += " AND ballot_year = ?"
        params.append(ballot_year)
    if level:
        sql += " AND level = ?"
        params.append(level)
    sql += " ORDER BY level, office, party, name"
    with get_connection(db_path) as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def save_consistency_score(db_path: Path, politician_id: int, politician_name: str,
                            window_start: str, window_end: str,
                            event_count: int, score: Optional[float], verdict: str,
                            summary: str, stable_positions: List[Dict],
                            shifts: List[Dict]) -> int:
    """Insert a consistency-score row. Returns the new row id."""
    import json as _json
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO consistency_scores
                 (politician_id, politician_name, window_start, window_end,
                  event_count, score, verdict, summary,
                  stable_positions, shifts, generated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (politician_id, politician_name, window_start, window_end,
             event_count, score, verdict, summary,
             _json.dumps(stable_positions, ensure_ascii=False),
             _json.dumps(shifts, ensure_ascii=False)),
        )
        return cur.lastrowid


def get_latest_consistency_score(db_path: Path, politician_id: int) -> Optional[Dict]:
    """Most recent score for a single politician, or None."""
    import json as _json
    with get_connection(db_path) as conn:
        row = conn.execute(
            """SELECT * FROM consistency_scores
               WHERE politician_id = ?
               ORDER BY generated_at DESC, id DESC LIMIT 1""",
            (politician_id,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("stable_positions", "shifts"):
        try:
            d[k] = _json.loads(d.get(k) or "[]")
        except Exception:
            d[k] = []
    return d


def list_latest_consistency_scores(db_path: Path) -> List[Dict]:
    """Latest score per politician (one row per politician)."""
    import json as _json
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT cs.* FROM consistency_scores cs
               JOIN (
                 SELECT politician_id, MAX(generated_at) AS max_at
                 FROM consistency_scores GROUP BY politician_id
               ) latest
                 ON latest.politician_id = cs.politician_id
                AND latest.max_at        = cs.generated_at
               ORDER BY cs.politician_name"""
        ).fetchall()
    out: List[Dict] = []
    for row in rows:
        d = dict(row)
        for k in ("stable_positions", "shifts"):
            try:
                d[k] = _json.loads(d.get(k) or "[]")
            except Exception:
                d[k] = []
        out.append(d)
    return out


# ── Historical news backfill: run log ─────────────────────────────────────────

def save_historical_news_run(db_path: Path, politician_id: int,
                              politician_name: str, window_start: str,
                              window_end: str, items_found: int,
                              items_new: int, status: str = "ok",
                              error: str = "") -> int:
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO historical_news_runs
                 (politician_id, politician_name, window_start, window_end,
                  items_found, items_new, status, error_log, ran_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (politician_id, politician_name, window_start, window_end,
             items_found, items_new, status, error),
        )
        return cur.lastrowid


def get_last_historical_news_run(db_path: Path,
                                   politician_id: int) -> Optional[Dict]:
    """Most-recent run; id DESC breaks ties when ran_at lands in the same second."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            """SELECT * FROM historical_news_runs
               WHERE politician_id = ?
               ORDER BY ran_at DESC, id DESC LIMIT 1""",
            (politician_id,),
        ).fetchone()
    return dict(row) if row else None


# ── Deferred events (multi-day overflow queue) ────────────────────────────────

def list_deferred_events(db_path: Path) -> Dict[int, Dict]:
    """
    Return {event_id: {defer_until, defer_count, created_at, last_deferred_at}}
    for every queued event. Used by the podcast generator to know which events
    must be force-included today and which fresh ones must be held back.
    """
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM deferred_events").fetchall()
    return {r["event_id"]: dict(r) for r in rows}


def defer_events(db_path: Path, event_ids: List[int], defer_until: str) -> None:
    """
    Queue events for a later podcast date. New rows start at defer_count=1;
    re-deferring an existing row increments defer_count and pushes defer_until.
    """
    if not event_ids:
        return
    with get_connection(db_path) as conn:
        for eid in event_ids:
            conn.execute(
                """INSERT INTO deferred_events
                     (event_id, defer_until, defer_count,
                      created_at, last_deferred_at)
                   VALUES (?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                   ON CONFLICT(event_id) DO UPDATE SET
                     defer_until      = excluded.defer_until,
                     defer_count      = defer_count + 1,
                     last_deferred_at = CURRENT_TIMESTAMP""",
                (eid, defer_until),
            )


def clear_deferred_events(db_path: Path, event_ids: List[int]) -> None:
    """Remove rows whose events were consumed by today's podcast."""
    if not event_ids:
        return
    with get_connection(db_path) as conn:
        conn.executemany(
            "DELETE FROM deferred_events WHERE event_id = ?",
            [(eid,) for eid in event_ids],
        )
