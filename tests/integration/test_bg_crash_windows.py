from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import pytest

from feishu_bridge.bg_supervisor import BgSupervisor, _STUCK_ENQUEUED_MS
from feishu_bridge.bg_tasks_db import BgTaskRepo, connect, init_db
from feishu_bridge.session_resume import SessionsIndex

from .bg_test_helpers import cleanup_home, make_short_home, run_cli_json, wait_until


def _repo(bg_home):
    conn = init_db(bg_home / "bg_tasks.db")
    return conn, BgTaskRepo(conn)


def _supervisor(bg_home, *, enqueue_fn=None, sessions_index=None):
    return BgSupervisor(
        db_path=bg_home / "bg_tasks.db",
        tasks_dir=bg_home / "bg_tasks",
        sock_path=bg_home / "wake.sock",
        poll_interval=0.05,
        enqueue_fn=enqueue_fn,
        sessions_index=sessions_index,
        bot_id="bot-test",
    )


def _make_pending_run(repo: BgTaskRepo, *, session_id: str = "sess_crash"):
    tid = repo.insert_task(
        chat_id="oc_crash",
        session_id=session_id,
        command_argv=["echo", "ok"],
        on_done_prompt="summarize",
    )
    assert repo.claim_queued_cas(tid, "bridge-a")
    run_id = repo.start_run(
        task_id=tid,
        runner_token=uuid.uuid4().hex,
        wrapper_pid=1000,
        wrapper_start_time_us=100,
    )
    repo.attach_child(
        run_id=run_id,
        task_id=tid,
        pid=2000,
        pgid=2000,
        process_start_time_us=200,
    )
    repo.finish_run(
        run_id=run_id,
        task_id=tid,
        terminal_state="completed",
        exit_code=0,
        signal=None,
        stdout_tail=b"ok\n",
        stderr_tail=b"",
        manifest_path="/tmp/task.json.done",
    )
    return tid, run_id


def test_post_claim_reconcile_marks_stale_launching_failed():
    root, bg_home = make_short_home("fb-bg-claim-")
    try:
        conn, repo = _repo(bg_home)
        try:
            tid = repo.insert_task(
                chat_id="oc_claim",
                session_id="sess_claim",
                command_argv=["sleep", "1"],
                on_done_prompt="done",
            )
            assert repo.claim_queued_cas(tid, "dead-bridge")
            old = int(time.time() * 1000) - 60_000
            conn.execute("UPDATE bg_tasks SET claimed_at=? WHERE id=?", (old, tid))
        finally:
            conn.close()

        stats = _supervisor(bg_home).reconcile()
        assert stats["stale_launching_failed"] == 1
        status = run_cli_json(root, ["bg", "status", tid])
        assert status["state"] == "failed"
        assert status["reason"] == "launch_interrupted"
    finally:
        cleanup_home(root)


def test_post_db_pre_enqueue_reconcile_hands_off_delivery():
    root, bg_home = make_short_home("fb-bg-db-pre-enq-")
    calls: list[dict] = []
    try:
        conn, repo = _repo(bg_home)
        try:
            tid, run_id = _make_pending_run(repo)
            idx = SessionsIndex(bg_home / "sessions.json")
            idx.touch("sess_crash", "oc_crash", int(time.time() * 1000))
        finally:
            conn.close()

        stats = _supervisor(
            bg_home,
            enqueue_fn=lambda **kw: (calls.append(kw), ("queued", {}))[1],
            sessions_index=idx,
        ).reconcile()
        assert stats["deliveries_handed_off"] == 1
        assert len(calls) == 1
        assert f"[bg-task:{tid}]" in calls[0]["prompt"]

        conn = connect(bg_home / "bg_tasks.db")
        try:
            row = conn.execute(
                "SELECT delivery_state FROM bg_runs WHERE id=?", (run_id,),
            ).fetchone()
            assert row["delivery_state"] == "enqueued"
        finally:
            conn.close()
    finally:
        cleanup_home(root)


def test_delivery_send_failure_retries_from_delivery_failed():
    root, bg_home = make_short_home("fb-bg-sendfail-")
    calls: list[dict] = []
    try:
        conn, repo = _repo(bg_home)
        try:
            _tid, run_id = _make_pending_run(repo)
            idx = SessionsIndex(bg_home / "sessions.json")
            idx.touch("sess_crash", "oc_crash", int(time.time() * 1000))
            first = _supervisor(
                bg_home,
                enqueue_fn=lambda **_: (_ for _ in ()).throw(RuntimeError("send failed")),
                sessions_index=idx,
            ).reconcile()
            assert first["deliveries_handed_off"] == 0
            row = conn.execute(
                "SELECT delivery_state, delivery_attempt_count FROM bg_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            assert row["delivery_state"] == "delivery_failed"
            assert row["delivery_attempt_count"] == 1
        finally:
            conn.close()

        second = _supervisor(
            bg_home,
            enqueue_fn=lambda **kw: (calls.append(kw), ("queued", {}))[1],
            sessions_index=idx,
        ).reconcile()
        assert second["deliveries_handed_off"] == 1
        assert len(calls) == 1
    finally:
        cleanup_home(root)


def test_stuck_enqueued_rolls_back_and_retries():
    root, bg_home = make_short_home("fb-bg-stuck-")
    calls: list[dict] = []
    try:
        conn, repo = _repo(bg_home)
        try:
            _tid, run_id = _make_pending_run(repo)
            stale = int(time.time() * 1000) - _STUCK_ENQUEUED_MS - 60_000
            conn.execute(
                "UPDATE bg_runs SET delivery_state='enqueued', enqueued_at=? WHERE id=?",
                (stale, run_id),
            )
            idx = SessionsIndex(bg_home / "sessions.json")
            idx.touch("sess_crash", "oc_crash", int(time.time() * 1000))
        finally:
            conn.close()

        stats = _supervisor(
            bg_home,
            enqueue_fn=lambda **kw: (calls.append(kw), ("queued", {}))[1],
            sessions_index=idx,
        ).reconcile()
        assert stats["deliveries_handed_off"] == 1
        assert len(calls) == 1
    finally:
        cleanup_home(root)


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="pre-register token scan relies on macOS ps/libproc assumptions",
)
def test_post_spawn_pre_register_reaps_real_child_by_runner_token():
    root, bg_home = make_short_home("fb-bg-pre-reg-")
    token = uuid.uuid4().hex
    child = None
    try:
        env = dict(os.environ)
        env["BG_TASK_TOKEN"] = token
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env=env,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.2)

        conn, repo = _repo(bg_home)
        try:
            tid = repo.insert_task(
                chat_id="oc_pre",
                session_id="sess_pre",
                command_argv=["sleep", "60"],
                on_done_prompt="done",
            )
            assert repo.claim_queued_cas(tid, "bridge-dead")
            old = int(time.time() * 1000) - 60_000
            conn.execute("UPDATE bg_tasks SET claimed_at=? WHERE id=?", (old, tid))
            repo.start_run(
                task_id=tid,
                runner_token=token,
                wrapper_pid=os.getpid(),
                wrapper_start_time_us=1,
            )
        finally:
            conn.close()

        stats = _supervisor(bg_home).reconcile()
        assert stats["pre_register_reaped"] == 1, stats
        assert wait_until(lambda: child.poll() is not None, timeout=3.0)
        status = run_cli_json(root, ["bg", "status", tid])
        assert status["state"] == "orphan"
        assert status["reason"] == "wrapper_died_pre_register"
        assert status["signal"] in ("SIGTERM", "SIGKILL")
    finally:
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, 9)
            except ProcessLookupError:
                pass
            child.wait(timeout=2.0)
        cleanup_home(root)
