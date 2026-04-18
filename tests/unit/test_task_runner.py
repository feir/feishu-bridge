"""Unit tests for feishu_bridge.task_runner (Section 2 — wrapper binary).

Covers the pure helpers (libproc, utf8_safe_tail, build_manifest_dict,
write_manifest_atomically, terminate_pgid) plus one integration test that
drives the full P→S→W→C lifecycle against a real short-lived child.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from feishu_bridge import task_runner
from feishu_bridge.bg_tasks_db import BgTaskRepo, TaskState, init_db


# ---------------------------------------------------------------------------
# libproc — struct layout + self-pid start_time plausibility
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS-only")
def test_proc_bsdinfo_struct_size_matches_xnu():
    """Sentinel: xnu struct must stay 136 bytes for offsets 120/128 to line up."""
    assert ctypes.sizeof(task_runner._ProcBsdInfo) == task_runner.PROC_PIDTBSDINFO_SIZE


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS-only")
def test_read_proc_start_time_self_is_recent():
    pid = os.getpid()
    start_us = task_runner.read_proc_start_time_us(pid)
    now_us = int(time.time() * 1_000_000)
    # self has been running less than 60s at test start; μs epoch.
    assert start_us > 0
    assert now_us - start_us < 600 * 1_000_000, (
        f"self start_time too old: {now_us - start_us} μs ago"
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS-only")
def test_read_proc_start_time_raises_for_dead_pid():
    # Spawn a quick child, wait for exit, then query → should raise.
    p = subprocess.Popen(["true"])
    p.wait()
    # kernel often reaps immediately; start_time read may succeed OR raise.
    # Use a pid that's almost certainly absent (max pid + 1).
    with pytest.raises(OSError):
        task_runner.read_proc_start_time_us(2_000_000)


# ---------------------------------------------------------------------------
# utf8_safe_tail — boundary alignment
# ---------------------------------------------------------------------------

def test_utf8_safe_tail_ascii_under_limit():
    assert task_runner.utf8_safe_tail(b"hello", limit=100) == b"hello"


def test_utf8_safe_tail_ascii_over_limit():
    payload = b"a" * 5000
    out = task_runner.utf8_safe_tail(payload, limit=4096)
    assert len(out) == 4096
    assert out == b"a" * 4096


def test_utf8_safe_tail_skips_split_emoji():
    # 😀 = U+1F600 = 4 bytes: F0 9F 98 80. Chunk such that the split lands mid-codepoint.
    prefix = b"X" * 100
    emoji = "😀".encode("utf-8")  # 4 bytes
    # Construct payload where last_limit bytes start inside the 4-byte sequence.
    # limit=2 → tail begins at byte offset len-2; in the final 😀 the last 2 bytes
    # are continuation (0x98 0x80) → both must be skipped, landing at b"".
    payload = prefix + emoji
    tail = task_runner.utf8_safe_tail(payload, limit=2)
    # Tail slice is bytes[-2:] = b"\x98\x80"; both are continuation bytes → stripped.
    assert tail == b""


def test_utf8_safe_tail_aligned_boundary_no_strip():
    # Boundary exactly at codepoint start: limit = len(emoji) = 4.
    payload = b"X" + "😀".encode("utf-8")
    tail = task_runner.utf8_safe_tail(payload, limit=4)
    # Exactly the emoji, no continuation byte at head.
    assert tail.decode("utf-8") == "😀"


def test_utf8_safe_tail_returns_bytes_copy():
    buf = bytearray(b"hello")
    out = task_runner.utf8_safe_tail(buf, limit=100)
    assert isinstance(out, bytes)
    out_from_bytes = task_runner.utf8_safe_tail(b"x" * 10, limit=5)
    assert isinstance(out_from_bytes, bytes)


# ---------------------------------------------------------------------------
# build_manifest_dict — schema shape
# ---------------------------------------------------------------------------

def _manifest_defaults():
    return dict(
        task_id="a" * 32,
        state="completed",
        exit_code=0,
        signal_name=None,
        reason=None,
        runner_token="00000000-0000-0000-0000-000000000001",
        pid=1234,
        pgid=1234,
        process_start_time_us=1_700_000_000_000_000,
        wrapper_pid=5678,
        wrapper_start_time_us=1_700_000_000_000_000,
        started_at_ms=1_700_000_000_000,
        finished_at_ms=1_700_000_000_500,
        command_argv=["echo", "hi"],
        cwd="/tmp",
        stdout_tail=b"out",
        stderr_tail=b"err",
        output_paths=["/tmp/a.txt"],
        on_done_prompt="notify me",
        chat_id="oc_123",
        session_id="sess-1",
    )


def test_build_manifest_schema_version_matches_db_contract():
    m = task_runner.build_manifest_dict(**_manifest_defaults())
    assert m["schema_version"] == 2
    assert m["schema_version"] == task_runner.MANIFEST_SCHEMA_VERSION


def test_build_manifest_has_all_required_keys():
    m = task_runner.build_manifest_dict(**_manifest_defaults())
    required = {
        "schema_version", "task_id", "state", "reason", "signal", "exit_code",
        "runner_token", "pid", "pgid", "process_start_time_us",
        "wrapper_pid", "wrapper_start_time_us",
        "started_at_ms", "finished_at_ms", "duration_seconds",
        "command_argv", "cwd",
        "stdout_tail_b64", "stderr_tail_b64",
        "output_paths", "on_done_prompt", "chat_id", "session_id",
    }
    assert required.issubset(m.keys()), f"missing: {required - m.keys()}"


def test_build_manifest_encodes_tails_as_base64():
    m = task_runner.build_manifest_dict(**_manifest_defaults())
    assert base64.b64decode(m["stdout_tail_b64"]) == b"out"
    assert base64.b64decode(m["stderr_tail_b64"]) == b"err"


def test_build_manifest_duration_seconds_rounded():
    d = _manifest_defaults()
    d["started_at_ms"] = 1000
    d["finished_at_ms"] = 2500
    m = task_runner.build_manifest_dict(**d)
    assert m["duration_seconds"] == 1.5


def test_build_manifest_duration_floored_at_zero():
    # Negative observed delta (clock drift) must not leak through.
    d = _manifest_defaults()
    d["started_at_ms"] = 2000
    d["finished_at_ms"] = 1000
    m = task_runner.build_manifest_dict(**d)
    assert m["duration_seconds"] == 0.0


# ---------------------------------------------------------------------------
# write_manifest_atomically — crash safety
# ---------------------------------------------------------------------------

def test_write_manifest_atomically_creates_target(tmp_path: Path):
    target = tmp_path / "task.json.done"
    data = {"schema_version": 2, "task_id": "deadbeef"}
    task_runner.write_manifest_atomically(target, data)
    assert target.exists()
    assert json.loads(target.read_text()) == data
    # .tmp should be gone (renamed).
    assert not target.with_suffix(".done.tmp").exists()


def test_write_manifest_atomically_rewrites_existing(tmp_path: Path):
    target = tmp_path / "task.json.done"
    target.write_text('{"old": true}')
    task_runner.write_manifest_atomically(target, {"new": True})
    assert json.loads(target.read_text()) == {"new": True}


def test_write_manifest_atomically_fsync_before_rename(tmp_path: Path, monkeypatch):
    """Guard: os.fsync must be called before os.rename.

    This encodes the crash-safety contract in the test: a power loss between
    write and fsync would corrupt the .tmp; between fsync and rename would
    leave only the prior target intact.
    """
    order: list[str] = []
    real_fsync = os.fsync
    real_rename = os.rename

    def track_fsync(fd):
        order.append("fsync")
        return real_fsync(fd)

    def track_rename(a, b):
        order.append("rename")
        return real_rename(a, b)

    monkeypatch.setattr(task_runner.os, "fsync", track_fsync)
    monkeypatch.setattr(task_runner.os, "rename", track_rename)
    task_runner.write_manifest_atomically(tmp_path / "task.json.done", {"x": 1})
    assert order == ["fsync", "rename"]


def test_write_manifest_atomically_rename_failure_leaves_tmp_untouched(
    tmp_path: Path, monkeypatch,
):
    """If rename fails the .tmp stays — recoverable on next attempt."""
    target = tmp_path / "task.json.done"

    def boom(a, b):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(task_runner.os, "rename", boom)
    with pytest.raises(OSError, match="simulated rename"):
        task_runner.write_manifest_atomically(target, {"x": 1})

    assert not target.exists()
    # .tmp written + fsynced, but not renamed.
    tmp_written = target.with_suffix(".done.tmp")
    assert tmp_written.exists()
    assert json.loads(tmp_written.read_text()) == {"x": 1}


# ---------------------------------------------------------------------------
# terminate_pgid — SIGTERM → SIGKILL flow
# ---------------------------------------------------------------------------

def _spawn_ignoring_sigterm():
    """Child that ignores SIGTERM and sleeps — forces SIGKILL path.

    Writes READY to stdout so the caller can await handler installation
    before sending SIGTERM, eliminating the startup race.
    """
    code = (
        "import signal, sys, time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "sys.stdout.write('READY\\n'); sys.stdout.flush();"
        "time.sleep(30)"
    )
    p = subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    # Block until the child confirms its handler is installed.
    line = p.stdout.readline()
    assert line.strip() == b"READY", f"unexpected readiness output: {line!r}"
    return p


def _spawn_polite_child():
    """Child that exits on SIGTERM promptly."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_terminate_pgid_sigterm_happy_path():
    p = _spawn_polite_child()
    try:
        pgid = os.getpgid(p.pid)
        start = time.monotonic()
        task_runner.terminate_pgid(pgid, grace_s=5.0)
        # Child should be reaped well within grace.
        p.wait(timeout=3.0)
        elapsed = time.monotonic() - start
        assert p.returncode is not None
        assert elapsed < 2.0, f"SIGTERM-respecting child took {elapsed:.2f}s"
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=1.0)


def test_terminate_pgid_escalates_to_sigkill():
    p = _spawn_ignoring_sigterm()
    try:
        pgid = os.getpgid(p.pid)
        start = time.monotonic()
        task_runner.terminate_pgid(pgid, grace_s=0.5)
        p.wait(timeout=3.0)
        elapsed = time.monotonic() - start
        # SIGKILL delivered after grace; total well under 3s.
        assert p.returncode == -signal.SIGKILL, f"returncode={p.returncode}"
        assert 0.4 < elapsed < 2.5, f"SIGKILL escalation timing off: {elapsed:.2f}s"
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=1.0)


def test_terminate_pgid_missing_group_is_silent():
    """ProcessLookupError on dead pgid must not propagate."""
    # Pick a pgid that certainly doesn't exist.
    task_runner.terminate_pgid(2_000_000, grace_s=0.1)


# ---------------------------------------------------------------------------
# _signal_name_from_exit_code
# ---------------------------------------------------------------------------

def test_signal_name_from_exit_code_known_signal():
    assert task_runner._signal_name_from_exit_code(-signal.SIGTERM) == "SIGTERM"
    assert task_runner._signal_name_from_exit_code(-signal.SIGKILL) == "SIGKILL"


def test_signal_name_from_exit_code_unknown_signal():
    # Use a signal number Python doesn't recognise.
    out = task_runner._signal_name_from_exit_code(-999)
    assert out == "SIG999"


# ---------------------------------------------------------------------------
# _StreamCollector — pipe reader retention + log write
# ---------------------------------------------------------------------------

def test_stream_collector_caps_window_and_writes_log(tmp_path: Path):
    r, w = os.pipe()
    rf = os.fdopen(r, "rb")
    log_path = tmp_path / "stdout.log"
    c = task_runner._StreamCollector(rf, log_path, "stdout")
    c.start()

    # Write > 8192 bytes to force window trimming.
    payload = b"A" * 10_000
    os.write(w, payload)
    os.close(w)
    c.join(timeout=2.0)

    assert log_path.read_bytes() == payload, "log file must capture full stream"
    tail = c.tail_bytes()
    # Window trimmed to 8192 → tail output to 4096.
    assert len(tail) <= 4096


# ---------------------------------------------------------------------------
# _nudge — best-effort UDS datagram
# ---------------------------------------------------------------------------

def test_nudge_is_silent_when_sock_missing(tmp_path: Path):
    # No wake.sock in bridge_home.
    task_runner._nudge(tmp_path, uuid.uuid4().hex)


def test_nudge_sends_frame_when_sock_present(tmp_path: Path):
    # macOS AF_UNIX sun_path is capped at 104 bytes; pytest's tmp_path is too
    # long, so bind from a short /tmp/<uuid>/ dir instead.
    import shutil
    short_dir = Path("/tmp") / f"fb-nudge-{uuid.uuid4().hex[:8]}"
    short_dir.mkdir()
    try:
        sock_path = short_dir / "wake.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            server.bind(str(sock_path))
            server.settimeout(2.0)

            task_id_hex = uuid.uuid4().hex
            task_runner._nudge(short_dir, task_id_hex)

            data, _ = server.recvfrom(64)
            assert data[:1] == b"\x03"
            assert data[1:] == uuid.UUID(hex=task_id_hex).bytes
        finally:
            server.close()
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# End-to-end: drive main() against real DB + real short-lived child.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS-only")
def test_main_happy_path_completes_and_writes_manifest(tmp_path: Path):
    db_path = tmp_path / "bg.db"
    tasks_dir = tmp_path / "tasks"

    # Bridge-side CAS-claim simulation: insert queued → flip to launching.
    conn = init_db(db_path)
    repo = BgTaskRepo(conn)
    task_id = repo.insert_task(
        chat_id="oc_test",
        session_id="sess-test",
        command_argv=[sys.executable, "-c", "import sys; sys.stdout.write('hello'); sys.exit(0)"],
        on_done_prompt="done",
        timeout_seconds=30,
    )
    assert repo.claim_queued_cas(task_id, bridge_instance_id="test-bridge")
    conn.close()

    runner_token = str(uuid.uuid4())
    rc = task_runner.main([
        "--task-id", task_id,
        "--db-path", str(db_path),
        "--tasks-dir", str(tasks_dir),
        "--runner-token", runner_token,
    ])
    assert rc == 0

    # Post-conditions: DB row completed, manifest written to completed/.
    conn = init_db(db_path)
    repo = BgTaskRepo(conn)
    row = repo.get(task_id)
    assert row is not None
    assert row.state == TaskState.COMPLETED.value
    conn.close()

    completed_dir = tasks_dir / "completed" / task_id
    manifest_path = completed_dir / "task.json.done"
    assert manifest_path.exists()
    data = json.loads(manifest_path.read_text())
    assert data["schema_version"] == 2
    assert data["state"] == "completed"
    assert data["exit_code"] == 0
    assert data["runner_token"] == runner_token
    assert data["pid"] is not None
    assert data["pgid"] is not None
    assert data["process_start_time_us"] is not None
    assert base64.b64decode(data["stdout_tail_b64"]) == b"hello"


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS-only")
def test_main_cancel_mid_flight_produces_cancelled_state(tmp_path: Path):
    db_path = tmp_path / "bg.db"
    tasks_dir = tmp_path / "tasks"

    conn = init_db(db_path)
    repo = BgTaskRepo(conn)
    task_id = repo.insert_task(
        chat_id="oc_test",
        session_id="sess-test",
        command_argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        on_done_prompt="done",
        timeout_seconds=30,
    )
    assert repo.claim_queued_cas(task_id, bridge_instance_id="test-bridge")
    conn.close()

    # Fire wrapper in a thread, cancel shortly after.
    runner_token = str(uuid.uuid4())
    rc_holder: dict = {}

    def run_wrapper():
        rc_holder["rc"] = task_runner.main([
            "--task-id", task_id,
            "--db-path", str(db_path),
            "--tasks-dir", str(tasks_dir),
            "--runner-token", runner_token,
        ])

    t = threading.Thread(target=run_wrapper, daemon=True)
    t.start()

    # Wait for the wrapper to flip the row to running, then issue cancel.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        conn = init_db(db_path)
        r = BgTaskRepo(conn).get(task_id)
        conn.close()
        if r is not None and r.state == TaskState.RUNNING.value:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("wrapper never flipped state to running")

    conn = init_db(db_path)
    BgTaskRepo(conn).set_cancel_requested(task_id)
    conn.close()

    t.join(timeout=15.0)
    assert not t.is_alive(), "wrapper thread hung"
    assert rc_holder.get("rc") == 0

    conn = init_db(db_path)
    row = BgTaskRepo(conn).get(task_id)
    conn.close()
    assert row is not None
    assert row.state == TaskState.CANCELLED.value

    manifest_path = tasks_dir / "completed" / task_id / "task.json.done"
    assert manifest_path.exists()
    data = json.loads(manifest_path.read_text())
    assert data["state"] == "cancelled"
    assert data["reason"] == "cancelled"
    assert data["signal"] in ("SIGTERM", "SIGKILL")


# ---------------------------------------------------------------------------
# Error-path coverage (spec-check S1): non-zero exit, timeout, phase_p guard.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS-only")
def test_main_non_zero_exit_produces_failed_state(tmp_path: Path):
    db_path = tmp_path / "bg.db"
    tasks_dir = tmp_path / "tasks"

    conn = init_db(db_path)
    repo = BgTaskRepo(conn)
    task_id = repo.insert_task(
        chat_id="oc_test",
        session_id="sess-test",
        command_argv=[sys.executable, "-c", "import sys; sys.exit(7)"],
        on_done_prompt="done",
        timeout_seconds=30,
    )
    assert repo.claim_queued_cas(task_id, bridge_instance_id="test-bridge")
    conn.close()

    rc = task_runner.main([
        "--task-id", task_id,
        "--db-path", str(db_path),
        "--tasks-dir", str(tasks_dir),
        "--runner-token", str(uuid.uuid4()),
    ])
    assert rc == 0

    conn = init_db(db_path)
    row = BgTaskRepo(conn).get(task_id)
    conn.close()
    assert row is not None
    assert row.state == TaskState.FAILED.value

    manifest = json.loads(
        (tasks_dir / "completed" / task_id / "task.json.done").read_text()
    )
    assert manifest["state"] == "failed"
    assert manifest["exit_code"] == 7
    assert manifest["reason"] == "exit_code=7"
    assert manifest["signal"] is None


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS-only")
def test_main_timeout_produces_timeout_state(tmp_path: Path):
    db_path = tmp_path / "bg.db"
    tasks_dir = tmp_path / "tasks"

    conn = init_db(db_path)
    repo = BgTaskRepo(conn)
    task_id = repo.insert_task(
        chat_id="oc_test",
        session_id="sess-test",
        command_argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        on_done_prompt="done",
        timeout_seconds=1,  # force timeout path
    )
    assert repo.claim_queued_cas(task_id, bridge_instance_id="test-bridge")
    conn.close()

    rc = task_runner.main([
        "--task-id", task_id,
        "--db-path", str(db_path),
        "--tasks-dir", str(tasks_dir),
        "--runner-token", str(uuid.uuid4()),
    ])
    assert rc == 0

    conn = init_db(db_path)
    row = BgTaskRepo(conn).get(task_id)
    conn.close()
    assert row is not None
    assert row.state == TaskState.TIMEOUT.value

    manifest = json.loads(
        (tasks_dir / "completed" / task_id / "task.json.done").read_text()
    )
    assert manifest["state"] == "timeout"
    assert manifest["reason"] == "timeout"
    assert manifest["signal"] in ("SIGTERM", "SIGKILL")


def test_main_rejects_task_not_in_launching_state(tmp_path: Path):
    """phase_p must refuse to run for a task still in 'queued'."""
    db_path = tmp_path / "bg.db"
    tasks_dir = tmp_path / "tasks"

    conn = init_db(db_path)
    repo = BgTaskRepo(conn)
    task_id = repo.insert_task(
        chat_id="oc_test",
        session_id="sess-test",
        command_argv=[sys.executable, "-c", "pass"],
        on_done_prompt="done",
        timeout_seconds=30,
    )
    # Intentionally DO NOT claim_queued_cas — row stays in 'queued'.
    row = repo.get(task_id)
    assert row is not None and row.state == "queued"
    conn.close()

    rc = task_runner.main([
        "--task-id", task_id,
        "--db-path", str(db_path),
        "--tasks-dir", str(tasks_dir),
        "--runner-token", str(uuid.uuid4()),
    ])
    # phase_p raises before Popen → main returns 1; no manifest, no child.
    assert rc == 1
    assert not (tasks_dir / "active" / task_id).exists() or \
        not (tasks_dir / "active" / task_id / "task.json.done").exists()
    assert not (tasks_dir / "completed" / task_id).exists()


def test_main_rejects_invalid_task_id():
    rc = task_runner.main([
        "--task-id", "not-a-hex",
        "--db-path", "/tmp/does-not-matter.db",
        "--tasks-dir", "/tmp/does-not-matter",
        "--runner-token", str(uuid.uuid4()),
    ])
    assert rc == 2


def test_main_rejects_invalid_runner_token():
    rc = task_runner.main([
        "--task-id", "a" * 32,
        "--db-path", "/tmp/does-not-matter.db",
        "--tasks-dir", "/tmp/does-not-matter",
        "--runner-token", "not-a-uuid",
    ])
    assert rc == 2


def test_phase_s_logs_redact_argv(tmp_path: Path, caplog):
    """argv at INFO level must only show argv[0] + length (no token leak)."""
    import logging as _logging

    db_path = tmp_path / "bg.db"
    tasks_dir = tmp_path / "tasks"

    conn = init_db(db_path)
    repo = BgTaskRepo(conn)
    task_id = repo.insert_task(
        chat_id="oc_test",
        session_id="sess-test",
        command_argv=[
            sys.executable, "-c",
            "import sys; sys.stdout.write('ok'); sys.exit(0)",
            "--secret-token=SUPERSECRETDONOTLOG",
        ],
        on_done_prompt="done",
        timeout_seconds=30,
    )
    assert repo.claim_queued_cas(task_id, bridge_instance_id="test-bridge")
    conn.close()

    with caplog.at_level(_logging.INFO, logger="feishu-bridge.task-runner"):
        rc = task_runner.main([
            "--task-id", task_id,
            "--db-path", str(db_path),
            "--tasks-dir", str(tasks_dir),
            "--runner-token", str(uuid.uuid4()),
        ])
    assert rc == 0

    info_records = [r.getMessage() for r in caplog.records if r.levelno == _logging.INFO]
    for msg in info_records:
        assert "SUPERSECRETDONOTLOG" not in msg, (
            f"secret leaked into INFO log: {msg!r}"
        )
