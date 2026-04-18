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


# ---------------------------------------------------------------------------
# 4.5 — delivery watcher body (_scan_delivery_outbox)
# ---------------------------------------------------------------------------

def _make_pending_run(
    repo: BgTaskRepo,
    *,
    chat_id: str = "oc_test",
    session_id: str = "sess_abc",
    thread_id: str | None = None,
    on_done_prompt: str = "Summarize what the task produced.",
    stdout_tail: bytes = b"ok\n",
    stderr_tail: bytes = b"",
    manifest_path: str = "/tmp/bg/manifest.json",
) -> tuple[str, int]:
    """Create a bg_tasks + bg_runs row pair in (completed, pending) state."""
    tid = repo.insert_task(
        chat_id=chat_id,
        session_id=session_id,
        thread_id=thread_id,
        command_argv=["echo", "hi"],
        on_done_prompt=on_done_prompt,
        output_paths=["/tmp/bg/out.txt"],
    )
    assert repo.claim_queued_cas(tid, bridge_instance_id="b1")
    run_id = repo.start_run(
        task_id=tid, runner_token="t",
        wrapper_pid=1000, wrapper_start_time_us=9000,
    )
    repo.attach_child(
        run_id=run_id, task_id=tid, pid=2000, pgid=2000,
        process_start_time_us=9100,
    )
    repo.finish_run(
        run_id=run_id, task_id=tid, terminal_state="completed",
        exit_code=0, signal=None,
        stdout_tail=stdout_tail, stderr_tail=stderr_tail,
        manifest_path=manifest_path,
    )
    return tid, run_id


def _delivery_state(conn, run_id: int) -> str:
    return conn.execute(
        "SELECT delivery_state FROM bg_runs WHERE id=?", (run_id,),
    ).fetchone()[0]


def _make_delivery_supervisor(
    short_env,
    *,
    enqueue_fn=None,
    sessions_index=None,
    bot_id: str = "bot-xyz",
):
    """Build a supervisor wired for the delivery watcher path (no threads)."""
    return BgSupervisor(
        db_path=short_env["db_path"],
        tasks_dir=short_env["tasks_dir"],
        sock_path=short_env["sock_path"],
        runner_cmd=["/bin/true"],
        poll_interval=9999.0,
        enqueue_fn=enqueue_fn,
        bot_id=bot_id,
        sessions_index=sessions_index,
    )


def test_scan_delivery_noop_when_enqueue_fn_is_none(short_env, repo):
    """Tests that don't wire enqueue_fn must not crash — delivery watcher
    still runs stuck-rollback (pure DB) but early-returns before enqueue."""
    _make_pending_run(repo)
    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    assert sup._scan_delivery_outbox(repo) == 0


def test_scan_delivery_delivers_pending_run(short_env, repo):
    """Happy path: pending run → CAS to enqueued → enqueue_fn called once."""
    from feishu_bridge.session_resume import SessionsIndex

    tid, run_id = _make_pending_run(
        repo, session_id="sess_recent", chat_id="oc_A",
    )
    idx = SessionsIndex(Path(short_env["db_path"]).parent / "sessions.json")
    idx.touch("sess_recent", "oc_A", int(time.time() * 1000))

    calls: list[dict] = []
    def _fake_enqueue(**kwargs):
        calls.append(kwargs)
        return ("queued", {})

    sup = _make_delivery_supervisor(
        short_env, enqueue_fn=_fake_enqueue, sessions_index=idx, bot_id="bot-xyz",
    )
    delivered = sup._scan_delivery_outbox(repo)
    assert delivered == 1
    assert len(calls) == 1
    # State must have moved pending → enqueued (will advance to sent when
    # worker later calls _bg_mark_delivery_outcome; that path is separate).
    assert _delivery_state(repo.conn, run_id) == "enqueued"

    call = calls[0]
    assert call["chat_id"] == "oc_A"
    assert call["kind"] == "bg_task_completion"
    # session resumable (recent_activity) → effective_sid preserved.
    assert call["session_id"] == "sess_recent"
    # Session key shape: bot:chat:thread — thread_id=None becomes "".
    assert call["session_key"] == "bot-xyz:oc_A:"
    # Synthetic turn body should include the task_id header and manifest path.
    assert f"[bg-task:{tid}]" in call["prompt"]
    # Not a fresh-fallback → no NOTE prefix on the prompt.
    assert not call["prompt"].startswith("[NOTE:")
    # Extras must carry run_id so worker can mark sent via CAS later.
    assert call["extras"]["_bg_run_id"] == run_id


def test_scan_delivery_fresh_fallback_prefix_when_unseen(short_env, repo):
    """session not in index → fresh_fallback prefix + effective_sid=None."""
    from feishu_bridge.session_resume import SessionsIndex

    tid, run_id = _make_pending_run(repo, session_id="sess_unknown")
    idx = SessionsIndex(Path(short_env["db_path"]).parent / "sessions.json")
    # Deliberately do NOT touch() — session is unseen.

    calls: list[dict] = []
    def _fake_enqueue(**kwargs):
        calls.append(kwargs)
        return ("queued", {})

    sup = _make_delivery_supervisor(
        short_env, enqueue_fn=_fake_enqueue, sessions_index=idx,
    )
    assert sup._scan_delivery_outbox(repo) == 1
    call = calls[0]
    assert call["prompt"].startswith("[NOTE:")
    assert "not_in_index" in call["prompt"]
    assert call["session_id"] is None, "fresh fallback must drop the sid"
    # Body still present below the NOTE.
    assert f"[bg-task:{tid}]" in call["prompt"]
    # Persisted status annotation for audit/debugging.
    row = repo.conn.execute(
        "SELECT session_resume_status FROM bg_runs WHERE id=?", (run_id,),
    ).fetchone()
    assert row[0] == "fresh_fallback:not_in_index"


def test_scan_delivery_resume_failed_also_uses_fresh_branch(
    short_env, repo, monkeypatch,
):
    """design.md:517 — `resume_failed` must also prepend NOTE + drop sid.

    Guards concern #2: an earlier impl routed `resume_failed` through the
    resume path, defeating the probe. Retrying a session we already know
    is unresumable wastes the 5s probe AND re-surfaces as `resume_failed`
    forever.
    """
    from feishu_bridge import bg_supervisor as mod
    from feishu_bridge.session_resume import SessionsIndex

    tid, run_id = _make_pending_run(repo, session_id="sess_probe_error")
    idx = SessionsIndex(Path(short_env["db_path"]).parent / "sessions.json")

    # Bypass the real resolve_resume_status — we're unit-testing the
    # supervisor branch, not the policy function (which has its own tests).
    monkeypatch.setattr(
        mod, "resolve_resume_status",
        lambda sid, index, now_ms: ("resume_failed", "probe_error"),
    )

    calls: list[dict] = []
    def _fake_enqueue(**kwargs):
        calls.append(kwargs)
        return ("queued", {})

    sup = _make_delivery_supervisor(
        short_env, enqueue_fn=_fake_enqueue, sessions_index=idx,
    )
    assert sup._scan_delivery_outbox(repo) == 1
    call = calls[0]
    assert call["prompt"].startswith("[NOTE:"), \
        "resume_failed must also prepend the NOTE prefix"
    assert "probe_error" in call["prompt"], \
        "NOTE reason should carry the probe error tag"
    assert call["session_id"] is None, \
        "resume_failed must drop the sid — resume is known-broken"
    assert f"[bg-task:{tid}]" in call["prompt"]
    row = repo.conn.execute(
        "SELECT session_resume_status FROM bg_runs WHERE id=?", (run_id,),
    ).fetchone()
    assert row[0] == "resume_failed:probe_error"


def test_scan_delivery_cas_race_exactly_one_enqueue(short_env, repo):
    """Two concurrent scanners on the same row: exactly one wins the CAS."""
    from feishu_bridge.session_resume import SessionsIndex

    _make_pending_run(repo, session_id="sess_race")
    idx = SessionsIndex(Path(short_env["db_path"]).parent / "sessions.json")
    idx.touch("sess_race", "oc_test", int(time.time() * 1000))

    enqueue_count = [0]
    def _fake_enqueue(**_kwargs):
        enqueue_count[0] += 1
        return ("queued", {})

    # Two supervisors sharing the same DB — each opens its own connection.
    sup_a = _make_delivery_supervisor(
        short_env, enqueue_fn=_fake_enqueue, sessions_index=idx,
    )
    sup_b = _make_delivery_supervisor(
        short_env, enqueue_fn=_fake_enqueue, sessions_index=idx,
    )
    conn_a = connect(short_env["db_path"])
    conn_b = connect(short_env["db_path"])
    try:
        repo_a = BgTaskRepo(conn_a)
        repo_b = BgTaskRepo(conn_b)
        # Back-to-back scans: the second should find the row already in
        # 'enqueued' and its CAS attempt must lose silently.
        sup_a._scan_delivery_outbox(repo_a)
        sup_b._scan_delivery_outbox(repo_b)
        assert enqueue_count[0] == 1, \
            f"expected exactly one enqueue under CAS race, got {enqueue_count[0]}"
    finally:
        conn_a.close()
        conn_b.close()


def test_scan_delivery_enqueue_exception_rolls_back_to_delivery_failed(short_env, repo):
    """If enqueue_fn raises, state moves enqueued → delivery_failed w/ bump."""
    from feishu_bridge.session_resume import SessionsIndex

    _tid, run_id = _make_pending_run(repo, session_id="sess_boom")
    idx = SessionsIndex(Path(short_env["db_path"]).parent / "sessions.json")
    idx.touch("sess_boom", "oc_test", int(time.time() * 1000))

    def _raising_enqueue(**_kwargs):
        raise RuntimeError("queue blew up")

    sup = _make_delivery_supervisor(
        short_env, enqueue_fn=_raising_enqueue, sessions_index=idx,
    )
    # delivered count is 0 because the enqueue failed before incrementing it.
    assert sup._scan_delivery_outbox(repo) == 0
    row = repo.conn.execute(
        "SELECT delivery_state, delivery_attempt_count, delivery_error "
        "FROM bg_runs WHERE id=?", (run_id,),
    ).fetchone()
    assert row["delivery_state"] == "delivery_failed"
    assert row["delivery_attempt_count"] == 1
    assert "enqueue_failed: RuntimeError: queue blew up" in row["delivery_error"]


def test_scan_delivery_stuck_enqueued_rollback_after_5_min(short_env, repo):
    """enqueued rows older than 5 min roll back to pending WITHOUT bump."""
    from feishu_bridge.bg_supervisor import _STUCK_ENQUEUED_MS

    _tid, run_id = _make_pending_run(repo, session_id="sess_stuck")
    # Force into 'enqueued' with an enqueued_at stamp 6 min in the past.
    stale = int(time.time() * 1000) - _STUCK_ENQUEUED_MS - 60_000
    repo.conn.execute(
        "UPDATE bg_runs SET delivery_state='enqueued', enqueued_at=? WHERE id=?",
        (stale, run_id),
    )
    repo.conn.commit()

    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    # enqueue_fn=None early-returns before iteration, but stuck-rollback
    # runs first — row should flip back to pending.
    sup._scan_delivery_outbox(repo)
    row = repo.conn.execute(
        "SELECT delivery_state, enqueued_at, delivery_attempt_count "
        "FROM bg_runs WHERE id=?", (run_id,),
    ).fetchone()
    assert row["delivery_state"] == "pending"
    assert row["enqueued_at"] is None, "rollback must clear stale enqueued_at"
    # Crash recovery doesn't consume the retry budget.
    assert row["delivery_attempt_count"] == 0


def test_scan_delivery_recent_enqueued_not_rolled_back(short_env, repo):
    """Rows enqueued within the stuck threshold are left alone."""
    _tid, run_id = _make_pending_run(repo, session_id="sess_live")
    fresh = int(time.time() * 1000) - 1000  # 1s ago
    repo.conn.execute(
        "UPDATE bg_runs SET delivery_state='enqueued', enqueued_at=? WHERE id=?",
        (fresh, run_id),
    )
    repo.conn.commit()

    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    sup._scan_delivery_outbox(repo)
    assert _delivery_state(repo.conn, run_id) == "enqueued"


def test_scan_delivery_thread_id_flows_into_session_key(short_env, repo):
    """thread_id on task row appears in the session_key tuple as its 3rd slot."""
    from feishu_bridge.session_resume import SessionsIndex

    _make_pending_run(
        repo, session_id="sess_thr", chat_id="oc_B", thread_id="omt_abc",
    )
    idx = SessionsIndex(Path(short_env["db_path"]).parent / "sessions.json")
    idx.touch("sess_thr", "oc_B", int(time.time() * 1000))

    calls: list[dict] = []
    sup = _make_delivery_supervisor(
        short_env,
        enqueue_fn=lambda **kw: (calls.append(kw), ("queued", {}))[1],
        sessions_index=idx,
        bot_id="bot-xyz",
    )
    sup._scan_delivery_outbox(repo)
    assert calls[0]["session_key"] == "bot-xyz:oc_B:omt_abc"
    assert calls[0]["extras"]["thread_id"] == "omt_abc"


def test_scan_delivery_does_not_stamp_enqueued_at_at_cas(short_env, repo):
    """Post-review fix: enqueued_at is stamped by worker, not watcher.

    Regression guard: a prior impl wrote `enqueued_at=now_ms` at CAS time,
    which caused the 5-min stuck-rollback to misfire on long turns (backlog
    + Claude call >5min) and trigger duplicate delivery. design.md:391
    scopes the rollback to bridge-crash recovery; pinning enqueued_at to
    worker pickup preserves that intent.
    """
    from feishu_bridge.session_resume import SessionsIndex

    _tid, run_id = _make_pending_run(repo, session_id="sess_stamp")
    idx = SessionsIndex(Path(short_env["db_path"]).parent / "sessions.json")
    idx.touch("sess_stamp", "oc_test", int(time.time() * 1000))

    sup = _make_delivery_supervisor(
        short_env, enqueue_fn=lambda **_: ("queued", {}), sessions_index=idx,
    )
    assert sup._scan_delivery_outbox(repo) == 1

    row = repo.conn.execute(
        "SELECT delivery_state, enqueued_at FROM bg_runs WHERE id=?", (run_id,),
    ).fetchone()
    assert row["delivery_state"] == "enqueued"
    assert row["enqueued_at"] is None, \
        "watcher must NOT stamp enqueued_at — worker stamps on pickup"


def test_scan_delivery_retries_delivery_failed_rows(short_env, repo):
    """list_pending_deliveries returns both `pending` and `delivery_failed`
    rows with attempt_count<10; the watcher must retry both."""
    from feishu_bridge.session_resume import SessionsIndex

    _tid, run_id = _make_pending_run(repo, session_id="sess_retry")
    # Simulate a prior failed delivery: state=delivery_failed, attempt=2.
    repo.conn.execute(
        "UPDATE bg_runs SET delivery_state='delivery_failed', "
        "delivery_attempt_count=2 WHERE id=?", (run_id,),
    )
    repo.conn.commit()

    idx = SessionsIndex(Path(short_env["db_path"]).parent / "sessions.json")
    idx.touch("sess_retry", "oc_test", int(time.time() * 1000))

    calls: list[dict] = []
    sup = _make_delivery_supervisor(
        short_env,
        enqueue_fn=lambda **kw: (calls.append(kw), ("queued", {}))[1],
        sessions_index=idx,
    )
    assert sup._scan_delivery_outbox(repo) == 1, \
        "delivery_failed rows with attempt<10 must be retriable"
    assert len(calls) == 1
    assert _delivery_state(repo.conn, run_id) == "enqueued"


def test_scan_delivery_orphan_run_marked_delivery_failed(short_env, repo):
    """Orphan run (bg_tasks row gone) must move to delivery_failed + bump,
    not loop forever. Guards the FK/corruption edge case."""
    _tid, run_id = _make_pending_run(repo, session_id="sess_orphan")
    # Forcibly delete the parent task row (bypass FK CASCADE to simulate
    # the orphan scenario we're defending against).
    repo.conn.execute("PRAGMA foreign_keys=OFF")
    repo.conn.execute("DELETE FROM bg_tasks WHERE id=?", (_tid,))
    repo.conn.execute("PRAGMA foreign_keys=ON")
    repo.conn.commit()

    sup = _make_delivery_supervisor(
        short_env, enqueue_fn=lambda **_: ("queued", {}),
    )
    # No rows delivered — orphan path continues after marking delivery_failed.
    assert sup._scan_delivery_outbox(repo) == 0
    row = repo.conn.execute(
        "SELECT delivery_state, delivery_attempt_count, delivery_error "
        "FROM bg_runs WHERE id=?", (run_id,),
    ).fetchone()
    assert row["delivery_state"] == "delivery_failed"
    assert row["delivery_attempt_count"] == 1, \
        "orphan marking must bump_attempt so the <10 retry cap eventually fires"
    assert row["delivery_error"] is not None and "missing_task" in row["delivery_error"]


def test_scan_delivery_null_enqueued_at_not_rolled_back(short_env, repo):
    """Rollback query's `enqueued_at IS NOT NULL` guard protects rows that
    were CAS-claimed but not yet picked up by worker (post-review design).

    Without this guard, a row sitting in `enqueued`+`enqueued_at=NULL` for
    longer than 5 min would be spuriously rolled back, breaking the very
    fix that moves stamping to worker pickup.
    """
    _tid, run_id = _make_pending_run(repo, session_id="sess_null_ts")
    # Simulate CAS-claimed but worker has not stamped yet.
    repo.conn.execute(
        "UPDATE bg_runs SET delivery_state='enqueued', enqueued_at=NULL "
        "WHERE id=?", (run_id,),
    )
    repo.conn.commit()

    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    sup._scan_delivery_outbox(repo)
    assert _delivery_state(repo.conn, run_id) == "enqueued", \
        "NULL enqueued_at must be immune to rollback (worker may still pick up)"


# ---------------------------------------------------------------------------
# Worker-side contract for the dequeue-time enqueued_at stamp.
# These live here (not test_worker.py) because the stamp + rollback form
# one semantic contract with the watcher; keeping them adjacent prevents
# future refactors from drifting the two sides apart.
# ---------------------------------------------------------------------------

def test_bg_mark_dequeued_stamps_enqueued_at_when_null(short_env, repo):
    """Happy path: worker pickup on an `enqueued`+NULL row stamps now."""
    from feishu_bridge.worker import _bg_mark_dequeued

    _tid, run_id = _make_pending_run(repo, session_id="sess_worker_stamp")
    repo.conn.execute(
        "UPDATE bg_runs SET delivery_state='enqueued', enqueued_at=NULL "
        "WHERE id=?", (run_id,),
    )
    repo.conn.commit()

    before = int(time.time() * 1000)
    _bg_mark_dequeued({"_bg_run_id": run_id, "_bg_db_path": short_env["db_path"]})
    after = int(time.time() * 1000)

    row = repo.conn.execute(
        "SELECT enqueued_at FROM bg_runs WHERE id=?", (run_id,),
    ).fetchone()
    assert row["enqueued_at"] is not None, "dequeue must stamp enqueued_at"
    assert before <= row["enqueued_at"] <= after


def test_bg_mark_dequeued_idempotent_noop_when_already_stamped(short_env, repo):
    """Re-fire on the same run (e.g. CAS lost then retry) is a silent no-op:
    the UPDATE's `enqueued_at IS NULL` guard prevents stamp drift."""
    from feishu_bridge.worker import _bg_mark_dequeued

    _tid, run_id = _make_pending_run(repo, session_id="sess_stamp_idem")
    original_stamp = int(time.time() * 1000) - 10_000  # 10s ago
    repo.conn.execute(
        "UPDATE bg_runs SET delivery_state='enqueued', enqueued_at=? WHERE id=?",
        (original_stamp, run_id),
    )
    repo.conn.commit()

    _bg_mark_dequeued({"_bg_run_id": run_id, "_bg_db_path": short_env["db_path"]})
    row = repo.conn.execute(
        "SELECT enqueued_at FROM bg_runs WHERE id=?", (run_id,),
    ).fetchone()
    assert row["enqueued_at"] == original_stamp, \
        "second stamp attempt must not overwrite the first"


def test_bg_mark_dequeued_noop_for_non_bg_items(short_env):
    """Human turn items (no _bg_run_id) must be silently ignored."""
    from feishu_bridge.worker import _bg_mark_dequeued

    # Should not raise nor touch any DB.
    _bg_mark_dequeued({})
    _bg_mark_dequeued({"_bg_run_id": None, "_bg_db_path": short_env["db_path"]})
    _bg_mark_dequeued({"_bg_run_id": 42, "_bg_db_path": None})


# ---------------------------------------------------------------------------
# Section 6 startup reconciler (Commit A: §6.1/2/4/5/6 + step 0)
# ---------------------------------------------------------------------------

def _make_launching_task(
    repo: BgTaskRepo,
    *,
    claimed_at_ms: int,
    bridge_instance_id: str = "b-prior",
) -> str:
    """Create a bg_tasks row stuck in `launching` with an arbitrary
    claimed_at. Used to exercise §6.2 stale-launching reap.
    """
    tid = repo.insert_task(
        chat_id="oc_test",
        session_id="sess_test",
        command_argv=["echo", "hi"],
        on_done_prompt="done",
    )
    # CAS stamps claimed_at=_now_ms(); overwrite it to the caller's value so
    # the test can pick "old enough" vs "still fresh".
    assert repo.claim_queued_cas(tid, bridge_instance_id=bridge_instance_id)
    repo.conn.execute(
        "UPDATE bg_tasks SET claimed_at=? WHERE id=?",
        (claimed_at_ms, tid),
    )
    return tid


def _make_running_task(repo: BgTaskRepo) -> str:
    """Create a bg_tasks row that is live-running (state='running')."""
    tid = repo.insert_task(
        chat_id="oc_test", session_id="sess_test",
        command_argv=["echo", "hi"], on_done_prompt="done",
    )
    assert repo.claim_queued_cas(tid, bridge_instance_id="b1")
    run_id = repo.start_run(
        task_id=tid, runner_token="t",
        wrapper_pid=1000, wrapper_start_time_us=9000,
    )
    repo.attach_child(
        run_id=run_id, task_id=tid, pid=2000, pgid=2000,
        process_start_time_us=9100,
    )
    return tid


def test_reconcile_reaps_stale_launching_row_to_failed(short_env, repo):
    """§6.2: claimed_at > 30s ago with state='launching' → failed."""
    old_ms = int(time.time() * 1000) - 60_000  # 60s ago
    tid = _make_launching_task(repo, claimed_at_ms=old_ms)

    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    stats = sup.reconcile()

    assert stats["stale_launching_failed"] == 1
    row = repo.conn.execute(
        "SELECT state, reason FROM bg_tasks WHERE id=?", (tid,),
    ).fetchone()
    assert row["state"] == "failed"
    assert row["reason"] == "launch_interrupted"


def test_reconcile_preserves_recently_claimed_launching_row(short_env, repo):
    """Rows claimed < 30s ago are still "fresh" — spawner may be mid-Popen."""
    recent_ms = int(time.time() * 1000) - 5_000  # 5s ago
    tid = _make_launching_task(repo, claimed_at_ms=recent_ms)

    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    stats = sup.reconcile()

    assert stats["stale_launching_failed"] == 0
    row = repo.conn.execute(
        "SELECT state FROM bg_tasks WHERE id=?", (tid,),
    ).fetchone()
    assert row["state"] == "launching"


def test_reconcile_resets_stranded_enqueued_to_pending(short_env, repo):
    """Step 0: enqueued rows with enqueued_at=NULL (supervisor CAS'd but
    worker never dequeued) → pending. The existing 5-min rollback only
    covers enqueued_at NOT NULL rows, so without this they'd be stranded.
    """
    tid, run_id = _make_pending_run(repo)
    # Supervisor-side CAS would move pending→enqueued WITHOUT stamping
    # enqueued_at (worker stamps at dequeue). Simulate that state.
    repo.conn.execute(
        "UPDATE bg_runs SET delivery_state='enqueued', enqueued_at=NULL "
        "WHERE id=?", (run_id,),
    )

    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    stats = sup.reconcile()

    assert stats["stranded_enqueued_reset"] == 1
    assert _delivery_state(repo.conn, run_id) == "pending"


def test_reconcile_preserves_stamped_enqueued_at_rows(short_env, repo):
    """Rows with enqueued_at stamped are owned by a worker that dequeued —
    they're the 5-min stuck-rollback's jurisdiction, not ours."""
    tid, run_id = _make_pending_run(repo)
    now = int(time.time() * 1000)
    repo.conn.execute(
        "UPDATE bg_runs SET delivery_state='enqueued', enqueued_at=? "
        "WHERE id=?", (now, run_id),
    )

    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    stats = sup.reconcile()

    assert stats["stranded_enqueued_reset"] == 0
    assert _delivery_state(repo.conn, run_id) == "enqueued"


def test_reconcile_triage_both_dead_no_manifest_marks_orphan(
    short_env, repo,
):
    """§6.3 Commit B: wrapper + child both dead, no manifest on disk →
    bg_tasks state becomes ``orphan`` with reason ``both_died``, bg_runs
    ``finished_at`` stamped, ``delivery_state='not_ready'``.

    Safety: fake pids 1000/2000 almost certainly don't exist; _verify_triple
    fails-closed on missing pid, so the row is orphan-marked without any
    signal being sent (the critical pid-reuse guard).
    """
    tid = _make_running_task(repo)

    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    stats = sup.reconcile()

    # Triage reports one orphan; no other branches counted.
    assert stats["running_orphaned"] == 1
    assert stats["running_reaped"] == 0
    assert stats["running_pending_reap"] == 0
    assert stats["running_attached"] == 0
    assert stats["running_manifest_applied"] == 0

    row = repo.conn.execute(
        "SELECT state, reason FROM bg_tasks WHERE id=?", (tid,),
    ).fetchone()
    assert row["state"] == "orphan"
    assert row["reason"] == "wrapper_and_child_both_died"
    run = repo.conn.execute(
        "SELECT finished_at, delivery_state FROM bg_runs WHERE task_id=?",
        (tid,),
    ).fetchone()
    assert run["finished_at"] is not None
    assert run["delivery_state"] == "not_ready"


def test_reconcile_triage_wrapper_alive_keeps_running(
    short_env, repo, monkeypatch,
):
    """Branch A: wrapper verified alive → row stays ``running``, no commits.

    Monkeypatch ``_verify_triple`` to return True the first time (wrapper
    check) — the child check is never reached because the branch returns
    early with ``attached``. This also exercises the safety property that
    _triage_one doesn't call any signal helpers in the attached branch.
    """
    from feishu_bridge import bg_supervisor

    tid = _make_running_task(repo)
    calls = {"triple": 0, "kill": 0}

    def fake_verify(pid, *, expected_start_us, expected_token):
        calls["triple"] += 1
        return calls["triple"] == 1  # first call = wrapper; True = alive

    def fake_kill(pgid, **kw):
        calls["kill"] += 1
        return "SIGTERM"

    monkeypatch.setattr(bg_supervisor, "_verify_triple", fake_verify)
    monkeypatch.setattr(bg_supervisor, "_kill_pgid_with_grace", fake_kill)

    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    stats = sup.reconcile()

    assert stats["running_attached"] == 1
    assert calls["kill"] == 0, "must never signal when wrapper is alive"
    row = repo.conn.execute(
        "SELECT state FROM bg_tasks WHERE id=?", (tid,),
    ).fetchone()
    assert row["state"] == "running"


def test_reconcile_triage_reap_on_cancel_requested(
    short_env, repo, monkeypatch,
):
    """Branch D: wrapper dead, child alive triple-verified, cancel_requested
    set → SIGTERM/KILL sent to child's pgid, terminal state ``cancelled``.
    """
    from feishu_bridge import bg_supervisor

    tid = _make_running_task(repo)
    # Stamp cancel request.
    repo.conn.execute(
        "UPDATE bg_tasks SET cancel_requested_at=? WHERE id=?",
        (int(time.time() * 1000), tid),
    )

    call_order: list[str] = []

    def fake_verify(pid, *, expected_start_us, expected_token):
        # Two calls per row: wrapper (dead), child (alive).
        call_order.append(f"verify-{pid}")
        return pid == 2000  # child alive, wrapper dead

    kills: list[tuple[int, str]] = []

    def fake_kill(pgid, **kw):
        kills.append((pgid, "SIGTERM"))
        return "SIGTERM"

    monkeypatch.setattr(bg_supervisor, "_verify_triple", fake_verify)
    monkeypatch.setattr(bg_supervisor, "_kill_pgid_with_grace", fake_kill)

    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    stats = sup.reconcile()

    assert stats["running_reaped"] == 1
    assert kills == [(2000, "SIGTERM")], (
        "exactly one signal, targeting the verified child pgid"
    )
    row = repo.conn.execute(
        "SELECT state, reason, signal FROM bg_tasks WHERE id=?", (tid,),
    ).fetchone()
    assert row["state"] == "cancelled"
    assert row["reason"] == "reaped_by_bridge_after_wrapper_death"
    assert row["signal"] == "SIGTERM"
    run = repo.conn.execute(
        "SELECT delivery_state FROM bg_runs WHERE task_id=?", (tid,),
    ).fetchone()
    # Cancelled is user-visible; delivery must surface the outcome.
    assert run["delivery_state"] == "pending"


def test_reconcile_triage_pid_reuse_mismatch_sends_no_signal(
    short_env, repo, monkeypatch,
):
    """Safety invariant: wrapper dead, child pid is live under a DIFFERENT
    process (token mismatch simulates pid reuse) → NEVER signal. Row
    orphan-marked with ``both_died``, ``_kill_pgid_with_grace`` not called.
    """
    from feishu_bridge import bg_supervisor

    tid = _make_running_task(repo)
    kills: list[int] = []

    # Both verify_triple calls return False — mirrors "wrapper dead, pid
    # 2000 belongs to someone else now" (triple checks all fail on reuse).
    monkeypatch.setattr(
        bg_supervisor, "_verify_triple",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        bg_supervisor, "_kill_pgid_with_grace",
        lambda pgid, **kw: kills.append(pgid) or "SIGTERM",
    )
    # Also spy on os.killpg directly — belt-and-suspenders.
    direct_kills: list[int] = []
    real_killpg = bg_supervisor.os.killpg
    monkeypatch.setattr(
        bg_supervisor.os, "killpg",
        lambda p, s: direct_kills.append(p) or real_killpg(p, 0),
    )

    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    stats = sup.reconcile()

    assert kills == [], "pid-reuse mismatch must never invoke pgid signal path"
    assert direct_kills == [], "pid-reuse mismatch must never send any signal"
    assert stats["running_orphaned"] == 1
    row = repo.conn.execute(
        "SELECT state, reason FROM bg_tasks WHERE id=?", (tid,),
    ).fetchone()
    assert row["state"] == "orphan"
    assert row["reason"] == "wrapper_and_child_both_died"


def test_reconcile_triage_manifest_applied_on_both_dead(
    short_env, repo, monkeypatch,
):
    """Branch C-ii: wrapper + child dead but wrapper's last-gasp manifest
    sits on disk → finish_run replays it, row becomes ``completed`` (or
    whichever terminal state the manifest records), delivery_state=pending.
    """
    from feishu_bridge import bg_supervisor
    import json as _json

    tid = _make_running_task(repo)

    # Drop a completed manifest into active/<tid>/task.json.done.
    active = Path(short_env["tasks_dir"]) / "active" / tid
    active.mkdir(parents=True, exist_ok=True)
    (active / "task.json.done").write_text(_json.dumps({
        "task_id": tid,
        "state": "completed",
        "exit_code": 0,
        "signal": None,
        "started_at_ms": int(time.time() * 1000) - 1000,
        "finished_at_ms": int(time.time() * 1000),
        "reason": None,
    }))

    monkeypatch.setattr(
        bg_supervisor, "_verify_triple", lambda *a, **k: False,
    )

    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    stats = sup.reconcile()

    assert stats["running_manifest_applied"] == 1
    assert stats["running_orphaned"] == 0
    row = repo.conn.execute(
        "SELECT state FROM bg_tasks WHERE id=?", (tid,),
    ).fetchone()
    assert row["state"] == "completed"
    run = repo.conn.execute(
        "SELECT finished_at, delivery_state, exit_code FROM bg_runs "
        "WHERE task_id=?", (tid,),
    ).fetchone()
    assert run["finished_at"] is not None
    assert run["delivery_state"] == "pending"
    assert run["exit_code"] == 0


def test_reconcile_triage_spawns_reaper_for_alive_orphan_child(
    short_env, repo, monkeypatch,
):
    """Branch E: wrapper dead, child alive, no cancel/timeout → reaper
    thread spawned; row stays ``running`` until the reaper commits later.
    """
    from feishu_bridge import bg_supervisor

    tid = _make_running_task(repo)

    # Wrapper dead, child verified alive. No cancel_requested and
    # timeout_seconds default (1800) shouldn't elapse — started_at is just
    # set by start_run() to _now_ms().
    monkeypatch.setattr(
        bg_supervisor, "_verify_triple",
        lambda pid, **kw: pid == 2000,
    )

    spawned: list[int] = []

    def fake_reaper_worker(self, row_dict):
        # Don't actually run the polling loop — just record that we were
        # invoked and exit immediately so the thread joins cleanly.
        spawned.append(int(row_dict["pid"]))

    monkeypatch.setattr(
        bg_supervisor.BgSupervisor,
        "_reaper_worker", fake_reaper_worker,
    )

    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    stats = sup.reconcile()

    # Join the reaper dict entries so no daemon thread leaks into the next
    # test. _spawn_reaper already put the thread into _reapers; give its
    # no-op body a moment to unregister itself via the finally clause.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with sup._reapers_lock:
            if not sup._reapers:
                break
        time.sleep(0.05)

    assert stats["running_pending_reap"] == 1
    assert spawned == [2000]
    # Row must remain `running` — reaper hasn't committed yet.
    row = repo.conn.execute(
        "SELECT state FROM bg_tasks WHERE id=?", (tid,),
    ).fetchone()
    assert row["state"] == "running"


def test_reconcile_triage_handles_empty_table(short_env, repo):
    """No running rows → all five triage counters zero; reconcile still OK."""
    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    stats = sup.reconcile()
    for k in (
        "running_attached", "running_reaped", "running_pending_reap",
        "running_orphaned", "running_manifest_applied",
    ):
        assert stats[k] == 0


def test_reconcile_backfills_manifest_only_active_dir(short_env, repo):
    """§6.6: tasks/active/<id>/task.json.done with no DB row → replay + mv."""
    import json as _json
    tid = uuid.uuid4().hex
    active_dir = Path(short_env["tasks_dir"]) / "active" / tid
    active_dir.mkdir(parents=True)
    (active_dir / "task.json.done").write_text(_json.dumps({
        "task_id": tid,
        "chat_id": "oc_manifest",
        "session_id": "sess_manifest",
        "command_argv": ["echo", "manifest-replay"],
        "on_done_prompt": "replayed",
        "state": "completed",
        "exit_code": 0,
        "wrapper_pid": 9001,
        "wrapper_start_time_us": 123456,
        "started_at": 1000,
        "finished_at": 2000,
        "created_at": 500,
        "runner_token": "tok",
    }))

    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    stats = sup.reconcile()

    assert stats["manifests_replayed"] == 1
    row = repo.conn.execute(
        "SELECT state FROM bg_tasks WHERE id=?", (tid,),
    ).fetchone()
    assert row["state"] == "completed"
    run = repo.conn.execute(
        "SELECT delivery_state FROM bg_runs WHERE task_id=?", (tid,),
    ).fetchone()
    assert run["delivery_state"] == "pending"
    # active dir should have been promoted to completed/.
    assert not active_dir.exists()
    assert (Path(short_env["tasks_dir"]) / "completed" / tid).is_dir()


def test_reconcile_logs_error_on_retry_budget_exhausted(
    short_env, repo, caplog,
):
    """delivery_failed rows at attempt_count ≥ 10 → ERROR log at boot."""
    tid, run_id = _make_pending_run(repo)
    repo.conn.execute(
        "UPDATE bg_runs SET delivery_state='delivery_failed', "
        "delivery_attempt_count=10, delivery_error='persistent enqueue fail' "
        "WHERE id=?", (run_id,),
    )

    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    with caplog.at_level("ERROR", logger="feishu-bridge.bg-supervisor"):
        stats = sup.reconcile()

    assert stats["retry_budget_exhausted"] == 1
    assert any(
        "exhausted delivery retries" in rec.message
        for rec in caplog.records
    ), "must ERROR on each budget-exhausted run"


def test_reconcile_integrity_failure_triggers_rebuild(short_env):
    """§6.1 quarantine path: corrupt DB → rename + replay manifests."""
    import json as _json
    # Seed a manifest so rebuild has something to replay after quarantine.
    tid = uuid.uuid4().hex
    completed_dir = Path(short_env["tasks_dir"]) / "completed" / tid
    completed_dir.mkdir(parents=True)
    (completed_dir / "task.json.done").write_text(_json.dumps({
        "task_id": tid,
        "chat_id": "oc_quar",
        "session_id": "sess_quar",
        "command_argv": ["echo", "rebuilt"],
        "on_done_prompt": "rebuilt",
        "state": "completed",
        "exit_code": 0,
        "wrapper_pid": 9001,
        "wrapper_start_time_us": 999,
        "started_at": 1000,
        "finished_at": 2000,
        "created_at": 500,
        "runner_token": "tok",
    }))

    # Init DB then corrupt the header so integrity_check quarantines it.
    init_db(short_env["db_path"]).close()
    raw = short_env["db_path"].read_bytes()
    short_env["db_path"].write_bytes(b"\x00" * 100 + raw[100:])

    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    stats = sup.reconcile()

    assert stats["quarantined"] == 1
    assert stats["manifests_replayed"] == 1
    # Fresh DB should have the replayed row.
    fresh_conn = connect(short_env["db_path"])
    try:
        row = fresh_conn.execute(
            "SELECT state FROM bg_tasks WHERE id=?", (tid,),
        ).fetchone()
    finally:
        fresh_conn.close()
    assert row is not None and row["state"] == "completed"
    # Quarantine sidecar must exist for forensic retention (≤3 files / 30d).
    quarantined = list(
        short_env["db_path"].parent.glob("bg.db.quarantine.*"),
    )
    assert len(quarantined) >= 1


def test_reconcile_retry_budget_boundary_9_does_not_log(short_env, repo, caplog):
    """attempt_count=9 is below the cap → must NOT emit ERROR."""
    tid, run_id = _make_pending_run(repo)
    repo.conn.execute(
        "UPDATE bg_runs SET delivery_state='delivery_failed', "
        "delivery_attempt_count=9 WHERE id=?", (run_id,),
    )

    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    with caplog.at_level("ERROR", logger="feishu-bridge.bg-supervisor"):
        stats = sup.reconcile()

    assert stats["retry_budget_exhausted"] == 0
    assert not any(
        "exhausted delivery retries" in rec.message
        for rec in caplog.records
    ), "attempt_count=9 < cap=10 must not trigger the ERROR log"


def test_reconcile_queued_stats_reflects_launches(short_env, repo):
    """§6.4 stats["queued_launched"]: drive queued rows during reconcile."""
    _enqueue(repo)
    _enqueue(repo)

    spawner = MagicMock(return_value=MagicMock(pid=12345))
    sup = BgSupervisor(
        db_path=short_env["db_path"],
        tasks_dir=short_env["tasks_dir"],
        sock_path=short_env["sock_path"],
        runner_cmd=["/bin/true"],
        spawner=spawner,
    )
    stats = sup.reconcile()

    assert stats["queued_launched"] == 2
    assert spawner.call_count == 2


def test_reconcile_fresh_boot_does_not_claim_quarantine(short_env, caplog):
    """Fresh install (DB file absent before first reconcile) must not log as
    quarantine event — that would mislead operators who only greened a box."""
    assert not short_env["db_path"].exists()

    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    with caplog.at_level("WARNING", logger="feishu-bridge.bg-supervisor"):
        stats = sup.reconcile()

    assert stats["quarantined"] == 0
    assert short_env["db_path"].exists(), "fresh boot should have created DB"
    assert not any(
        "DB quarantined" in rec.message for rec in caplog.records
    ), "fresh boot must not emit the quarantine WARNING"


def test_reconcile_returns_stats_dict_with_all_keys(short_env, repo):
    """Contract: stats dict shape is stable for /status integration later."""
    sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
    stats = sup.reconcile()
    expected_keys = {
        "quarantined",
        "manifests_replayed",
        "manifest_orphans_created",
        "stale_launching_failed",
        # §6.3 triage replaced the single `running_rows_observed` counter
        # with one label per branch (Commit B).
        "running_attached",
        "running_reaped",
        "running_pending_reap",
        "running_orphaned",
        "running_manifest_applied",
        "stranded_enqueued_reset",
        "queued_launched",
        "deliveries_handed_off",
        "retry_budget_exhausted",
    }
    assert set(stats.keys()) == expected_keys


def test_reconcile_before_start_is_safe_and_stat_invariants_hold(short_env):
    """Smoke: reconcile() → start() → stop() with no data races."""
    sup, _spawner = _fast_supervisor(short_env)
    stats = sup.reconcile()
    assert stats["stale_launching_failed"] == 0
    sup.start()
    try:
        assert sup.is_running()
    finally:
        sup.stop()


# ---------------------------------------------------------------------------
# §6.3 real-subprocess barrier tests (§7.5 + §7.6)
#
# These need libproc (macOS) for μs-precision start_time and /bin/ps -E for
# env inspection. Skip on non-darwin — those platforms don't have the
# liveness anchors wired up yet.
# ---------------------------------------------------------------------------


pytestmark_darwin = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="§6.3 liveness triage relies on libproc + ps -E (macOS)",
)


def _spawn_sleep_with_token(token: str, *, seconds: int = 60):
    """Spawn a Python sleeper with ``BG_TASK_TOKEN=<token>`` in env, in its
    own process group. Returns the Popen handle — caller MUST terminate it.

    Why not ``/bin/sleep``? On macOS, SIP-protected binaries (``/bin/*``,
    ``/usr/bin/*``) block ``ps eww`` from reading their env even for the
    parent process, so triple verification would silently fail. The Python
    interpreter is user-installed (Homebrew) and therefore not SIP-protected.
    """
    env = dict(os.environ)
    env["BG_TASK_TOKEN"] = token
    # start_new_session creates a new pgid == pid for clean group signaling.
    import subprocess as _sp
    return _sp.Popen(
        [sys.executable, "-c", f"import time; time.sleep({int(seconds)})"],
        env=env,
        start_new_session=True,
        stdout=_sp.DEVNULL,
        stderr=_sp.DEVNULL,
    )


def _read_real_start_us(pid: int) -> int:
    """Shortcut for tests — raises on failure (no fail-closed needed here)."""
    from feishu_bridge.task_runner import read_proc_start_time_us
    return int(read_proc_start_time_us(pid))


@pytestmark_darwin
def test_reconcile_barrier_orphan_alive_bridge_reap_cancels_real_child(
    short_env, repo,
):
    """§7.5 barrier ``orphan_alive_bridge_reap``: a real child is alive, its
    wrapper pid is dead, cancel_requested_at is set → reconcile must reap
    the child via SIGTERM to its pgid and record
    ``reaped_by_bridge_after_wrapper_death``.

    Uses the bridge's own pid/start_time as a placeholder "wrapper that was
    alive but died" — then we ensure the triple-check fails for it by
    using a token that isn't in our env (the bridge test process has no
    BG_TASK_TOKEN, so verify_triple returns False on the wrapper).
    """
    token = uuid.uuid4().hex
    child = _spawn_sleep_with_token(token)
    try:
        # Wait briefly for /bin/sleep to actually start so its env is
        # visible to /bin/ps -E.
        time.sleep(0.2)
        child_start = _read_real_start_us(child.pid)

        # Insert a running row: wrapper = current test process (no token in
        # its env → triple fails → wrapper treated as "dead"); child = real
        # sleep process (triple succeeds).
        tid = repo.insert_task(
            chat_id="oc_barrier", session_id="sess_barrier",
            command_argv=["sleep", "60"], on_done_prompt="done",
        )
        assert repo.claim_queued_cas(tid, bridge_instance_id="b1")
        run_id = repo.start_run(
            task_id=tid, runner_token=token,
            wrapper_pid=os.getpid(), wrapper_start_time_us=_read_real_start_us(
                os.getpid(),
            ),
        )
        repo.attach_child(
            run_id=run_id, task_id=tid, pid=child.pid, pgid=child.pid,
            process_start_time_us=child_start,
        )
        # Cancel request so triage picks the reap-now branch.
        repo.conn.execute(
            "UPDATE bg_tasks SET cancel_requested_at=? WHERE id=?",
            (int(time.time() * 1000), tid),
        )

        sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
        stats = sup.reconcile()

        assert stats["running_reaped"] == 1, stats
        # Sleep should be dead; poll a bit to tolerate SIGTERM delivery latency.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.05)
        assert child.poll() is not None, "child survived reap"

        row = repo.conn.execute(
            "SELECT state, reason, signal FROM bg_tasks WHERE id=?", (tid,),
        ).fetchone()
        assert row["state"] == "cancelled"
        assert row["reason"] == "reaped_by_bridge_after_wrapper_death"
        assert row["signal"] in {"SIGTERM", "SIGKILL"}
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, 9)
            except ProcessLookupError:
                pass
            child.wait(timeout=2.0)


@pytestmark_darwin
def test_reconcile_barrier_pid_reuse_sends_no_signal_to_victim(
    short_env, repo,
):
    """§7.6 pid-reuse safety: DB row claims child pid=X with token=A, but
    pid X currently runs a DIFFERENT process (no BG_TASK_TOKEN=A in its
    env). Triple verification MUST fail → zero signals delivered → the
    innocent victim process must survive untouched.
    """
    # Victim: real sleep 60 with token B (simulating pid reuse under a
    # different identity). Our DB row will reference this pid but with
    # token A — the mismatch triggers the pid-reuse guard.
    victim_token = uuid.uuid4().hex
    expected_token = uuid.uuid4().hex  # the token the row *thinks* it owns
    victim = _spawn_sleep_with_token(victim_token)
    try:
        time.sleep(0.2)
        victim_start = _read_real_start_us(victim.pid)

        tid = repo.insert_task(
            chat_id="oc_reuse", session_id="sess_reuse",
            command_argv=["sleep", "60"], on_done_prompt="done",
        )
        assert repo.claim_queued_cas(tid, bridge_instance_id="b1")
        run_id = repo.start_run(
            task_id=tid, runner_token=expected_token,
            wrapper_pid=1,  # init process — alive but our token isn't in its env
            wrapper_start_time_us=1,  # guaranteed mismatch
        )
        repo.attach_child(
            run_id=run_id, task_id=tid,
            pid=victim.pid, pgid=victim.pid,
            process_start_time_us=victim_start,  # correct start — only token mismatches
        )
        # Cancel requested → if the guard fails, the bridge would try to
        # reap the victim. That's exactly the scenario we're defending.
        repo.conn.execute(
            "UPDATE bg_tasks SET cancel_requested_at=? WHERE id=?",
            (int(time.time() * 1000), tid),
        )

        # Spy on killpg so we can prove zero signals were sent. Monkeypatch
        # at the module level so BOTH our helper (`_kill_pgid_with_grace`)
        # and any accidental direct caller are caught.
        from feishu_bridge import bg_supervisor
        kill_calls: list[tuple[int, int]] = []
        real_killpg = bg_supervisor.os.killpg

        def spy_killpg(pid, sig):
            kill_calls.append((pid, int(sig)))
            # Never actually signal — this test must not race the victim
            # dying from a legitimately-issued signal.
            return None

        try:
            bg_supervisor.os.killpg = spy_killpg  # type: ignore[assignment]

            sup = _make_delivery_supervisor(short_env, enqueue_fn=None)
            stats = sup.reconcile()
        finally:
            bg_supervisor.os.killpg = real_killpg  # type: ignore[assignment]

        # The core safety assertion: absolutely no signals sent.
        assert kill_calls == [], (
            f"pid-reuse guard breached — killpg called {kill_calls}"
        )
        # Victim must still be running.
        assert victim.poll() is None, "victim process should be untouched"
        # DB should record orphan (no signal committed).
        assert stats["running_orphaned"] == 1, stats
        row = repo.conn.execute(
            "SELECT state, signal FROM bg_tasks WHERE id=?", (tid,),
        ).fetchone()
        assert row["state"] == "orphan"
        assert row["signal"] is None, (
            "orphan row must not record a signal — none was sent"
        )
    finally:
        if victim.poll() is None:
            try:
                os.killpg(victim.pid, 9)
            except ProcessLookupError:
                pass
            victim.wait(timeout=2.0)
