"""Background-task SQLite layer.

Provides:
    - Schema bootstrap (DDL + PRAGMAs, WAL mode, foreign keys)
    - TaskState enum + validated state transitions
    - BgTaskRepo — DAO for bg_tasks / bg_runs
    - integrity_check_and_maybe_quarantine() + rebuild_from_manifests()

See .specs/changes/feishu-bridge-bg-tasks/design.md for design and tasks.md for task-by-task
breakdown.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger("feishu-bridge.bg")

# Task IDs are 32-char lowercase hex (uuid4().hex). Directory names coming from
# the filesystem are untrusted — enforce the exact shape before using them as
# primary keys or writing them back into paths.
_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Upper bound on manifest schema versions we know how to replay. Bump when
# design.md schema_version changes and `_replay_completed_manifest` is updated
# to handle the new shape.
_MAX_MANIFEST_SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

PRAGMAS = [
    "PRAGMA journal_mode = WAL",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
]

DDL = [
    """
    CREATE TABLE IF NOT EXISTS bg_tasks (
        id                    TEXT PRIMARY KEY,
        chat_id               TEXT NOT NULL,
        session_id            TEXT NOT NULL,
        requester_open_id     TEXT,
        kind                  TEXT NOT NULL DEFAULT 'adhoc',
        command_argv          TEXT NOT NULL,
        cwd                   TEXT,
        env_overlay           TEXT,
        timeout_seconds       INTEGER NOT NULL DEFAULT 1800,
        on_done_prompt        TEXT NOT NULL,
        output_paths          TEXT,
        state                 TEXT NOT NULL DEFAULT 'queued',
        reason                TEXT,
        signal                TEXT,
        error_message         TEXT,
        cancel_requested_at   INTEGER,
        claimed_by            TEXT,
        claimed_at            INTEGER,
        created_at            INTEGER NOT NULL,
        updated_at            INTEGER NOT NULL,
        CHECK (state IN ('queued','launching','running','completed','failed','cancelled','timeout','orphan')),
        CHECK (kind = 'adhoc')
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bg_tasks_state       ON bg_tasks(state)",
    "CREATE INDEX IF NOT EXISTS idx_bg_tasks_chat        ON bg_tasks(chat_id, state)",
    "CREATE INDEX IF NOT EXISTS idx_bg_tasks_updated     ON bg_tasks(updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_bg_tasks_launching   ON bg_tasks(state, claimed_at) WHERE state = 'launching'",
    """
    CREATE TABLE IF NOT EXISTS bg_runs (
        id                        INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id                   TEXT NOT NULL REFERENCES bg_tasks(id) ON DELETE CASCADE,
        runner_token              TEXT NOT NULL,
        pid                       INTEGER,
        pgid                      INTEGER,
        process_start_time_us     INTEGER,
        wrapper_pid               INTEGER NOT NULL,
        wrapper_start_time_us     INTEGER NOT NULL,
        started_at                INTEGER NOT NULL,
        finished_at               INTEGER,
        exit_code                 INTEGER,
        signal                    TEXT,
        manifest_path             TEXT,
        stdout_tail               BLOB,
        stderr_tail               BLOB,
        delivery_state            TEXT NOT NULL DEFAULT 'not_ready',
        delivery_error            TEXT,
        delivery_attempt_count    INTEGER NOT NULL DEFAULT 0,
        completion_detected_at    INTEGER,
        enqueued_at               INTEGER,
        sent_at                   INTEGER,
        session_resume_status     TEXT,
        CHECK (delivery_state IN ('not_ready','pending','enqueued','sent','delivery_failed'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bg_runs_task      ON bg_runs(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_bg_runs_delivery  ON bg_runs(delivery_state)",
    """
    CREATE TABLE IF NOT EXISTS bg_schema (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
]


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class TaskState(str, Enum):
    QUEUED     = "queued"
    LAUNCHING  = "launching"
    RUNNING    = "running"
    COMPLETED  = "completed"
    FAILED     = "failed"
    CANCELLED  = "cancelled"
    TIMEOUT    = "timeout"
    ORPHAN     = "orphan"

    @classmethod
    def terminal(cls) -> frozenset["TaskState"]:
        return _TERMINAL_STATES

    @classmethod
    def is_terminal(cls, s: "str | TaskState") -> bool:
        return TaskState(s) in _TERMINAL_STATES

    @classmethod
    def validate_transition(cls, old: "str | TaskState", new: "str | TaskState") -> bool:
        """Return True iff old→new is a legal transition.

        Also accepts identity transitions (old == new) — those are no-ops and the
        repo layer elsewhere rejects them, but this helper is purely about legality
        of the directed edge between distinct states.
        """
        o = TaskState(old)
        n = TaskState(new)
        return n in _ALLOWED.get(o, frozenset())


_TERMINAL_STATES: frozenset[TaskState] = frozenset({
    TaskState.COMPLETED,
    TaskState.FAILED,
    TaskState.CANCELLED,
    TaskState.TIMEOUT,
    TaskState.ORPHAN,
})

# Explicit adjacency table. Tight by design: any new transition needs a conscious edit.
_ALLOWED: dict[TaskState, frozenset[TaskState]] = {
    TaskState.QUEUED: frozenset({
        TaskState.LAUNCHING,
        TaskState.CANCELLED,  # cancel before launch
    }),
    TaskState.LAUNCHING: frozenset({
        TaskState.RUNNING,
        TaskState.FAILED,     # launch_interrupted reap
    }),
    TaskState.RUNNING: frozenset({
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.TIMEOUT,
        TaskState.ORPHAN,
    }),
    # Terminal states have no outgoing edges.
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.TIMEOUT: frozenset(),
    TaskState.ORPHAN: frozenset(),
}


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with project-wide PRAGMAs applied.

    Connections are **not** thread-safe. Each thread that needs DB access must
    call ``connect()`` (or ``init_db()``) to obtain its own connection.
    SQLite's C API is serialised, but a shared Python connection plus concurrent
    ``BEGIN``/``COMMIT`` produces ``sqlite3.ProgrammingError`` or interleaved
    transactions. WAL mode makes per-thread connections cheap.
    """
    db_path = str(db_path)
    conn = sqlite3.connect(
        db_path,
        timeout=10.0,              # separate from PRAGMA busy_timeout; belt & braces
        isolation_level=None,      # explicit transactions — we use BEGIN ... COMMIT
    )
    conn.row_factory = sqlite3.Row
    for pragma in PRAGMAS:
        conn.execute(pragma)
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Create tables/indexes idempotently and return a live connection.

    Creates the parent directory with mode 0700 on first call (contains secrets
    indirectly via command_argv / env_overlay).

    DDL is skipped when an on-disk schema row already matches `SCHEMA_VERSION`.
    That keeps concurrent `init_db` callers (e.g. worker threads) from serialising
    on exclusive write locks they never actually need.
    """
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, 0o700)
    except PermissionError:
        pass

    conn = connect(p)
    if not _schema_up_to_date(conn):
        with _tx(conn):
            for stmt in DDL:
                conn.execute(stmt)
            migrate(conn)
            conn.execute(
                "INSERT OR IGNORE INTO bg_schema(key,value) VALUES('version', ?)",
                (str(SCHEMA_VERSION),),
            )
    _verify_pragmas(conn)
    return conn


def _schema_up_to_date(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT value FROM bg_schema WHERE key='version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row) and row[0] == str(SCHEMA_VERSION)


def migrate(conn: sqlite3.Connection) -> None:
    """Upgrade schema from older versions to SCHEMA_VERSION.

    v1 is the initial version; there is nothing to migrate from. When a future
    version ships, add an ALTER sequence keyed on the current `bg_schema.version`
    row and bump the `SCHEMA_VERSION` constant.
    """
    current = conn.execute(
        "SELECT value FROM bg_schema WHERE key='version'"
    ).fetchone()
    if current and current[0] != str(SCHEMA_VERSION):
        raise RuntimeError(
            f"bg_tasks_db v{current[0]} has no registered migration to "
            f"v{SCHEMA_VERSION}; add it to migrate() before deploying."
        )


def _verify_pragmas(conn: sqlite3.Connection) -> None:
    """Sanity-check PRAGMAs took effect. In-memory DBs silently refuse WAL."""
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    sync = conn.execute("PRAGMA synchronous").fetchone()[0]
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    # WAL is unavailable on :memory: — fall back silently. File-backed expected WAL.
    # Other PRAGMAs must stick on any backend.
    if timeout < 5000:
        raise RuntimeError(f"busy_timeout not applied (got {timeout})")
    if sync != 1:  # NORMAL == 1
        raise RuntimeError(f"synchronous != NORMAL (got {sync})")
    if fk != 1:
        raise RuntimeError("foreign_keys = ON failed")
    log.debug(
        "bg_tasks_db ready: journal=%s busy_timeout=%dms synchronous=%d foreign_keys=%d",
        mode, timeout, sync, fk,
    )


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit BEGIN/COMMIT; rolls back on exception."""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# Integrity check / quarantine / manifest replay
# ---------------------------------------------------------------------------

def integrity_check_and_maybe_quarantine(db_path: str | Path) -> Path:
    """Verify `PRAGMA integrity_check` on db_path.

    If the file is missing, return the path unchanged (caller will init_db).
    If integrity_check passes, return the path unchanged.
    If it fails, rename to `<db_path>.quarantine.<ts>` and return the path so the
    caller can create a fresh DB and replay from manifests.
    """
    p = Path(db_path)
    if not p.exists():
        return p
    try:
        conn = sqlite3.connect(str(p), timeout=5.0)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
        if row and row[0] == "ok":
            return p
        log.error("integrity_check failed: %s", row[0] if row else "<null>")
    except sqlite3.DatabaseError as exc:
        log.error("integrity_check raised DatabaseError: %s", exc)

    ts = int(time.time())
    quarantined = p.with_name(f"{p.name}.quarantine.{ts}")
    p.rename(quarantined)
    # move sidecars too, if any
    for suffix in ("-shm", "-wal"):
        side = p.with_name(p.name + suffix)
        if side.exists():
            side.rename(p.with_name(f"{p.name}.quarantine.{ts}{suffix}"))
    log.warning("bg_tasks DB quarantined → %s", quarantined)
    return p  # caller will init_db() on the (now-absent) original path


def rebuild_from_manifests(conn: sqlite3.Connection, tasks_dir: str | Path) -> dict[str, int]:
    """Replay `tasks/completed/*/task.json.done` into a fresh DB.

    `tasks/active/<id>/` directories without a committed manifest become `orphan`
    rows. Pre-launch queued tasks that never produced a manifest are lost (logged).

    Returns {'completed_replayed': N, 'orphans_created': N}.
    """
    stats = {"completed_replayed": 0, "orphans_created": 0}
    root = Path(tasks_dir)

    completed_dir = root / "completed"
    if completed_dir.is_dir():
        for task_dir in sorted(completed_dir.iterdir()):
            if not _is_trusted_task_dir(task_dir):
                continue
            manifest = task_dir / "task.json.done"
            if not manifest.is_file() or manifest.is_symlink():
                continue
            try:
                data = json.loads(manifest.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("skip corrupt manifest %s: %s", manifest, exc)
                continue
            if _replay_completed_manifest(conn, data, str(manifest), task_dir.name):
                stats["completed_replayed"] += 1

    active_dir = root / "active"
    if active_dir.is_dir():
        for task_dir in sorted(active_dir.iterdir()):
            if not _is_trusted_task_dir(task_dir):
                continue
            tid = task_dir.name
            if _row_exists(conn, tid):
                continue

            # B3: wrapper may have crashed between Phase C2 (rename .partial →
            # .done) and Phase C3 (mv active/ → completed/). The committed
            # manifest is the source of truth — replay it and physically move
            # the directory so the next reconcile doesn't see it twice.
            manifest = task_dir / "task.json.done"
            if manifest.is_file() and not manifest.is_symlink():
                try:
                    data = json.loads(manifest.read_text())
                except (json.JSONDecodeError, OSError) as exc:
                    log.warning("active/%s has unreadable manifest: %s", tid, exc)
                else:
                    if _replay_completed_manifest(conn, data, str(manifest), tid):
                        stats["completed_replayed"] += 1
                        try:
                            dest = root / "completed" / tid
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            task_dir.rename(dest)
                            log.info(
                                "bg reconcile: promoted active/%s → completed/", tid,
                            )
                        except OSError as exc:
                            log.warning(
                                "active/%s manifest replayed but mv failed: %s",
                                tid, exc,
                            )
                    continue

            # Neither DB row nor manifest — wrapper and child died together.
            now = _now_ms()
            with _tx(conn):
                conn.execute(
                    """INSERT OR IGNORE INTO bg_tasks
                       (id, chat_id, session_id, kind, command_argv, on_done_prompt,
                        state, reason, created_at, updated_at)
                       VALUES (?, '', '', 'adhoc', '[]', '',
                               'orphan', 'wrapper_and_child_both_died', ?, ?)""",
                    (tid, now, now),
                )
            stats["orphans_created"] += 1
            log.warning("bg reconcile: active/%s has no manifest → orphan row created", tid)

    log.info("bg reconcile: manifests replayed=%d orphans=%d",
             stats["completed_replayed"], stats["orphans_created"])
    return stats


def _row_exists(conn: sqlite3.Connection, task_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM bg_tasks WHERE id=?", (task_id,)).fetchone()
    return row is not None


def _is_trusted_task_dir(task_dir: Path) -> bool:
    """B5: reject filesystem entries that don't match the expected task-dir shape.

    - Must be a real directory (not a symlink-to-dir escape)
    - Name must match the ``uuid4().hex`` form used everywhere else
    """
    if task_dir.is_symlink():
        log.warning("bg reconcile: refusing symlink %s", task_dir)
        return False
    if not task_dir.is_dir():
        return False
    if not _TASK_ID_RE.fullmatch(task_dir.name):
        log.warning("bg reconcile: skip malformed task dir %s", task_dir)
        return False
    return True


def _replay_completed_manifest(
    conn: sqlite3.Connection, data: dict[str, Any], manifest_path: str,
    expected_task_id: str,
) -> bool:
    """Insert bg_tasks + bg_runs rows derived from a completed task.json.done.

    Returns True if a new row was written; False if it already existed or the
    manifest failed validation.
    """
    # B5: manifest content is untrusted — verify shape before touching the DB.
    tid = data.get("task_id")
    if not isinstance(tid, str) or not _TASK_ID_RE.fullmatch(tid):
        log.warning("manifest %s has missing/malformed task_id", manifest_path)
        return False
    if tid != expected_task_id:
        log.warning(
            "manifest %s task_id=%r mismatches dir=%r — refusing",
            manifest_path, tid, expected_task_id,
        )
        return False
    schema = data.get("schema_version")
    if schema is not None and (
        not isinstance(schema, int) or schema < 1 or schema > _MAX_MANIFEST_SCHEMA_VERSION
    ):
        log.warning(
            "manifest %s schema_version=%r unsupported (max %d)",
            manifest_path, schema, _MAX_MANIFEST_SCHEMA_VERSION,
        )
        return False
    chat_id = data.get("chat_id", "")
    session_id = data.get("session_id", "")
    if not isinstance(chat_id, str) or not isinstance(session_id, str):
        log.warning("manifest %s has non-string chat_id/session_id", manifest_path)
        return False

    if _row_exists(conn, tid):
        return False

    now = _now_ms()
    state = data.get("state", "completed")
    if state not in {s.value for s in _TERMINAL_STATES}:
        # Any non-terminal state in a .done manifest is suspicious — coerce to orphan.
        log.warning("manifest %s has non-terminal state %r → orphan", manifest_path, state)
        state = TaskState.ORPHAN.value

    command_argv = data.get("command_argv", [])
    if not isinstance(command_argv, list):
        command_argv = []

    with _tx(conn):
        conn.execute(
            """INSERT INTO bg_tasks
               (id, chat_id, session_id, requester_open_id, kind, command_argv,
                cwd, env_overlay, timeout_seconds, on_done_prompt, output_paths,
                state, reason, signal, error_message, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'adhoc', ?,
                       ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?)""",
            (
                tid,
                data.get("chat_id", ""),
                data.get("session_id", ""),
                data.get("requester_open_id"),
                json.dumps(command_argv),
                data.get("cwd"),
                json.dumps(data.get("env_overlay") or {}),
                int(data.get("timeout_seconds", 1800)),
                data.get("on_done_prompt", ""),
                json.dumps(data.get("output_paths") or []),
                state,
                data.get("reason"),
                data.get("signal"),
                data.get("error_message"),
                int(data.get("created_at", now)),
                now,
            ),
        )
        conn.execute(
            """INSERT INTO bg_runs
               (task_id, runner_token, pid, pgid, process_start_time_us,
                wrapper_pid, wrapper_start_time_us, started_at, finished_at,
                exit_code, signal, manifest_path,
                delivery_state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (
                tid,
                data.get("runner_token", ""),
                data.get("pid"),
                data.get("pgid"),
                data.get("process_start_time_us"),
                int(data.get("wrapper_pid", 0)),
                int(data.get("wrapper_start_time_us", 0)),
                int(data.get("started_at", now)),
                data.get("finished_at"),
                data.get("exit_code"),
                data.get("signal"),
                manifest_path,
            ),
        )
    return True


# ---------------------------------------------------------------------------
# Repository (DAO)
# ---------------------------------------------------------------------------

@dataclass
class BgTaskRow:
    id: str
    chat_id: str
    session_id: str
    requester_open_id: Optional[str]
    kind: str
    command_argv: list[str]
    cwd: Optional[str]
    env_overlay: dict[str, str]
    timeout_seconds: int
    on_done_prompt: str
    output_paths: list[str]
    state: str
    reason: Optional[str]
    signal: Optional[str]
    error_message: Optional[str]
    cancel_requested_at: Optional[int]
    claimed_by: Optional[str]
    claimed_at: Optional[int]
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "BgTaskRow":
        def _json(col: Any, default: Any) -> Any:
            if col is None or col == "":
                return default
            try:
                return json.loads(col)
            except json.JSONDecodeError:
                return default
        return cls(
            id=r["id"],
            chat_id=r["chat_id"],
            session_id=r["session_id"],
            requester_open_id=r["requester_open_id"],
            kind=r["kind"],
            command_argv=_json(r["command_argv"], []),
            cwd=r["cwd"],
            env_overlay=_json(r["env_overlay"], {}),
            timeout_seconds=r["timeout_seconds"],
            on_done_prompt=r["on_done_prompt"],
            output_paths=_json(r["output_paths"], []),
            state=r["state"],
            reason=r["reason"],
            signal=r["signal"],
            error_message=r["error_message"],
            cancel_requested_at=r["cancel_requested_at"],
            claimed_by=r["claimed_by"],
            claimed_at=r["claimed_at"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )


class BgTaskRepo:
    """DAO for bg_tasks and bg_runs.

    Every mutation goes through a method here — no raw SQL from callers. This
    centralises state-machine validation and the single-transaction guarantees
    required by the spec.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ---- inserts --------------------------------------------------------------

    def insert_task(
        self,
        *,
        chat_id: str,
        session_id: str,
        command_argv: list[str],
        on_done_prompt: str,
        requester_open_id: Optional[str] = None,
        cwd: Optional[str] = None,
        env_overlay: Optional[dict[str, str]] = None,
        timeout_seconds: int = 1800,
        output_paths: Optional[list[str]] = None,
        task_id: Optional[str] = None,
    ) -> str:
        """Insert a new queued task. Returns the task_id."""
        tid = task_id or uuid.uuid4().hex
        now = _now_ms()
        with _tx(self.conn):
            self.conn.execute(
                """INSERT INTO bg_tasks
                   (id, chat_id, session_id, requester_open_id, kind, command_argv,
                    cwd, env_overlay, timeout_seconds, on_done_prompt, output_paths,
                    state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'adhoc', ?,
                           ?, ?, ?, ?, ?,
                           'queued', ?, ?)""",
                (
                    tid, chat_id, session_id, requester_open_id,
                    json.dumps(command_argv),
                    cwd,
                    json.dumps(env_overlay or {}),
                    timeout_seconds,
                    on_done_prompt,
                    json.dumps(output_paths or []),
                    now, now,
                ),
            )
        return tid

    # ---- reads ----------------------------------------------------------------

    def get(self, task_id: str) -> Optional[BgTaskRow]:
        row = self.conn.execute(
            "SELECT * FROM bg_tasks WHERE id=?", (task_id,),
        ).fetchone()
        return BgTaskRow.from_row(row) if row else None

    def list(
        self,
        *,
        chat_id: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 20,
    ) -> list[BgTaskRow]:
        where = []
        args: list[Any] = []
        if chat_id:
            where.append("chat_id = ?")
            args.append(chat_id)
        if state:
            where.append("state = ?")
            args.append(state)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        args.append(limit)
        rows = self.conn.execute(
            f"SELECT * FROM bg_tasks {clause} ORDER BY updated_at DESC LIMIT ?",
            args,
        ).fetchall()
        return [BgTaskRow.from_row(r) for r in rows]

    def list_queued_for_launch(self, limit: int = 16) -> list[str]:
        rows = self.conn.execute(
            """SELECT id FROM bg_tasks
               WHERE state = 'queued' AND cancel_requested_at IS NULL
               ORDER BY created_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [r["id"] for r in rows]

    def list_pending_deliveries(self, limit: int = 32) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT r.* FROM bg_runs r
               WHERE r.delivery_state IN ('pending','delivery_failed')
                 AND r.delivery_attempt_count < 10
               ORDER BY r.finished_at ASC LIMIT ?""",
            (limit,),
        ).fetchall())

    # ---- CAS claim ------------------------------------------------------------

    def claim_queued_cas(self, task_id: str, bridge_instance_id: str) -> bool:
        """Atomically transition queued→launching. Returns True iff we won.

        WHERE predicate rejects already-claimed rows AND rows with pending cancel
        (so cancel-before-launch never spawns a wrapper).
        """
        now = _now_ms()
        cur = self.conn.execute(
            """UPDATE bg_tasks
               SET state='launching', claimed_by=?, claimed_at=?, updated_at=?
               WHERE id=? AND state='queued' AND cancel_requested_at IS NULL""",
            (bridge_instance_id, now, now, task_id),
        )
        return cur.rowcount == 1

    # ---- guarded state transitions -------------------------------------------

    def set_state_guarded(
        self,
        task_id: str,
        *,
        expected_from: str,
        new_state: str,
        reason: Optional[str] = None,
        signal: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> bool:
        """UPDATE bg_tasks.state only if current state matches `expected_from`.

        Returns True iff one row was updated. Rejects illegal transitions up front
        (a bug on the caller side), and rejects state races via the WHERE clause.
        """
        if not TaskState.validate_transition(expected_from, new_state):
            raise ValueError(
                f"illegal transition {expected_from}→{new_state}"
            )
        now = _now_ms()
        cur = self.conn.execute(
            """UPDATE bg_tasks
               SET state=?, reason=COALESCE(?, reason),
                   signal=COALESCE(?, signal),
                   error_message=COALESCE(?, error_message),
                   updated_at=?
               WHERE id=? AND state=?""",
            (new_state, reason, signal, error_message, now, task_id, expected_from),
        )
        return cur.rowcount == 1

    def set_cancel_requested(self, task_id: str) -> bool:
        """Flag cancellation. State itself is updated by wrapper or reconciler.

        Rejects cancellation on terminal tasks.
        """
        now = _now_ms()
        cur = self.conn.execute(
            """UPDATE bg_tasks
               SET cancel_requested_at = COALESCE(cancel_requested_at, ?),
                   updated_at = ?
               WHERE id=? AND state NOT IN ('completed','failed','cancelled','timeout','orphan')""",
            (now, now, task_id),
        )
        return cur.rowcount == 1

    # ---- bg_runs writes -------------------------------------------------------

    def start_run(
        self,
        *,
        task_id: str,
        runner_token: str,
        wrapper_pid: int,
        wrapper_start_time_us: int,
    ) -> int:
        """Phase P: insert bg_runs row before Popen. Returns new run id."""
        now = _now_ms()
        with _tx(self.conn):
            cur = self.conn.execute(
                """INSERT INTO bg_runs
                   (task_id, runner_token, wrapper_pid, wrapper_start_time_us,
                    started_at, delivery_state)
                   VALUES (?, ?, ?, ?, ?, 'not_ready')""",
                (task_id, runner_token, wrapper_pid, wrapper_start_time_us, now),
            )
            return int(cur.lastrowid or 0)

    def attach_child(
        self,
        *,
        run_id: int,
        task_id: str,
        pid: int,
        pgid: int,
        process_start_time_us: int,
    ) -> bool:
        """Phase S single-transaction: attach child pid AND flip bg_tasks to running.

        Returns True iff both rows updated. Caller should treat False as a race
        that the reconciler will resolve.
        """
        now = _now_ms()
        with _tx(self.conn):
            cur_run = self.conn.execute(
                """UPDATE bg_runs
                   SET pid=?, pgid=?, process_start_time_us=?
                   WHERE id=? AND task_id=? AND pid IS NULL""",
                (pid, pgid, process_start_time_us, run_id, task_id),
            )
            cur_task = self.conn.execute(
                """UPDATE bg_tasks
                   SET state='running', updated_at=?
                   WHERE id=? AND state='launching'""",
                (now, task_id),
            )
            if cur_run.rowcount != 1 or cur_task.rowcount != 1:
                raise _AttachRace(
                    f"attach_child race: run_rows={cur_run.rowcount} task_rows={cur_task.rowcount}"
                )
        return True

    def finish_run(
        self,
        *,
        run_id: int,
        task_id: str,
        terminal_state: str,
        exit_code: Optional[int],
        signal: Optional[str],
        stdout_tail: Optional[bytes],
        stderr_tail: Optional[bytes],
        manifest_path: str,
        reason: Optional[str] = None,
    ) -> bool:
        """Phase C single-transaction: commit bg_runs terminal + flip bg_tasks state.

        Both writes share the transaction so a reconciler never sees an inconsistent
        split view. Legal state edges mirror ``_ALLOWED``:

        - running → {completed, failed, cancelled, timeout, orphan}
        - launching → failed only (early launch kill)

        If ``cancel_requested_at`` was set while the child ran, a ``completed``
        exit is coerced to ``cancelled`` — the wrapper may have missed the
        cancel signal window, but the user's intent is recorded.

        Returns True when both rows updated. Raises ``_FinishRace`` if the row
        state no longer permits the requested transition.
        """
        if not TaskState.is_terminal(terminal_state):
            raise ValueError(f"finish_run requires terminal state, got {terminal_state!r}")
        now = _now_ms()
        with _tx(self.conn):
            # Read current state + cancel flag inside the tx so the decision is
            # consistent with the UPDATE we're about to issue.
            row = self.conn.execute(
                "SELECT state, cancel_requested_at FROM bg_tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise _FinishRace(f"finish_run: task {task_id} not found")
            current_state = row["state"]
            cancel_requested = row["cancel_requested_at"] is not None

            # B2: user asked to cancel — record that intent even if the child
            # finished cleanly before the signal landed.
            if cancel_requested and terminal_state == TaskState.COMPLETED.value:
                terminal_state = TaskState.CANCELLED.value

            # B1: enforce _ALLOWED exactly. Only launching→failed is a legal
            # shortcut; everything else must originate from running.
            if current_state == TaskState.LAUNCHING.value:
                if terminal_state != TaskState.FAILED.value:
                    raise _FinishRace(
                        f"finish_run: launching→{terminal_state} is illegal "
                        f"(only launching→failed permitted)"
                    )
                legal_from_clause = "state='launching'"
            elif current_state == TaskState.RUNNING.value:
                legal_from_clause = "state='running'"
            else:
                raise _FinishRace(
                    f"finish_run: task {task_id} is in {current_state!r}, "
                    f"not running/launching"
                )

            cur_run = self.conn.execute(
                """UPDATE bg_runs
                   SET finished_at=?, exit_code=?, signal=?,
                       stdout_tail=?, stderr_tail=?,
                       manifest_path=?, delivery_state='pending',
                       completion_detected_at=?
                   WHERE id=? AND task_id=? AND finished_at IS NULL""",
                (now, exit_code, signal, stdout_tail, stderr_tail,
                 manifest_path, now, run_id, task_id),
            )
            cur_task = self.conn.execute(
                f"""UPDATE bg_tasks
                    SET state=?, reason=COALESCE(?, reason), signal=COALESCE(?, signal),
                        updated_at=?
                    WHERE id=? AND {legal_from_clause}""",
                (terminal_state, reason, signal, now, task_id),
            )
            if cur_run.rowcount != 1 or cur_task.rowcount != 1:
                raise _FinishRace(
                    f"finish_run race: run_rows={cur_run.rowcount} task_rows={cur_task.rowcount}"
                )
        return True

    def mark_delivery_state(
        self,
        run_id: int,
        new_delivery_state: str,
        *,
        error: Optional[str] = None,
        bump_attempt: bool = False,
        enqueued_at: Optional[int] = None,
        sent_at: Optional[int] = None,
        session_resume_status: Optional[str] = None,
    ) -> bool:
        allowed = {"pending", "enqueued", "sent", "delivery_failed"}
        if new_delivery_state not in allowed:
            raise ValueError(f"bad delivery_state {new_delivery_state!r}")
        cur = self.conn.execute(
            f"""UPDATE bg_runs
                SET delivery_state=?,
                    delivery_error=COALESCE(?, delivery_error),
                    delivery_attempt_count = delivery_attempt_count + ?,
                    enqueued_at = COALESCE(?, enqueued_at),
                    sent_at = COALESCE(?, sent_at),
                    session_resume_status = COALESCE(?, session_resume_status)
                WHERE id=?""",
            (new_delivery_state, error, 1 if bump_attempt else 0,
             enqueued_at, sent_at, session_resume_status, run_id),
        )
        return cur.rowcount == 1


class _AttachRace(RuntimeError):
    pass


class _FinishRace(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)
