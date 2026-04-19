from __future__ import annotations

import sqlite3
import threading
import time

from feishu_bridge.bg_tasks_db import init_db

from .bg_test_helpers import cleanup_home, make_short_home, run_cli_json


def test_cli_enqueue_waits_out_writer_lock_and_succeeds():
    root, bg_home = make_short_home("fb-bg-busy-")
    locker = None
    release = threading.Event()
    try:
        db_path = bg_home / "bg_tasks.db"
        init_db(db_path).close()

        locker = sqlite3.connect(
            str(db_path),
            isolation_level=None,
            timeout=10.0,
            check_same_thread=False,
        )
        locker.execute("BEGIN IMMEDIATE")

        def release_later():
            time.sleep(1.0)
            release.set()

        t = threading.Thread(target=release_later, daemon=True)
        t.start()

        def commit_when_released():
            release.wait(timeout=3.0)
            locker.execute("COMMIT")

        releaser = threading.Thread(target=commit_when_released)
        releaser.start()

        start = time.monotonic()
        payload = run_cli_json(root, [
            "bg", "enqueue",
            "--chat-id", "oc_busy",
            "--on-done-prompt", "done",
            "--", "echo", "busy",
        ], timeout=8.0)
        elapsed = time.monotonic() - start

        releaser.join(timeout=3.0)
        assert payload["state"] == "queued"
        assert elapsed < 5.0
    finally:
        if locker is not None:
            try:
                locker.close()
            except sqlite3.Error:
                pass
        cleanup_home(root)
