from __future__ import annotations

import base64
import json
import time
import uuid

from feishu_bridge.bg_supervisor import BgSupervisor
from feishu_bridge.bg_tasks_db import init_db

from .bg_test_helpers import cleanup_home, make_short_home, run_cli_json


def test_corrupt_db_is_quarantined_and_rebuilt_from_manifests(caplog):
    root, bg_home = make_short_home("fb-bg-recover-")
    try:
        db_path = bg_home / "bg_tasks.db"
        tasks_dir = bg_home / "bg_tasks"
        task_id = uuid.uuid4().hex
        completed = tasks_dir / "completed" / task_id
        completed.mkdir(parents=True, exist_ok=True)
        (completed / "task.json.done").write_text(json.dumps({
            "schema_version": 2,
            "task_id": task_id,
            "chat_id": "oc_recover",
            "session_id": "sess_recover",
            "command_argv": ["echo", "recover"],
            "on_done_prompt": "done",
            "state": "completed",
            "exit_code": 0,
            "runner_token": "tok",
            "pid": 123,
            "pgid": 123,
            "process_start_time_us": 1000,
            "wrapper_pid": 456,
            "wrapper_start_time_us": 900,
            "started_at_ms": int(time.time() * 1000) - 1000,
            "finished_at_ms": int(time.time() * 1000),
            "stdout_tail_b64": base64.b64encode(b"recover\n").decode("ascii"),
            "stderr_tail_b64": "",
        }))

        init_db(db_path).close()
        raw = db_path.read_bytes()
        db_path.write_bytes(b"\x00" * 100 + raw[100:])

        sup = BgSupervisor(
            db_path=db_path,
            tasks_dir=tasks_dir,
            sock_path=bg_home / "wake.sock",
        )
        with caplog.at_level("ERROR", logger="feishu-bridge.bg"):
            stats = sup.reconcile()

        assert stats["quarantined"] == 1
        assert stats["manifests_replayed"] == 1
        assert list(bg_home.glob("bg_tasks.db.quarantine.*"))
        assert any("integrity_check" in rec.message for rec in caplog.records)

        status = run_cli_json(root, ["bg", "status", task_id])
        assert status["state"] == "completed"
        assert status["chat_id"] == "oc_recover"
    finally:
        cleanup_home(root)
