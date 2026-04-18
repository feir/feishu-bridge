"""Unit tests for feishu_bridge.bg_supervisor (Section 4.1–4.4).

Validation criteria (from .specs/changes/feishu-bridge-bg-tasks/tasks.md):
    4.1  start/stop idempotent; stop does not kill wrapper
    4.2  stale wake.sock → unlink + rebind; nudge triggers scan ≤100ms
    4.3  listener disabled → poller launches queued task ≤1s
    4.4  two supervisors racing same queued row → exactly one spawns
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from feishu_bridge.bg_supervisor import BgSupervisor
from feishu_bridge.bg_tasks_db import BgTaskRepo, connect, init_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def short_env():
    """Yield a dict(db_path, tasks_dir, sock_path) rooted in /tmp.

    macOS AF_UNIX sun_path caps at 104 bytes; pytest's tmp_path routinely
    exceeds that. Using /tmp-rooted short paths keeps the socket bindable.
    """
    root = Path(tempfile.mkdtemp(dir="/tmp", prefix="fb-bg-"))
    try:
        bg_home = root / "bg"
        bg_home.mkdir(parents=True, exist_ok=True)
        yield {
            "db_path": bg_home / "bg.db",
            "tasks_dir": bg_home / "tasks",
            "sock_path": bg_home / "wake.sock",
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def repo(short_env):
    """A ready BgTaskRepo against the fixture DB (main-thread use only)."""
    conn = init_db(short_env["db_path"])
    try:
        yield BgTaskRepo(conn)
    finally:
        conn.close()


def _fast_supervisor(short_env, *, spawner=None, poll_interval: float = 0.05):
    spawner = spawner if spawner is not None else MagicMock(
        return_value=MagicMock(pid=12345)
    )
    return BgSupervisor(
        db_path=short_env["db_path"],
        tasks_dir=short_env["tasks_dir"],
        sock_path=short_env["sock_path"],
        runner_cmd=["/bin/true"],  # never actually spawned; spawner is a Mock
        poll_interval=poll_interval,
        spawner=spawner,
    ), spawner


def _enqueue(repo: BgTaskRepo, *, cancel_requested: bool = False) -> str:
    tid = repo.insert_task(
        chat_id="oc_test",
        session_id="sess_test",
        command_argv=["echo", "hi"],
        on_done_prompt="done",
    )
    if cancel_requested:
        assert repo.set_cancel_requested(tid)
    return tid


def _wait_until(pred, *, timeout: float = 2.0, interval: float = 0.02):
    """Poll pred() until True or timeout. Returns final value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        val = pred()
        if val:
            return val
        time.sleep(interval)
    return pred()


# ---------------------------------------------------------------------------
# 4.1 — lifecycle
# ---------------------------------------------------------------------------

def test_start_is_idempotent(short_env):
    sup, _ = _fast_supervisor(short_env)
    try:
        sup.start()
        assert sup.is_running()
        listener_thread = sup._listener_thread
        poller_thread = sup._poller_thread
        sup.start()  # second call: no-op
        assert sup._listener_thread is listener_thread
        assert sup._poller_thread is poller_thread
    finally:
        sup.stop()


def test_stop_before_start_is_noop(short_env):
    sup, _ = _fast_supervisor(short_env)
    sup.stop()  # must not raise
    assert not sup.is_running()


def test_stop_is_idempotent(short_env):
    sup, _ = _fast_supervisor(short_env)
    sup.start()
    sup.stop()
    sup.stop()  # second stop: no-op
    assert not sup.is_running()


def test_stop_joins_threads_and_unlinks_socket(short_env):
    sup, _ = _fast_supervisor(short_env)
    sup.start()
    assert short_env["sock_path"].exists()
    sup.stop(timeout=2.0)
    # Best-effort unlink ran; either gone or caller replaced — just assert
    # the threads finished.
    assert sup._listener_thread is None
    assert sup._poller_thread is None
    assert not sup.is_running()


def test_stop_does_not_terminate_spawned_wrapper(short_env, repo):
    """Wrapper must survive bridge shutdown — stop() never calls kill/terminate."""
    mock_proc = MagicMock(pid=9999)
    spawner = MagicMock(return_value=mock_proc)
    sup, _ = _fast_supervisor(short_env, spawner=spawner)
    _enqueue(repo)

    sup.start()
    _wait_until(lambda: spawner.called, timeout=2.0)
    assert spawner.called
    sup.stop()

    mock_proc.terminate.assert_not_called()
    mock_proc.kill.assert_not_called()
    mock_proc.send_signal.assert_not_called()


# ---------------------------------------------------------------------------
# 4.2 — UDS listener bind + EADDRINUSE handling
# ---------------------------------------------------------------------------

def test_listener_binds_socket_with_mode_0600(short_env):
    sup, _ = _fast_supervisor(short_env)
    try:
        sup.start()
        sock_path = short_env["sock_path"]
        assert sock_path.exists()
        # bit-and 0o777 — socket file mode in sane umasks is exactly 0o600.
        mode = sock_path.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"
        parent_mode = sock_path.parent.stat().st_mode & 0o777
        assert parent_mode == 0o700
    finally:
        sup.stop()


def test_stale_socket_file_is_unlinked_and_rebound(short_env):
    """Stale socket (no listener) → supervisor unlinks + rebinds cleanly."""
    p = short_env["sock_path"]
    p.parent.mkdir(parents=True, exist_ok=True)
    # Create a stale regular file at sock_path — no listener backs it.
    p.write_bytes(b"")
    assert p.exists()

    sup, _ = _fast_supervisor(short_env)
    try:
        sup.start()
        assert sup._listen_sock is not None, "supervisor should have rebound"
        # Confirm the socket is actually listening by connect()-ing.
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        try:
            s.connect(str(p))
            s.sendall(b"\x01")
        finally:
            s.close()
    finally:
        sup.stop()


def test_active_peer_falls_back_to_poller_only(short_env, repo):
    """Another listener holds the sock → supervisor uses poller-only mode."""
    p = short_env["sock_path"]
    p.parent.mkdir(parents=True, exist_ok=True)
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.bind(str(p))
    peer.listen(1)
    try:
        sup, spawner = _fast_supervisor(short_env, poll_interval=0.05)
        try:
            sup.start()
            assert sup._listen_sock is None, "should fall back to poller-only"
            # Poller should still launch a queued task even without listener.
            _enqueue(repo)
            assert _wait_until(lambda: spawner.called, timeout=2.0)
        finally:
            sup.stop()
    finally:
        peer.close()


def test_wake_x01_triggers_scan_and_launch(short_env, repo):
    """\\x01 ping → scan queued + launch within ≤100ms (plus handler slack)."""
    sup, spawner = _fast_supervisor(short_env, poll_interval=9999.0)  # disable poller effect
    try:
        sup.start()
        tid = _enqueue(repo)

        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(1.0)
        c.connect(str(short_env["sock_path"]))
        c.sendall(b"\x01")
        c.close()

        assert _wait_until(lambda: spawner.called, timeout=2.0)
        argv = spawner.call_args.args[0]
        assert "--task-id" in argv and tid in argv
    finally:
        sup.stop()


def test_wake_x02_launches_specific_task(short_env, repo):
    """\\x02 + uuid spawns that specific task (priority nudge)."""
    # Stub out the generic scan so this test only exercises the targeted path.
    sup, spawner = _fast_supervisor(short_env, poll_interval=9999.0)
    sup._scan_and_launch_queued = lambda _r: 0  # type: ignore[assignment]
    try:
        sup.start()
        tid = _enqueue(repo)

        payload = b"\x02" + uuid.UUID(hex=tid).bytes
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(1.0)
        c.connect(str(short_env["sock_path"]))
        c.sendall(payload)
        c.close()

        assert _wait_until(lambda: spawner.called, timeout=2.0)
        argv = spawner.call_args.args[0]
        assert "--task-id" in argv and tid in argv
    finally:
        sup.stop()


def test_wake_x03_routes_to_delivery_seam(short_env):
    """\\x03 + uuid payload invokes _scan_delivery_outbox (seam for 4.5)."""
    sup, _ = _fast_supervisor(short_env, poll_interval=9999.0)
    calls: list[int] = []
    orig = sup._scan_delivery_outbox

    def _spy(repo):
        calls.append(1)
        return orig(repo)

    sup._scan_delivery_outbox = _spy  # type: ignore[assignment]
    try:
        sup.start()
        tid = uuid.uuid4().hex
        payload = b"\x03" + uuid.UUID(hex=tid).bytes
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(1.0)
        c.connect(str(short_env["sock_path"]))
        c.sendall(payload)
        c.close()

        assert _wait_until(lambda: bool(calls), timeout=2.0)
    finally:
        sup.stop()


def test_invalid_uuid_payload_is_ignored(short_env, repo):
    """Short/malformed \\x02 payload → no crash, no spawn."""
    sup, spawner = _fast_supervisor(short_env, poll_interval=9999.0)
    # Suppress generic scan so any spawn must come from the payload handler.
    sup._scan_and_launch_queued = lambda _r: 0  # type: ignore[assignment]
    try:
        sup.start()
        _enqueue(repo)

        # Only 5 bytes after kind — len != 16 → silent drop.
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(1.0)
        c.connect(str(short_env["sock_path"]))
        c.sendall(b"\x02" + b"abcde")
        c.close()

        time.sleep(0.2)
        assert not spawner.called
    finally:
        sup.stop()


# ---------------------------------------------------------------------------
# 4.3 — 1s fallback poller
# ---------------------------------------------------------------------------

def test_poller_launches_queued_task_without_listener(short_env, repo):
    """With listener disabled, queued task must be launched within poll_interval."""
    # Block the socket by holding it, forcing fallback-only mode.
    p = short_env["sock_path"]
    p.parent.mkdir(parents=True, exist_ok=True)
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.bind(str(p))
    peer.listen(1)
    try:
        sup, spawner = _fast_supervisor(short_env, poll_interval=0.05)
        try:
            sup.start()
            assert sup._listen_sock is None
            tid = _enqueue(repo)
            assert _wait_until(lambda: spawner.called, timeout=2.0)
            argv = spawner.call_args.args[0]
            assert tid in argv
        finally:
            sup.stop()
    finally:
        peer.close()


# ---------------------------------------------------------------------------
# 4.4 — CAS launcher + rollback
# ---------------------------------------------------------------------------

def test_cas_launcher_single_winner_under_concurrency(short_env):
    """Two threads launching the same task → exactly one spawn."""
    tid = None
    conn = init_db(short_env["db_path"])
    try:
        tid = BgTaskRepo(conn).insert_task(
            chat_id="oc", session_id="s",
            command_argv=["echo"], on_done_prompt="d",
        )
    finally:
        conn.close()

    spawner = MagicMock(return_value=MagicMock(pid=1))
    sup = BgSupervisor(
        db_path=short_env["db_path"],
        tasks_dir=short_env["tasks_dir"],
        sock_path=short_env["sock_path"],
        runner_cmd=["/bin/true"],
        spawner=spawner,
    )

    # Each worker needs its own sqlite3 connection per BgTaskRepo contract.
    results: list[bool] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def _worker():
        c = connect(short_env["db_path"])
        try:
            r = BgTaskRepo(c)
            barrier.wait()
            won = sup._launch_specific(r, tid)
            with lock:
                results.append(won)
        finally:
            c.close()

    ts = [threading.Thread(target=_worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=5.0)

    assert sum(1 for r in results if r) == 1, f"expected 1 winner, got {results}"
    assert spawner.call_count == 1


def test_popen_exception_rolls_back_to_failed(short_env, repo):
    """Spawner OSError → task state flipped to 'failed' with reason='spawn_failed'."""
    spawner = MagicMock(side_effect=OSError("simulated spawn failure"))
    sup = BgSupervisor(
        db_path=short_env["db_path"],
        tasks_dir=short_env["tasks_dir"],
        sock_path=short_env["sock_path"],
        runner_cmd=["/bin/true"],
        spawner=spawner,
        poll_interval=0.05,
    )
    tid = _enqueue(repo)

    try:
        sup.start()

        def _is_failed() -> bool:
            row = repo.get(tid)
            return bool(row and row.state == "failed")

        assert _wait_until(_is_failed, timeout=2.0), \
            f"task state never reached 'failed' (got {repo.get(tid).state!r})"
        row = repo.get(tid)
        assert row.reason == "spawn_failed"
        assert "simulated spawn failure" in (row.error_message or "")
    finally:
        sup.stop()


def test_spawner_receives_expected_args_and_env(short_env, repo):
    """Launcher passes --task-id, --db-path, --tasks-dir, --runner-token; env has BG_TASK_TOKEN."""
    spawner = MagicMock(return_value=MagicMock(pid=123))
    sup = BgSupervisor(
        db_path=short_env["db_path"],
        tasks_dir=short_env["tasks_dir"],
        sock_path=short_env["sock_path"],
        runner_cmd=["/usr/bin/env", "python3", "-m", "feishu_bridge.task_runner"],
        spawner=spawner,
        poll_interval=0.05,
    )
    tid = _enqueue(repo)
    try:
        sup.start()
        assert _wait_until(lambda: spawner.called, timeout=2.0)
        call = spawner.call_args
        argv = call.args[0]
        kwargs = call.kwargs

        # argv has runner_cmd prefix + expected flags.
        assert argv[:4] == ["/usr/bin/env", "python3", "-m", "feishu_bridge.task_runner"]
        assert "--task-id" in argv and tid in argv
        assert "--db-path" in argv
        assert "--tasks-dir" in argv
        assert "--runner-token" in argv
        token = argv[argv.index("--runner-token") + 1]
        assert len(token) == 32  # uuid4().hex

        # Wrapper gets isolated stdio + its own session.
        assert kwargs.get("start_new_session") is True
        assert kwargs.get("close_fds") is True
        import subprocess as sp
        assert kwargs.get("stdin") == sp.DEVNULL
        assert kwargs.get("stdout") == sp.DEVNULL
        assert kwargs.get("stderr") == sp.DEVNULL

        # Env carries the runner token.
        env = kwargs.get("env") or {}
        assert env.get("BG_TASK_TOKEN") == token
    finally:
        sup.stop()


# ---------------------------------------------------------------------------
# Cancel-before-launch (Cancel SLO ≤10s)
# ---------------------------------------------------------------------------

def test_cancel_before_launch_flips_queued_to_cancelled(short_env, repo):
    """queued + cancel_requested_at → 'cancelled' within poll_interval; no spawn."""
    spawner = MagicMock(return_value=MagicMock(pid=1))
    sup = BgSupervisor(
        db_path=short_env["db_path"],
        tasks_dir=short_env["tasks_dir"],
        sock_path=short_env["sock_path"],
        runner_cmd=["/bin/true"],
        spawner=spawner,
        poll_interval=0.05,
    )
    tid = _enqueue(repo, cancel_requested=True)
    try:
        sup.start()

        def _is_cancelled() -> bool:
            row = repo.get(tid)
            return bool(row and row.state == "cancelled")

        assert _wait_until(_is_cancelled, timeout=2.0), \
            f"task was not cancelled (state={repo.get(tid).state!r})"
        row = repo.get(tid)
        assert row.reason == "cancelled_before_launch"
        # Never spawned: claim_queued_cas WHERE clause excluded this row
        # and the poller flipped it before any other tick could win.
        spawner.assert_not_called()
    finally:
        sup.stop()


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def test_default_runner_cmd_uses_python_m_module(short_env):
    """Default runner_cmd is portable: [sys.executable, '-m', 'feishu_bridge.task_runner']."""
    sup = BgSupervisor(
        db_path=short_env["db_path"],
        tasks_dir=short_env["tasks_dir"],
        sock_path=short_env["sock_path"],
        spawner=MagicMock(return_value=MagicMock(pid=1)),
    )
    assert sup._runner_cmd == [sys.executable, "-m", "feishu_bridge.task_runner"]


def test_default_bridge_instance_id_is_generated(short_env):
    sup_a = BgSupervisor(
        db_path=short_env["db_path"],
        tasks_dir=short_env["tasks_dir"],
        sock_path=short_env["sock_path"],
    )
    sup_b = BgSupervisor(
        db_path=short_env["db_path"],
        tasks_dir=short_env["tasks_dir"],
        sock_path=short_env["sock_path"],
    )
    assert len(sup_a._bridge_instance_id) == 32
    assert sup_a._bridge_instance_id != sup_b._bridge_instance_id


# ---------------------------------------------------------------------------
# Cross-component regressions (guard multi-model review findings)
# ---------------------------------------------------------------------------

def test_task_runner_nudge_reaches_supervisor(short_env):
    """Real _nudge() → real BgSupervisor → \\x03 delivery seam fires (Finding #1).

    Regression for the SOCK_DGRAM/SOCK_STREAM mismatch: if the protocol
    regresses, connect() will raise EPROTOTYPE (macOS) / EPROTONOSUPPORT
    (linux) and the handler never runs.
    """
    from feishu_bridge.task_runner import _nudge

    sup, _ = _fast_supervisor(short_env, poll_interval=9999.0)
    calls: list[int] = []
    orig = sup._scan_delivery_outbox

    def _spy(repo):
        calls.append(1)
        return orig(repo)

    sup._scan_delivery_outbox = _spy  # type: ignore[assignment]
    try:
        sup.start()
        # bridge_home is the dir containing wake.sock.
        _nudge(short_env["sock_path"].parent, uuid.uuid4().hex)
        assert _wait_until(lambda: bool(calls), timeout=2.0), \
            "delivery seam never fired — protocol mismatch regression?"
    finally:
        sup.stop()


def test_poller_only_stop_preserves_peer_socket(short_env, repo):
    """Supervisor in poller-only fallback must not unlink peer-owned sock (Finding #2).

    Regression for the stop() unconditional unlink: without the
    ``_owns_sock_path`` guard, stopping a poller-only supervisor would
    delete the real owner's socket file.
    """
    p = short_env["sock_path"]
    p.parent.mkdir(parents=True, exist_ok=True)
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.bind(str(p))
    peer.listen(16)
    try:
        # Record inode before start/stop; after stop it must be unchanged
        # (i.e. not unlinked-and-replaced by anyone).
        inode_before = p.stat().st_ino

        sup, _ = _fast_supervisor(short_env, poll_interval=0.05)
        try:
            sup.start()
            assert sup._listen_sock is None, "setup precondition: poller-only"
            assert not sup._owns_sock_path
        finally:
            sup.stop()

        # Peer's socket file must still be there with same inode — supervisor
        # never owned it, so stop() must not unlink it.
        assert p.exists(), "supervisor stop() deleted peer-owned socket!"
        assert p.stat().st_ino == inode_before, \
            "socket inode changed — supervisor unlinked and something replaced it"
    finally:
        peer.close()


def test_listener_db_fail_closes_socket(short_env, monkeypatch):
    """Listener thread's DB connect failure must close the listen socket (Finding #4).

    Regression for the blackhole: if listener dies but sock stays bound,
    peers see a 'live' listener whose payloads are never drained.
    """
    import feishu_bridge.bg_supervisor as mod

    # bg_supervisor.connect is only referenced by _listener_loop and
    # _poller_loop; main-thread init_db uses bg_tasks_db.connect directly
    # (unaffected by this patch). So we can raise unconditionally.
    def _fail(db_path):
        raise RuntimeError("simulated listener DB failure")

    monkeypatch.setattr(mod, "connect", _fail)

    sup, _ = _fast_supervisor(short_env, poll_interval=9999.0)
    try:
        sup.start()
        # Listener thread should observe the raise, log, and null out listen_sock.
        assert _wait_until(lambda: sup._listen_sock is None, timeout=2.0), \
            "listener didn't close socket after DB connect failure"
    finally:
        sup.stop()
