"""SQLite persistence for bridge-owned workflow state.

One row per in-progress workflow. Terminal rows (completed/failed/expired/
cancelled) are retained for /status history but pruned by max-age sweep.

Schema is intentionally workflow-agnostic: `payload_json` carries skill-
specific state (e.g. draft JSON for PlanWorkflow). Storage owns only the
state machine envelope + TTL.

Concurrency: SQLite's default BEGIN IMMEDIATE on writes gives us scope-
level mutual exclusion without per-process locks. All writes run under a
single connection per process (bridge is single-process).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from feishu_bridge.paths import bridge_home
from feishu_bridge.workflows.runtime import (
    STATE_DRAFT,
    STATE_EXPIRED,
    STATE_WAITING_CONFIRMATION,
    TERMINAL_STATES,
)

log = logging.getLogger("feishu-bridge")

# Retention policy: terminal rows older than this are pruned on each expire
# sweep. Tuned so /status still shows recent workflow history.
TERMINAL_RETENTION_SECONDS = 30 * 86400  # 30 days

# Sentinel for "argument not supplied" — distinguishes None (explicitly clear)
# from omission (leave existing value).
_UNSET: object = object()


@dataclass
class WorkflowRecord:
    """One row from the `workflows` table."""

    id: str
    scope_key: str
    skill_name: str
    state: str
    payload: dict = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0
    last_error: str | None = None

    @property
    def is_active(self) -> bool:
        return self.state not in TERMINAL_STATES

    @property
    def is_waiting(self) -> bool:
        return self.state == STATE_WAITING_CONFIRMATION


class WorkflowStorage:
    """SQLite-backed workflow persistence."""

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS workflows (
            id TEXT PRIMARY KEY,
            scope_key TEXT NOT NULL,
            skill_name TEXT NOT NULL,
            state TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_workflows_scope
            ON workflows(scope_key, state);
        CREATE INDEX IF NOT EXISTS idx_workflows_expires
            ON workflows(expires_at);
    """

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            db_path = bridge_home() / "workflows.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            isolation_level=None,  # autocommit; we manage BEGIN/COMMIT ourselves
            check_same_thread=False,
            timeout=10.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(self._SCHEMA)

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # ---- writers ------------------------------------------------------

    def create(
        self, *, scope_key: str, skill_name: str, payload: dict,
        ttl_seconds: int, state: str = STATE_DRAFT, now: float | None = None,
    ) -> WorkflowRecord:
        now = now if now is not None else time.time()
        wf_id = str(uuid.uuid4())
        rec = WorkflowRecord(
            id=wf_id, scope_key=scope_key, skill_name=skill_name,
            state=state, payload=dict(payload),
            created_at=now, updated_at=now,
            expires_at=now + max(0, int(ttl_seconds)),
        )
        self._conn.execute(
            "INSERT INTO workflows("
            " id, scope_key, skill_name, state, payload_json,"
            " created_at, updated_at, expires_at, last_error"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rec.id, rec.scope_key, rec.skill_name, rec.state,
                json.dumps(rec.payload, ensure_ascii=False),
                rec.created_at, rec.updated_at, rec.expires_at,
                rec.last_error,
            ),
        )
        return rec

    def update(
        self, wf_id: str, *, state: str | None = None,
        payload: dict | None = None, expires_at: float | None = None,
        last_error: object = _UNSET,
        now: float | None = None,
    ) -> WorkflowRecord | None:
        now = now if now is not None else time.time()
        existing = self.get(wf_id)
        if existing is None:
            return None
        new_state = state if state is not None else existing.state
        new_payload = payload if payload is not None else existing.payload
        new_expires = expires_at if expires_at is not None else existing.expires_at
        if last_error is _UNSET:
            new_error: str | None = existing.last_error
        else:
            new_error = last_error  # type: ignore[assignment]
        self._conn.execute(
            "UPDATE workflows SET state=?, payload_json=?, updated_at=?,"
            " expires_at=?, last_error=? WHERE id=?",
            (
                new_state,
                json.dumps(new_payload, ensure_ascii=False),
                now, new_expires, new_error, wf_id,
            ),
        )
        existing.state = new_state
        existing.payload = new_payload
        existing.updated_at = now
        existing.expires_at = new_expires
        existing.last_error = new_error
        return existing

    def mark_expired_waiting(self, now: float | None = None) -> list[str]:
        """Mark all waiting_confirmation rows past TTL as expired. Returns ids."""
        now = now if now is not None else time.time()
        cur = self._conn.execute(
            "SELECT id FROM workflows WHERE state=? AND expires_at <= ?",
            (STATE_WAITING_CONFIRMATION, now),
        )
        ids = [row["id"] for row in cur.fetchall()]
        if not ids:
            return []
        self._conn.executemany(
            "UPDATE workflows SET state=?, updated_at=? WHERE id=?",
            [(STATE_EXPIRED, now, wf_id) for wf_id in ids],
        )
        return ids

    def prune_terminal(self, now: float | None = None) -> int:
        """Delete terminal rows older than TERMINAL_RETENTION_SECONDS."""
        now = now if now is not None else time.time()
        cutoff = now - TERMINAL_RETENTION_SECONDS
        terminal_list = sorted(TERMINAL_STATES)
        placeholders = ",".join("?" * len(terminal_list))
        cur = self._conn.execute(
            f"DELETE FROM workflows WHERE state IN ({placeholders})"
            f" AND updated_at < ?",
            (*terminal_list, cutoff),
        )
        return cur.rowcount or 0

    # ---- readers ------------------------------------------------------

    def get(self, wf_id: str) -> WorkflowRecord | None:
        row = self._conn.execute(
            "SELECT * FROM workflows WHERE id=?", (wf_id,),
        ).fetchone()
        return self._row_to_record(row)

    def active_for_scope(self, scope_key: str) -> WorkflowRecord | None:
        """Return the single active workflow for a scope, or None.

        MVP invariant: at most one active workflow per scope. If multiple are
        somehow present, the most recent one wins; older ones are left for
        /status to surface.
        """
        terminal_list = sorted(TERMINAL_STATES)
        placeholders = ",".join("?" * len(terminal_list))
        row = self._conn.execute(
            f"SELECT * FROM workflows WHERE scope_key=?"
            f" AND state NOT IN ({placeholders})"
            f" ORDER BY created_at DESC LIMIT 1",
            (scope_key, *terminal_list),
        ).fetchone()
        return self._row_to_record(row)

    def list_for_scope(
        self, scope_key: str, limit: int = 20,
    ) -> list[WorkflowRecord]:
        cur = self._conn.execute(
            "SELECT * FROM workflows WHERE scope_key=?"
            " ORDER BY created_at DESC LIMIT ?",
            (scope_key, int(limit)),
        )
        return [self._row_to_record(row) for row in cur.fetchall() if row]

    # ---- internals ----------------------------------------------------

    def _row_to_record(self, row: sqlite3.Row | None) -> WorkflowRecord | None:
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        return WorkflowRecord(
            id=row["id"],
            scope_key=row["scope_key"],
            skill_name=row["skill_name"],
            state=row["state"],
            payload=payload if isinstance(payload, dict) else {},
            created_at=float(row["created_at"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0),
            expires_at=float(row["expires_at"] or 0.0),
            last_error=row["last_error"],
        )


__all__ = [
    "WorkflowRecord",
    "WorkflowStorage",
    "TERMINAL_RETENTION_SECONDS",
]
