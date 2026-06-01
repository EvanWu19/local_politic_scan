"""
scanner.notifications — single place every scanner role surfaces issues
when a Cowork brief fails or work is stuck.

The listener (2026-05-15) chose "Surface errors to me" over silent fallback,
so failures must be visible. This module writes three things:

  1. `notifications.log` (project root) — one line per event, easy to tail.
  2. `cowork_inbox/notify_<date>.md` — a daily rollup. If the file already
     exists for today, it is appended to; the file is consumed by the
     Cowork drain task, which surfaces the contents via `dispatch_to_user`.
  3. The `scanner_notifications` SQLite table — so the web UI and the
     weekly review can render notifications inline next to the digest.

Every scanner role should use `notify(...)` instead of swallowing exceptions
silently. Failures here are deliberately non-fatal — a notifications outage
must never break the daily pipeline.

Usage
-----
    from scanner.notifications import notify
    notify("analyst", "consistency brief failed: ANTHROPIC_API_KEY missing",
           severity="error", context={"politician_id": 42})
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
NOTIFICATIONS_LOG = _PROJECT_ROOT / "notifications.log"
COWORK_INBOX = _PROJECT_ROOT / "cowork_inbox"

SEVERITIES = ("info", "warn", "error")


def _db_path() -> Optional[Path]:
    try:
        from config import Config as _Cfg
        return Path(_Cfg.DB_PATH)
    except Exception:
        return None


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scanner_notifications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            role       TEXT NOT NULL,
            severity   TEXT NOT NULL,
            message    TEXT NOT NULL,
            context    TEXT,
            seen       INTEGER DEFAULT 0
        )
    """)


def _write_log_line(role: str, severity: str, message: str) -> None:
    try:
        line = f"{datetime.now().isoformat(timespec='seconds')}\t{severity.upper()}\t{role}\t{message}\n"
        NOTIFICATIONS_LOG.touch(exist_ok=True)
        with NOTIFICATIONS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as e:
        log.debug("notifications: file log failed — %s", e)


def _write_db_row(role: str, severity: str, message: str,
                   context: Optional[Dict[str, Any]]) -> None:
    p = _db_path()
    if not p or not p.exists():
        return
    try:
        with sqlite3.connect(str(p)) as conn:
            _ensure_table(conn)
            conn.execute(
                "INSERT INTO scanner_notifications "
                "(role, severity, message, context) VALUES (?, ?, ?, ?)",
                (role, severity, message,
                 json.dumps(context or {}, ensure_ascii=False)),
            )
    except Exception as e:
        log.debug("notifications: DB write failed — %s", e)


def _append_daily_rollup(role: str, severity: str, message: str) -> None:
    try:
        COWORK_INBOX.mkdir(parents=True, exist_ok=True)
        path = COWORK_INBOX / f"notify_{date.today().isoformat()}.md"
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"- **[{severity.upper()}]** `{role}` · {ts} — {message}\n"
        if not path.exists():
            path.write_text(
                f"# Scanner notifications — {date.today().isoformat()}\n\n"
                "These items need your attention. The drain-cowork-inbox "
                "task surfaces this list via dispatch_to_user.\n\n" + line,
                encoding="utf-8",
            )
        else:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception as e:
        log.debug("notifications: rollup write failed — %s", e)


def notify(role: str, message: str, *,
           severity: str = "warn",
           context: Optional[Dict[str, Any]] = None) -> None:
    """Surface a notification to the listener.

    Logs to file, persists to SQLite, and appends to today's Cowork-drained
    rollup so the listener sees the issue both in the web UI and via the
    next drain dispatch. Never raises — notification failures must not
    cascade into pipeline failures.
    """
    sev = severity if severity in SEVERITIES else "warn"
    log.warning("notify[%s] %s: %s", sev, role, message)
    _write_log_line(role, sev, message)
    _write_db_row(role, sev, message, context)
    _append_daily_rollup(role, sev, message)


def list_unseen(limit: int = 50) -> list:
    """Return the most recent unseen notifications for the web UI."""
    p = _db_path()
    if not p or not p.exists():
        return []
    try:
        with sqlite3.connect(str(p)) as conn:
            _ensure_table(conn)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM scanner_notifications WHERE seen = 0 "
                "ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        log.debug("notifications: list_unseen failed — %s", e)
        return []


def mark_seen(notification_id: int) -> None:
    p = _db_path()
    if not p or not p.exists():
        return
    try:
        with sqlite3.connect(str(p)) as conn:
            _ensure_table(conn)
            conn.execute(
                "UPDATE scanner_notifications SET seen = 1 WHERE id = ?",
                (notification_id,),
            )
    except Exception as e:
        log.debug("notifications: mark_seen failed — %s", e)


def scan_failed_briefs(within_hours: int = 24) -> int:
    """Find Cowork briefs that errored in the last `within_hours`. Returns
    the count surfaced. Call this from a Sunday/nightly job so stuck briefs
    don't sit forever.
    """
    if not COWORK_INBOX.exists():
        return 0
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(hours=within_hours)
    surfaced = 0
    for p in COWORK_INBOX.glob("*.error.json"):
        try:
            if datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
                continue
            data = json.loads(p.read_text(encoding="utf-8"))
            brief_type = data.get("type", "unknown")
            brief_id = data.get("brief_id", p.stem)
            ctx = data.get("context", {})
            who = ctx.get("candidate_name") or ctx.get("politician_name") or ""
            msg = f"Cowork brief '{brief_id}' (type={brief_type}) errored"
            if who:
                msg += f" for {who}"
            notify("cowork_drain", msg, severity="error",
                   context={"brief_id": brief_id, "type": brief_type})
            surfaced += 1
        except Exception as e:
            log.debug("notifications: failed reading %s — %s", p, e)
    return surfaced
