"""Per-turn usage ledger backed by SQLite.

Records one row per Claude turn so cross-session views (total spend last
7d, compact frequency, per-chat breakdown) can be computed later without
keeping everything in memory.  Keep this module dependency-free beyond
stdlib — it runs in the worker hot path and must not block on network
or heavy imports.

Schema is idempotent on startup: callers get a ready-to-use Ledger by
calling ``Ledger.open(path)``.  Writes are best-effort; a DB error
never fails the outer turn (it just logs a warning).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("feishu-bridge")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    session_id      TEXT    NOT NULL,
    bot_id          TEXT    NOT NULL,
    chat_id         TEXT    NOT NULL,
    thread_id       TEXT,
    model           TEXT,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_read      INTEGER NOT NULL DEFAULT 0,
    cache_creation  INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL    NOT NULL DEFAULT 0,
    compact_event   INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_turns_session_ts ON turns(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_turns_ts          ON turns(ts);
"""


class Ledger:
    """Thread-safe SQLite ledger writer.

    One connection per Ledger, guarded by a lock.  Suitable for the
    bridge's single-process worker pool; for multi-process use pass the
    same Path — SQLite handles file-level locking.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.Lock()

    @classmethod
    def open(cls, path: Path) -> "Ledger":
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = sqlite3.connect(str(path), check_same_thread=False, timeout=1.0)
        conn.executescript(_SCHEMA)
        conn.commit()
        return cls(conn)

    def record_turn(
        self,
        *,
        session_id: str,
        bot_id: str,
        chat_id: str,
        thread_id: Optional[str],
        model: Optional[str],
        usage: dict,
        cost_usd: float,
        compact_event: bool,
        duration_ms: int,
    ) -> None:
        """Insert one turn row.  Never raises — DB errors are logged."""
        if not session_id:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO turns (ts, session_id, bot_id, chat_id, thread_id,"
                    " model, input_tokens, output_tokens, cache_read, cache_creation,"
                    " cost_usd, compact_event, duration_ms)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        time.time(),
                        session_id,
                        bot_id,
                        chat_id,
                        thread_id,
                        model,
                        int(usage.get("input_tokens", 0) or 0),
                        int(usage.get("output_tokens", 0) or 0),
                        int(usage.get("cache_read_input_tokens", 0) or 0),
                        int(usage.get("cache_creation_input_tokens", 0) or 0),
                        float(cost_usd or 0),
                        1 if compact_event else 0,
                        int(duration_ms or 0),
                    ),
                )
                self._conn.commit()
        except Exception as e:
            # Never fail the outer turn — log and move on.  Widened from
            # sqlite3.Error because upstream usage dicts / costs may have
            # unexpected shapes (type/attr errors) that must stay contained.
            log.warning("ledger.record_turn failed: %s", e)

    def compact_count(self, session_id: str) -> int:
        """Return how many compact events this session has recorded."""
        if not session_id:
            return 0
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM turns WHERE session_id=? AND compact_event=1",
                    (session_id,),
                ).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error as e:
            log.warning("ledger.compact_count failed: %s", e)
            return 0

    def prev_ctx_tokens(self, session_id: str) -> int:
        """Return ``input + cache_read`` from the turn BEFORE the most
        recent one for this session, or 0 if none.  /status shows
        delta against the current (latest) turn, so the reference point
        must be the turn prior to it."""
        if not session_id:
            return 0
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT input_tokens + cache_read FROM turns"
                    " WHERE session_id=? ORDER BY id DESC LIMIT 1 OFFSET 1",
                    (session_id,),
                ).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error as e:
            log.warning("ledger.prev_ctx_tokens failed: %s", e)
            return 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
