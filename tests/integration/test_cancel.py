from __future__ import annotations

import sys
import time

import pytest

from feishu_bridge.bg_supervisor import BgSupervisor

from .bg_test_helpers import cleanup_home, make_short_home, run_cli_json, wait_until


def _supervisor(bg_home):
    return BgSupervisor(
        db_path=bg_home / "bg_tasks.db",
        tasks_dir=bg_home / "bg_tasks",
        sock_path=bg_home / "wake.sock",
        poll_interval=0.05,
    )


def test_queued_cancel_flips_to_cancelled_without_launch():
    root, bg_home = make_short_home("fb-bg-cancel-queued-")
    try:
        payload = run_cli_json(root, [
            "bg", "enqueue",
            "--chat-id", "oc_cancel",
            "--on-done-prompt", "done",
            "--", "sleep", "60",
        ])
        task_id = payload["task_id"]
        run_cli_json(root, ["bg", "cancel", task_id])

        sup = _supervisor(bg_home)
        sup.start()
        try:
            status = wait_until(
                lambda: run_cli_json(root, ["bg", "status", task_id])
                if run_cli_json(root, ["bg", "status", task_id])["state"] == "cancelled"
                else None,
                timeout=3.0,
            )
            assert status is not None
            assert status["reason"] == "cancelled_before_launch"
        finally:
            sup.stop()
    finally:
        cleanup_home(root)


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="task-runner identity checks use macOS libproc",
)
def test_running_cancel_terminates_process_group_with_sigkill_fallback():
    root, bg_home = make_short_home("fb-bg-cancel-run-")
    try:
        sup = _supervisor(bg_home)
        sup.start()
        try:
            payload = run_cli_json(root, [
                "bg", "enqueue",
                "--chat-id", "oc_cancel",
                "--on-done-prompt", "done",
                "--timeout-seconds", "30",
                "--",
                sys.executable,
                "-c",
                (
                    "import signal,time;"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                    "time.sleep(60)"
                ),
            ])
            task_id = payload["task_id"]

            assert wait_until(
                lambda: run_cli_json(root, ["bg", "status", task_id])["state"] == "running",
                timeout=5.0,
            )

            start = time.monotonic()
            run_cli_json(root, ["bg", "cancel", task_id])

            status = wait_until(
                lambda: (
                    s if (s := run_cli_json(root, ["bg", "status", task_id]))["state"]
                    == "cancelled" else None
                ),
                timeout=10.0,
                interval=0.2,
            )
            elapsed = time.monotonic() - start
            assert status is not None
            assert elapsed <= 10.0
            assert status["reason"] == "cancelled"
            assert status["signal"] in ("SIGTERM", "SIGKILL")
        finally:
            sup.stop()
    finally:
        cleanup_home(root)
