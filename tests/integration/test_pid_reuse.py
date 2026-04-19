from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import pytest

from feishu_bridge import bg_supervisor
from feishu_bridge.bg_supervisor import BgSupervisor
from feishu_bridge.bg_tasks_db import BgTaskRepo, init_db
from feishu_bridge.task_runner import read_proc_start_time_us

from .bg_test_helpers import cleanup_home, make_short_home, run_cli_json


pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="pid-reuse guard uses macOS libproc start-time identity",
)


def test_pid_reuse_guard_never_signals_mismatched_process(monkeypatch):
    root, bg_home = make_short_home("fb-bg-reuse-")
    victim = None
    real_killpg = os.killpg
    try:
        victim_token = uuid.uuid4().hex
        expected_token = uuid.uuid4().hex
        env = dict(os.environ)
        env["BG_TASK_TOKEN"] = victim_token
        victim = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env=env,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.2)
        victim_start_us = int(read_proc_start_time_us(victim.pid))

        conn = init_db(bg_home / "bg_tasks.db")
        repo = BgTaskRepo(conn)
        try:
            tid = repo.insert_task(
                chat_id="oc_reuse",
                session_id="sess_reuse",
                command_argv=["sleep", "60"],
                on_done_prompt="done",
            )
            assert repo.claim_queued_cas(tid, "bridge-a")
            run_id = repo.start_run(
                task_id=tid,
                runner_token=expected_token,
                wrapper_pid=1,
                wrapper_start_time_us=1,
            )
            repo.attach_child(
                run_id=run_id,
                task_id=tid,
                pid=victim.pid,
                pgid=victim.pid,
                process_start_time_us=victim_start_us,
            )
            conn.execute(
                "UPDATE bg_tasks SET cancel_requested_at=? WHERE id=?",
                (int(time.time() * 1000), tid),
            )
        finally:
            conn.close()

        kill_calls: list[tuple[int, int]] = []

        def spy_killpg(pgid, sig):
            kill_calls.append((int(pgid), int(sig)))
            return None

        monkeypatch.setattr(bg_supervisor.os, "killpg", spy_killpg)

        sup = BgSupervisor(
            db_path=bg_home / "bg_tasks.db",
            tasks_dir=bg_home / "bg_tasks",
            sock_path=bg_home / "wake.sock",
        )
        stats = sup.reconcile()

        assert kill_calls == []
        assert victim.poll() is None
        assert stats["running_orphaned"] == 1
        status = run_cli_json(root, ["bg", "status", tid])
        assert status["state"] == "orphan"
        assert status["signal"] is None
    finally:
        if victim is not None and victim.poll() is None:
            try:
                real_killpg(victim.pid, 9)
            except ProcessLookupError:
                pass
            victim.wait(timeout=2.0)
        cleanup_home(root)
