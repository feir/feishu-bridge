from __future__ import annotations

import sys
import time

import pytest

from feishu_bridge.bg_supervisor import BgSupervisor
from feishu_bridge.session_resume import SessionsIndex

from .bg_test_helpers import cleanup_home, make_short_home, run_cli_json, wait_until


pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="task-runner identity checks use macOS libproc",
)


def test_cli_enqueue_real_runner_delivers_synthetic_turn():
    root, bg_home = make_short_home("fb-bg-e2e-")
    calls: list[dict] = []
    try:
        idx = SessionsIndex(bg_home / "sessions.json")
        idx.touch("sess_e2e", "oc_e2e", int(time.time() * 1000))

        def enqueue_spy(**kwargs):
            calls.append(kwargs)
            return ("queued", {})

        sup = BgSupervisor(
            db_path=bg_home / "bg_tasks.db",
            tasks_dir=bg_home / "bg_tasks",
            sock_path=bg_home / "wake.sock",
            poll_interval=0.05,
            enqueue_fn=enqueue_spy,
            bot_id="bot-test",
            sessions_index=idx,
        )
        sup.start()
        try:
            payload = run_cli_json(root, [
                "bg", "enqueue",
                "--chat-id", "oc_e2e",
                "--session-id", "sess_e2e",
                "--on-done-prompt", "Summarize the background result.",
                "--",
                sys.executable, "-c", "print('BG_E2E_OK')",
            ])
            task_id = payload["task_id"]

            assert wait_until(lambda: calls, timeout=8.0), "no synthetic turn queued"
            turn = calls[0]
            assert turn["kind"] == "bg_task_completion"
            assert turn["chat_id"] == "oc_e2e"
            assert turn["session_id"] == "sess_e2e"
            assert turn["session_key"] == "bot-test:oc_e2e:"
            assert f"[bg-task:{task_id}]" in turn["prompt"]
            assert "BG_E2E_OK" in turn["prompt"]

            status = run_cli_json(root, ["bg", "status", task_id])
            assert status["state"] == "completed"
        finally:
            sup.stop()
    finally:
        cleanup_home(root)
