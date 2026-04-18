"""Unit tests for feishu_bridge.bg_tasks_db (Tasks 1.1 – 1.4)."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import pytest

from feishu_bridge.bg_tasks_db import (
    SCHEMA_VERSION,
    BgTaskRepo,
    TaskState,
    _AttachRace,
    _FinishRace,
    _now_ms,
    init_db,
    integrity_check_and_maybe_quarantine,
    rebuild_from_manifests,
)


# ---------------------------------------------------------------------------
# 1.1 — schema bootstrap
# ---------------------------------------------------------------------------

def _bootstrap(tmp_path: Path) -> sqlite3.Connection:
    conn = init_db(tmp_path / "bg.db")
    return conn


def test_schema_tables_and_indexes_created(tmp_path):
    conn = _bootstrap(tmp_path)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert {"bg_tasks", "bg_runs", "bg_schema"}.issubset(tables)

    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
    ).fetchall()}
    assert "idx_bg_tasks_state" in idx
    assert "idx_bg_tasks_launching" in idx
    assert "idx_bg_runs_delivery" in idx


def test_schema_pragmas_applied(tmp_path):
    conn = _bootstrap(tmp_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_init_db_idempotent(tmp_path):
    db = tmp_path / "bg.db"
    init_db(db).close()
    # Second call must not raise and must keep schema row.
    conn = init_db(db)
    row = conn.execute("SELECT value FROM bg_schema WHERE key='version'").fetchone()
    assert row[0] == str(SCHEMA_VERSION)


def test_schema_check_constraints_reject_bad_kind(tmp_path):
    conn = _bootstrap(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO bg_tasks
               (id, chat_id, session_id, kind, command_argv, on_done_prompt,
                created_at, updated_at)
               VALUES ('x','c','s','NOT_ADHOC','[]','',0,0)"""
        )


def test_schema_check_constraints_reject_bad_delivery_state(tmp_path):
    conn = _bootstrap(tmp_path)
    repo = BgTaskRepo(conn)
    tid = repo.insert_task(
        chat_id="c", session_id="s", command_argv=["sleep", "1"],
        on_done_prompt="done",
    )
    run_id = repo.start_run(
        task_id=tid, runner_token="t", wrapper_pid=1, wrapper_start_time_us=1,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE bg_runs SET delivery_state='BOGUS' WHERE id=?", (run_id,),
        )


# ---------------------------------------------------------------------------
# 1.2 — state machine
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("old,new", [
    ("queued",    "launching"),
    ("queued",    "cancelled"),
    ("launching", "running"),
    ("launching", "failed"),
    ("running",   "completed"),
    ("running",   "failed"),
    ("running",   "cancelled"),
    ("running",   "timeout"),
    ("running",   "orphan"),
])
def test_state_transitions_legal(old, new):
    assert TaskState.validate_transition(old, new)


@pytest.mark.parametrize("old,new", [
    ("queued",    "running"),        # must pass through launching
    ("queued",    "completed"),
    ("completed", "running"),        # terminal → anything disallowed
    ("cancelled", "running"),
    ("orphan",    "running"),
    ("running",   "queued"),         # no going back
    ("launching", "cancelled"),      # must go via running or be reaped to failed
    ("launching", "completed"),
])
def test_state_transitions_illegal(old, new):
    assert not TaskState.validate_transition(old, new)


def test_terminal_states_set():
    assert TaskState.terminal() == frozenset({
        TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED,
        TaskState.TIMEOUT, TaskState.ORPHAN,
    })
    for s in TaskState:
        is_term = s in TaskState.terminal()
        assert TaskState.is_terminal(s.value) is is_term


# ---------------------------------------------------------------------------
# 1.3 — repo CRUD + CAS claim + guarded transitions
# ---------------------------------------------------------------------------

def test_insert_and_get_roundtrip(tmp_path):
    conn = _bootstrap(tmp_path)
    repo = BgTaskRepo(conn)
    tid = repo.insert_task(
        chat_id="oc_x", session_id="sess_y",
        command_argv=["sleep", "1"],
        on_done_prompt="done",
        env_overlay={"FOO": "bar"},
        output_paths=["/tmp/a"],
        timeout_seconds=60,
    )
    row = repo.get(tid)
    assert row is not None
    assert row.state == "queued"
    assert row.command_argv == ["sleep", "1"]
    assert row.env_overlay == {"FOO": "bar"}
    assert row.output_paths == ["/tmp/a"]
    assert row.timeout_seconds == 60


def test_list_filters_and_limit(tmp_path):
    conn = _bootstrap(tmp_path)
    repo = BgTaskRepo(conn)
    for i in range(5):
        repo.insert_task(
            chat_id="cA" if i % 2 == 0 else "cB",
            session_id=f"s{i}",
            command_argv=["echo", str(i)],
            on_done_prompt="",
        )
    assert len(repo.list(chat_id="cA")) == 3
    assert len(repo.list(limit=2)) == 2
    assert len(repo.list(state="queued")) == 5


def test_claim_queued_cas_under_concurrency(tmp_path):
    """100 threads race for one queued row; exactly one wins."""
    db_path = tmp_path / "bg.db"
    init_db(db_path).close()

    # Seed the row via a fresh connection so the race begins equally.
    seed_conn = init_db(db_path)
    seed_repo = BgTaskRepo(seed_conn)
    tid = seed_repo.insert_task(
        chat_id="c", session_id="s",
        command_argv=["true"], on_done_prompt="",
    )
    seed_conn.close()

    winners: list[str] = []
    winners_lock = threading.Lock()

    def worker(idx: int) -> None:
        conn = init_db(db_path)
        try:
            repo = BgTaskRepo(conn)
            if repo.claim_queued_cas(tid, bridge_instance_id=f"b{idx}"):
                with winners_lock:
                    winners.append(f"b{idx}")
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1, winners

    # Row should now be in 'launching' state, claimed by the one winner.
    verify_conn = init_db(db_path)
    row = BgTaskRepo(verify_conn).get(tid)
    assert row.state == "launching"
    assert row.claimed_by in winners


def test_insert_100_tasks_under_2_seconds(tmp_path):
    conn = _bootstrap(tmp_path)
    repo = BgTaskRepo(conn)
    t0 = time.monotonic()
    for i in range(100):
        repo.insert_task(
            chat_id=f"c{i}", session_id=f"s{i}",
            command_argv=["true"], on_done_prompt="",
        )
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"100 inserts took {elapsed:.2f}s"


def test_cas_rejects_already_cancelled(tmp_path):
    conn = _bootstrap(tmp_path)
    repo = BgTaskRepo(conn)
    tid = repo.insert_task(
        chat_id="c", session_id="s", command_argv=["true"], on_done_prompt="",
    )
    assert repo.set_cancel_requested(tid)
    assert repo.claim_queued_cas(tid, bridge_instance_id="b1") is False
    assert repo.get(tid).state == "queued"


def test_set_state_guarded_rejects_illegal(tmp_path):
    conn = _bootstrap(tmp_path)
    repo = BgTaskRepo(conn)
    tid = repo.insert_task(
        chat_id="c", session_id="s", command_argv=["true"], on_done_prompt="",
    )
    with pytest.raises(ValueError):
        repo.set_state_guarded(tid, expected_from="queued", new_state="running")


def test_set_state_guarded_rejects_state_mismatch(tmp_path):
    conn = _bootstrap(tmp_path)
    repo = BgTaskRepo(conn)
    tid = repo.insert_task(
        chat_id="c", session_id="s", command_argv=["true"], on_done_prompt="",
    )
    # Pretend to claim via CAS — state becomes 'launching'.
    repo.claim_queued_cas(tid, bridge_instance_id="b1")
    # An old caller still thinks state='queued' → transition is legal in table
    # (queued→cancelled) but the WHERE guard rejects because row is now launching.
    assert repo.set_state_guarded(tid, expected_from="queued", new_state="cancelled") is False


def test_set_cancel_requested_rejects_terminal(tmp_path):
    conn = _bootstrap(tmp_path)
    repo = BgTaskRepo(conn)
    tid = repo.insert_task(
        chat_id="c", session_id="s", command_argv=["true"], on_done_prompt="",
    )
    repo.claim_queued_cas(tid, bridge_instance_id="b1")
    repo.set_state_guarded(tid, expected_from="launching", new_state="failed",
                           reason="x")
    assert repo.set_cancel_requested(tid) is False


def test_start_run_attach_child_finish_run_happy_path(tmp_path):
    conn = _bootstrap(tmp_path)
    repo = BgTaskRepo(conn)
    tid = repo.insert_task(
        chat_id="c", session_id="s", command_argv=["true"], on_done_prompt="",
    )
    assert repo.claim_queued_cas(tid, bridge_instance_id="b1")
    run_id = repo.start_run(
        task_id=tid, runner_token="t", wrapper_pid=1000, wrapper_start_time_us=9000,
    )
    assert run_id > 0
    # bg_tasks still launching (Phase S not yet committed).
    assert repo.get(tid).state == "launching"
    repo.attach_child(
        run_id=run_id, task_id=tid, pid=2000, pgid=2000,
        process_start_time_us=9100,
    )
    assert repo.get(tid).state == "running"
    repo.finish_run(
        run_id=run_id, task_id=tid, terminal_state="completed",
        exit_code=0, signal=None,
        stdout_tail=b"hello", stderr_tail=b"",
        manifest_path="/tmp/manifest.json",
    )
    task = repo.get(tid)
    assert task.state == "completed"
    # bg_runs should be in delivery_state='pending'
    row = conn.execute(
        "SELECT delivery_state, manifest_path FROM bg_runs WHERE id=?", (run_id,)
    ).fetchone()
    assert row["delivery_state"] == "pending"
    assert row["manifest_path"] == "/tmp/manifest.json"


def test_finish_run_rejects_non_terminal(tmp_path):
    conn = _bootstrap(tmp_path)
    repo = BgTaskRepo(conn)
    tid = repo.insert_task(
        chat_id="c", session_id="s", command_argv=["true"], on_done_prompt="",
    )
    repo.claim_queued_cas(tid, bridge_instance_id="b1")
    run_id = repo.start_run(
        task_id=tid, runner_token="t", wrapper_pid=1, wrapper_start_time_us=1,
    )
    with pytest.raises(ValueError):
        repo.finish_run(
            run_id=run_id, task_id=tid, terminal_state="running",
            exit_code=0, signal=None,
            stdout_tail=None, stderr_tail=None,
            manifest_path="",
        )


def test_attach_child_race_raises(tmp_path):
    """If the bg_tasks row somehow already moved past launching, Phase S aborts."""
    conn = _bootstrap(tmp_path)
    repo = BgTaskRepo(conn)
    tid = repo.insert_task(
        chat_id="c", session_id="s", command_argv=["true"], on_done_prompt="",
    )
    repo.claim_queued_cas(tid, bridge_instance_id="b1")
    run_id = repo.start_run(
        task_id=tid, runner_token="t", wrapper_pid=1, wrapper_start_time_us=1,
    )
    # Simulate reconciler externally flipping the task to failed.
    repo.set_state_guarded(tid, expected_from="launching", new_state="failed",
                           reason="launch_interrupted")
    with pytest.raises(_AttachRace):
        repo.attach_child(
            run_id=run_id, task_id=tid, pid=2, pgid=2, process_start_time_us=1,
        )


def test_mark_delivery_state_transitions(tmp_path):
    conn = _bootstrap(tmp_path)
    repo = BgTaskRepo(conn)
    tid = repo.insert_task(
        chat_id="c", session_id="s", command_argv=["true"], on_done_prompt="",
    )
    repo.claim_queued_cas(tid, bridge_instance_id="b1")
    run_id = repo.start_run(
        task_id=tid, runner_token="t", wrapper_pid=1, wrapper_start_time_us=1,
    )
    repo.attach_child(
        run_id=run_id, task_id=tid, pid=1, pgid=1, process_start_time_us=1,
    )
    repo.finish_run(
        run_id=run_id, task_id=tid, terminal_state="completed",
        exit_code=0, signal=None,
        stdout_tail=None, stderr_tail=None, manifest_path="/tmp/m",
    )
    now = _now_ms()
    assert repo.mark_delivery_state(run_id, "enqueued", enqueued_at=now)
    assert repo.mark_delivery_state(run_id, "sent", sent_at=now+1)
    row = conn.execute(
        "SELECT delivery_state, enqueued_at, sent_at FROM bg_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    assert row["delivery_state"] == "sent"
    assert row["enqueued_at"] == now
    assert row["sent_at"] == now + 1


def test_mark_delivery_state_attempt_bump(tmp_path):
    conn = _bootstrap(tmp_path)
    repo = BgTaskRepo(conn)
    tid = repo.insert_task(
        chat_id="c", session_id="s", command_argv=["true"], on_done_prompt="",
    )
    repo.claim_queued_cas(tid, bridge_instance_id="b1")
    run_id = repo.start_run(
        task_id=tid, runner_token="t", wrapper_pid=1, wrapper_start_time_us=1,
    )
    repo.attach_child(
        run_id=run_id, task_id=tid, pid=1, pgid=1, process_start_time_us=1,
    )
    repo.finish_run(
        run_id=run_id, task_id=tid, terminal_state="completed",
        exit_code=0, signal=None,
        stdout_tail=None, stderr_tail=None, manifest_path="/tmp/m",
    )
    repo.mark_delivery_state(run_id, "delivery_failed", error="boom", bump_attempt=True)
    repo.mark_delivery_state(run_id, "delivery_failed", error="boom2", bump_attempt=True)
    row = conn.execute(
        "SELECT delivery_attempt_count, delivery_error FROM bg_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    assert row["delivery_attempt_count"] == 2
    assert row["delivery_error"] == "boom2"


def test_list_pending_deliveries_filters_attempt_cap(tmp_path):
    conn = _bootstrap(tmp_path)
    repo = BgTaskRepo(conn)

    def _finished_run(manifest: str, delivery_state: str, attempts: int) -> int:
        tid = repo.insert_task(
            chat_id="c", session_id="s", command_argv=["true"], on_done_prompt="",
        )
        repo.claim_queued_cas(tid, bridge_instance_id="b")
        rid = repo.start_run(
            task_id=tid, runner_token="t", wrapper_pid=1, wrapper_start_time_us=1,
        )
        repo.attach_child(run_id=rid, task_id=tid, pid=1, pgid=1, process_start_time_us=1)
        repo.finish_run(
            run_id=rid, task_id=tid, terminal_state="completed",
            exit_code=0, signal=None,
            stdout_tail=None, stderr_tail=None, manifest_path=manifest,
        )
        conn.execute(
            "UPDATE bg_runs SET delivery_state=?, delivery_attempt_count=? WHERE id=?",
            (delivery_state, attempts, rid),
        )
        return rid

    _finished_run("/tmp/a", "pending", 0)
    _finished_run("/tmp/b", "delivery_failed", 3)
    _finished_run("/tmp/c", "delivery_failed", 10)   # above cap → excluded
    _finished_run("/tmp/d", "sent", 0)               # already sent → excluded
    _finished_run("/tmp/e", "enqueued", 0)           # in-flight, not a candidate
    _finished_run("/tmp/f", "not_ready", 0)          # never committed, not a candidate

    rows = repo.list_pending_deliveries()
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# 1.4 — integrity check + manifest replay
# ---------------------------------------------------------------------------

def test_integrity_check_passes_on_fresh_db(tmp_path):
    db = tmp_path / "bg.db"
    init_db(db).close()
    # Release WAL sidecars by opening+closing. integrity_check should be ok.
    result = integrity_check_and_maybe_quarantine(db)
    assert result == db
    assert db.exists()


def test_integrity_check_quarantines_corrupt_db(tmp_path):
    db = tmp_path / "bg.db"
    init_db(db).close()
    # Truncate header → catastrophic corruption that integrity_check WILL catch.
    data = db.read_bytes()
    db.write_bytes(b"\x00" * 100 + data[100:])
    result = integrity_check_and_maybe_quarantine(db)
    # Returned path is the original — absent now, caller will re-init.
    assert result == db
    assert not db.exists()
    # Quarantine file must exist.
    quarantined = list(tmp_path.glob("bg.db.quarantine.*"))
    assert len(quarantined) == 1


def test_rebuild_from_manifests_replays_completed(tmp_path):
    # Quarantine scenario: DB is gone. Re-init, then replay from filesystem.
    tasks_dir = tmp_path / "tasks"
    completed = tasks_dir / "completed"
    tid = uuid.uuid4().hex
    task_dir = completed / tid
    task_dir.mkdir(parents=True)
    manifest = task_dir / "task.json.done"
    manifest.write_text(json.dumps({
        "task_id": tid,
        "chat_id": "oc_x",
        "session_id": "sess_y",
        "command_argv": ["sleep", "1"],
        "on_done_prompt": "done",
        "state": "completed",
        "exit_code": 0,
        "wrapper_pid": 9001,
        "wrapper_start_time_us": 12345,
        "started_at": 1000,
        "finished_at": 2000,
        "created_at": 500,
        "runner_token": "tok",
    }))

    conn = init_db(tmp_path / "bg.db")
    stats = rebuild_from_manifests(conn, tasks_dir)
    assert stats["completed_replayed"] == 1
    assert stats["orphans_created"] == 0

    row = conn.execute("SELECT state FROM bg_tasks WHERE id=?", (tid,)).fetchone()
    assert row["state"] == "completed"
    run = conn.execute(
        "SELECT delivery_state, manifest_path FROM bg_runs WHERE task_id=?", (tid,)
    ).fetchone()
    assert run["delivery_state"] == "pending"
    assert run["manifest_path"].endswith("task.json.done")


def test_rebuild_from_manifests_uses_canonical_manifest_schema(tmp_path):
    """Regression: task_runner writes started_at_ms/finished_at_ms + base64 tails.

    Earlier replay code read plain started_at/finished_at and ignored tails,
    silently zeroing timestamps and dropping tails on every recovered row.
    """
    import base64
    tasks_dir = tmp_path / "tasks"
    completed = tasks_dir / "completed"
    tid = uuid.uuid4().hex
    task_dir = completed / tid
    task_dir.mkdir(parents=True)
    manifest = task_dir / "task.json.done"
    stdout_raw = b"hello stdout\n"
    stderr_raw = b"hello stderr\n"
    manifest.write_text(json.dumps({
        "task_id": tid,
        "chat_id": "oc_x",
        "session_id": "sess_y",
        "command_argv": ["echo", "hi"],
        "on_done_prompt": "done",
        "state": "completed",
        "exit_code": 0,
        "wrapper_pid": 9001,
        "wrapper_start_time_us": 12345,
        "started_at_ms": 1700000000000,
        "finished_at_ms": 1700000005000,
        "created_at": 1700000000000,
        "runner_token": "tok",
        "stdout_tail_b64": base64.b64encode(stdout_raw).decode("ascii"),
        "stderr_tail_b64": base64.b64encode(stderr_raw).decode("ascii"),
    }))

    conn = init_db(tmp_path / "bg.db")
    stats = rebuild_from_manifests(conn, tasks_dir)
    assert stats["completed_replayed"] == 1

    run = conn.execute(
        "SELECT started_at, finished_at, stdout_tail, stderr_tail "
        "FROM bg_runs WHERE task_id=?", (tid,)
    ).fetchone()
    assert run["started_at"] == 1700000000000
    assert run["finished_at"] == 1700000005000
    assert run["stdout_tail"] == stdout_raw
    assert run["stderr_tail"] == stderr_raw


def test_rebuild_from_manifests_active_without_manifest_marks_orphan(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tid = uuid.uuid4().hex
    (tasks_dir / "active" / tid).mkdir(parents=True)
    (tasks_dir / "active" / tid / "stdout.log").write_bytes(b"partial")

    conn = init_db(tmp_path / "bg.db")
    stats = rebuild_from_manifests(conn, tasks_dir)
    assert stats["completed_replayed"] == 0
    assert stats["orphans_created"] == 1
    row = conn.execute("SELECT state, reason FROM bg_tasks WHERE id=?", (tid,)).fetchone()
    assert row["state"] == "orphan"
    assert row["reason"] == "wrapper_and_child_both_died"


def test_rebuild_from_manifests_replay_only_suppresses_orphan_minting(tmp_path):
    """Regression: reconcile on live DB must not mint orphan rows for active/

    dirs with no manifest — the wrapper may still be alive writing its
    manifest, and mis-marking it as orphan would break a running task.
    Only quarantine recovery (empty DB) should synthesize orphans.
    """
    tasks_dir = tmp_path / "tasks"
    tid = uuid.uuid4().hex
    (tasks_dir / "active" / tid).mkdir(parents=True)
    (tasks_dir / "active" / tid / "stdout.log").write_bytes(b"partial")

    conn = init_db(tmp_path / "bg.db")
    stats = rebuild_from_manifests(conn, tasks_dir, replay_only=True)
    assert stats["completed_replayed"] == 0
    assert stats["orphans_created"] == 0
    row = conn.execute("SELECT state FROM bg_tasks WHERE id=?", (tid,)).fetchone()
    assert row is None


def test_rebuild_from_manifests_preserves_null_finished_at_ms(tmp_path):
    """Regression: a completed manifest missing finished_at_ms stores NULL."""
    tasks_dir = tmp_path / "tasks"
    completed = tasks_dir / "completed"
    tid = uuid.uuid4().hex
    task_dir = completed / tid
    task_dir.mkdir(parents=True)
    (task_dir / "task.json.done").write_text(json.dumps({
        "task_id": tid,
        "chat_id": "oc_x",
        "session_id": "sess_y",
        "command_argv": ["echo", "hi"],
        "on_done_prompt": "done",
        "state": "completed",
        "exit_code": 0,
        "wrapper_pid": 9001,
        "wrapper_start_time_us": 12345,
        "started_at_ms": 1700000000000,
        "runner_token": "tok",
    }))

    conn = init_db(tmp_path / "bg.db")
    stats = rebuild_from_manifests(conn, tasks_dir)
    assert stats["completed_replayed"] == 1
    run = conn.execute(
        "SELECT started_at, finished_at FROM bg_runs WHERE task_id=?", (tid,)
    ).fetchone()
    assert run["started_at"] == 1700000000000
    assert run["finished_at"] is None


def test_rebuild_from_manifests_skips_corrupt_manifest(tmp_path):
    tasks_dir = tmp_path / "tasks"
    completed = tasks_dir / "completed"
    tid = uuid.uuid4().hex
    task_dir = completed / tid
    task_dir.mkdir(parents=True)
    (task_dir / "task.json.done").write_text("{not valid json")

    conn = init_db(tmp_path / "bg.db")
    stats = rebuild_from_manifests(conn, tasks_dir)
    assert stats["completed_replayed"] == 0


def test_db_dir_created_with_secure_perms(tmp_path):
    target = tmp_path / "nested" / "bg.db"
    init_db(target).close()
    mode = os.stat(target.parent).st_mode & 0o777
    assert mode == 0o700


# ---------------------------------------------------------------------------
# Regression tests for code-review findings B1 / B2 / B3 / B5
# ---------------------------------------------------------------------------

def test_finish_run_rejects_launching_to_completed(tmp_path):
    """B1: launching→completed is illegal per _ALLOWED; must raise _FinishRace."""
    conn = _bootstrap(tmp_path)
    repo = BgTaskRepo(conn)
    tid = repo.insert_task(
        chat_id="c", session_id="s", command_argv=["true"], on_done_prompt="",
    )
    repo.claim_queued_cas(tid, bridge_instance_id="b1")
    run_id = repo.start_run(
        task_id=tid, runner_token="t", wrapper_pid=1, wrapper_start_time_us=1,
    )
    # State is 'launching' — attach_child was never called.
    with pytest.raises(_FinishRace):
        repo.finish_run(
            run_id=run_id, task_id=tid, terminal_state="completed",
            exit_code=0, signal=None,
            stdout_tail=None, stderr_tail=None, manifest_path="/tmp/m",
        )
    # Neither row was written — roll-back guarantee.
    task = repo.get(tid)
    assert task.state == "launching"
    run_row = conn.execute(
        "SELECT finished_at, delivery_state FROM bg_runs WHERE id=?", (run_id,)
    ).fetchone()
    assert run_row["finished_at"] is None
    assert run_row["delivery_state"] == "not_ready"


def test_finish_run_launching_to_failed_allowed(tmp_path):
    """B1: launching→failed is the one legal shortcut (launch reaped before run)."""
    conn = _bootstrap(tmp_path)
    repo = BgTaskRepo(conn)
    tid = repo.insert_task(
        chat_id="c", session_id="s", command_argv=["true"], on_done_prompt="",
    )
    repo.claim_queued_cas(tid, bridge_instance_id="b1")
    run_id = repo.start_run(
        task_id=tid, runner_token="t", wrapper_pid=1, wrapper_start_time_us=1,
    )
    assert repo.finish_run(
        run_id=run_id, task_id=tid, terminal_state="failed",
        exit_code=None, signal="SIGKILL",
        stdout_tail=None, stderr_tail=None, manifest_path="/tmp/m",
        reason="launch_interrupted",
    )
    assert repo.get(tid).state == "failed"


def test_finish_run_coerces_completed_to_cancelled_on_cancel_request(tmp_path):
    """B2: user cancel + race with clean exit — preserve cancel intent."""
    conn = _bootstrap(tmp_path)
    repo = BgTaskRepo(conn)
    tid = repo.insert_task(
        chat_id="c", session_id="s", command_argv=["true"], on_done_prompt="",
    )
    repo.claim_queued_cas(tid, bridge_instance_id="b1")
    run_id = repo.start_run(
        task_id=tid, runner_token="t", wrapper_pid=1, wrapper_start_time_us=1,
    )
    repo.attach_child(
        run_id=run_id, task_id=tid, pid=1, pgid=1, process_start_time_us=1,
    )
    # User cancels while child is still running.
    assert repo.set_cancel_requested(tid)
    # Wrapper reports clean exit (child finished before signal landed).
    repo.finish_run(
        run_id=run_id, task_id=tid, terminal_state="completed",
        exit_code=0, signal=None,
        stdout_tail=None, stderr_tail=None, manifest_path="/tmp/m",
    )
    assert repo.get(tid).state == "cancelled"


def test_finish_run_does_not_coerce_failed_on_cancel_request(tmp_path):
    """B2: only completed is coerced; failed/timeout keep their own terminal state."""
    conn = _bootstrap(tmp_path)
    repo = BgTaskRepo(conn)
    tid = repo.insert_task(
        chat_id="c", session_id="s", command_argv=["true"], on_done_prompt="",
    )
    repo.claim_queued_cas(tid, bridge_instance_id="b1")
    run_id = repo.start_run(
        task_id=tid, runner_token="t", wrapper_pid=1, wrapper_start_time_us=1,
    )
    repo.attach_child(
        run_id=run_id, task_id=tid, pid=1, pgid=1, process_start_time_us=1,
    )
    repo.set_cancel_requested(tid)
    repo.finish_run(
        run_id=run_id, task_id=tid, terminal_state="failed",
        exit_code=1, signal=None,
        stdout_tail=None, stderr_tail=None, manifest_path="/tmp/m",
    )
    # Failed wins: the child actually failed; cancel doesn't override exit status.
    assert repo.get(tid).state == "failed"


def test_rebuild_from_manifests_replays_active_with_committed_done(tmp_path):
    """B3: wrapper crashed between .partial→.done rename and active→completed mv.

    The committed manifest must be replayed and the directory promoted so a
    second reconcile doesn't re-process it.
    """
    tasks_dir = tmp_path / "tasks"
    tid = uuid.uuid4().hex
    active_task = tasks_dir / "active" / tid
    active_task.mkdir(parents=True)
    (active_task / "task.json.done").write_text(json.dumps({
        "schema_version": 2,
        "task_id": tid,
        "chat_id": "oc_x",
        "session_id": "sess_y",
        "command_argv": ["true"],
        "on_done_prompt": "",
        "state": "completed",
        "exit_code": 0,
        "wrapper_pid": 1,
        "wrapper_start_time_us": 1,
        "started_at": 1,
        "finished_at": 2,
        "created_at": 0,
        "runner_token": "tok",
    }))

    conn = init_db(tmp_path / "bg.db")
    stats = rebuild_from_manifests(conn, tasks_dir)
    assert stats["completed_replayed"] == 1
    assert stats["orphans_created"] == 0
    row = conn.execute("SELECT state FROM bg_tasks WHERE id=?", (tid,)).fetchone()
    assert row["state"] == "completed"
    # Directory was promoted.
    assert not active_task.exists()
    assert (tasks_dir / "completed" / tid / "task.json.done").is_file()


def test_rebuild_from_manifests_rejects_mismatched_task_id(tmp_path):
    """B5: manifest task_id must match dir name; otherwise we refuse to write."""
    tasks_dir = tmp_path / "tasks"
    dir_tid = uuid.uuid4().hex
    claim_tid = uuid.uuid4().hex       # different, attacker-chosen
    task_dir = tasks_dir / "completed" / dir_tid
    task_dir.mkdir(parents=True)
    (task_dir / "task.json.done").write_text(json.dumps({
        "schema_version": 2,
        "task_id": claim_tid,
        "chat_id": "oc_x",
        "session_id": "sess_y",
        "command_argv": ["true"],
        "on_done_prompt": "",
        "state": "completed",
        "wrapper_pid": 1,
        "wrapper_start_time_us": 1,
        "runner_token": "tok",
    }))

    conn = init_db(tmp_path / "bg.db")
    stats = rebuild_from_manifests(conn, tasks_dir)
    assert stats["completed_replayed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM bg_tasks").fetchone()[0] == 0


def test_rebuild_from_manifests_rejects_non_uuid_dir(tmp_path):
    """B5: malformed directory names (non-32-hex) are skipped silently."""
    tasks_dir = tmp_path / "tasks"
    bad = tasks_dir / "completed" / "not-a-uuid"
    bad.mkdir(parents=True)
    (bad / "task.json.done").write_text(json.dumps({
        "schema_version": 2, "task_id": "not-a-uuid",
        "chat_id": "", "session_id": "",
        "command_argv": [], "on_done_prompt": "",
        "state": "completed", "wrapper_pid": 1, "wrapper_start_time_us": 1,
        "runner_token": "t",
    }))
    conn = init_db(tmp_path / "bg.db")
    stats = rebuild_from_manifests(conn, tasks_dir)
    assert stats["completed_replayed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM bg_tasks").fetchone()[0] == 0


def test_rebuild_from_manifests_rejects_future_schema_version(tmp_path):
    """B5: schema_version beyond what we know how to replay is refused."""
    tasks_dir = tmp_path / "tasks"
    tid = uuid.uuid4().hex
    task_dir = tasks_dir / "completed" / tid
    task_dir.mkdir(parents=True)
    (task_dir / "task.json.done").write_text(json.dumps({
        "schema_version": 999,
        "task_id": tid,
        "chat_id": "", "session_id": "",
        "command_argv": [], "on_done_prompt": "",
        "state": "completed", "wrapper_pid": 1, "wrapper_start_time_us": 1,
        "runner_token": "t",
    }))
    conn = init_db(tmp_path / "bg.db")
    stats = rebuild_from_manifests(conn, tasks_dir)
    assert stats["completed_replayed"] == 0


def test_rebuild_from_manifests_rejects_symlink_dir(tmp_path):
    """B5: symlinks under active/ or completed/ are not trusted."""
    tasks_dir = tmp_path / "tasks"
    completed = tasks_dir / "completed"
    completed.mkdir(parents=True)
    # A real task elsewhere.
    real_tid = uuid.uuid4().hex
    real_dir = tmp_path / "elsewhere" / real_tid
    real_dir.mkdir(parents=True)
    (real_dir / "task.json.done").write_text("{}")
    # Symlink it into completed/ under a different name.
    evil_tid = uuid.uuid4().hex
    (completed / evil_tid).symlink_to(real_dir, target_is_directory=True)

    conn = init_db(tmp_path / "bg.db")
    stats = rebuild_from_manifests(conn, tasks_dir)
    assert stats["completed_replayed"] == 0
