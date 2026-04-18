"""Unit tests for `feishu-cli bg ...` subcommands (Task 3.1-3.4)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from feishu_bridge.bg_tasks_db import BgTaskRepo, TaskState, connect


@pytest.fixture
def bg_home(tmp_path, monkeypatch):
    """Redirect HOME so CLI writes under tmp_path/.feishu-bridge."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / ".feishu-bridge"


def _run_cli(args, env_overrides=None, expect_ok=False):
    """Invoke the CLI as a subprocess; returns (rc, stdout, stderr)."""
    env = {**os.environ}
    if env_overrides:
        env.update(env_overrides)
    # HOME is the knob — child inherits via env.
    proc = subprocess.run(
        [sys.executable, "-m", "feishu_bridge.cli", *args],
        capture_output=True, text=True, env=env, timeout=20,
    )
    if expect_ok:
        assert proc.returncode == 0, (
            f"rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# 3.2 — enqueue
# ---------------------------------------------------------------------------

def test_enqueue_positional_argv_succeeds(bg_home, tmp_path):
    rc, out, _ = _run_cli(
        ["bg", "enqueue",
         "--chat-id", "oc_smoke",
         "--on-done-prompt", "analyze it",
         "--", "python3", "-c", "print('hi')"],
        env_overrides={"HOME": str(tmp_path)},
        expect_ok=True,
    )
    payload = json.loads(out)
    assert payload["state"] == "queued"
    assert len(payload["task_id"]) == 32
    assert isinstance(payload["enqueue_latency_ms"], int)
    assert payload["enqueue_latency_ms"] >= 0

    # Verify DB row matches
    conn = connect(bg_home / "bg_tasks.db")
    repo = BgTaskRepo(conn)
    row = repo.get(payload["task_id"])
    assert row is not None
    assert row.state == "queued"
    assert row.chat_id == "oc_smoke"
    assert row.command_argv == ["python3", "-c", "print('hi')"]
    assert row.session_id == "oc_smoke"  # defaults to chat_id
    assert row.timeout_seconds == 1800
    conn.close()


def test_enqueue_cmd_json_accepts_array(bg_home, tmp_path):
    rc, out, _ = _run_cli(
        ["bg", "enqueue",
         "--chat-id", "oc_x",
         "--on-done-prompt", "p",
         "--cmd-json", '["bash","-lc","true"]'],
        env_overrides={"HOME": str(tmp_path)},
        expect_ok=True,
    )
    payload = json.loads(out)
    conn = connect(bg_home / "bg_tasks.db")
    row = BgTaskRepo(conn).get(payload["task_id"])
    assert row.command_argv == ["bash", "-lc", "true"]
    conn.close()


def test_enqueue_rejects_bare_string_no_argv(bg_home, tmp_path):
    rc, out, err = _run_cli(
        ["bg", "enqueue",
         "--chat-id", "oc_x",
         "--on-done-prompt", "p"],
        env_overrides={"HOME": str(tmp_path)},
    )
    assert rc == 2
    assert "Command argv required" in err


def test_enqueue_rejects_both_argv_and_cmd_json(bg_home, tmp_path):
    rc, _, err = _run_cli(
        ["bg", "enqueue",
         "--chat-id", "oc_x",
         "--on-done-prompt", "p",
         "--cmd-json", '["x"]',
         "--", "echo", "hi"],
        env_overrides={"HOME": str(tmp_path)},
    )
    assert rc == 2
    assert "not both" in err


def test_enqueue_missing_on_done_prompt_rejected(bg_home, tmp_path):
    rc, _, err = _run_cli(
        ["bg", "enqueue", "--chat-id", "oc_x", "--", "echo", "hi"],
        env_overrides={"HOME": str(tmp_path)},
    )
    # argparse itself rejects this with rc=2
    assert rc == 2
    assert "on-done-prompt" in err


def test_enqueue_default_timeout_is_1800(bg_home, tmp_path):
    rc, out, _ = _run_cli(
        ["bg", "enqueue", "--chat-id", "oc_x",
         "--on-done-prompt", "p", "--", "echo", "hi"],
        env_overrides={"HOME": str(tmp_path)},
        expect_ok=True,
    )
    tid = json.loads(out)["task_id"]
    conn = connect(bg_home / "bg_tasks.db")
    assert BgTaskRepo(conn).get(tid).timeout_seconds == 1800
    conn.close()


# Codex Round 2 findings — positive-int validators
def test_enqueue_timeout_zero_rejected(bg_home, tmp_path):
    rc, _, err = _run_cli(
        ["bg", "enqueue", "--chat-id", "oc_x",
         "--on-done-prompt", "p",
         "--timeout-seconds", "0",
         "--", "echo", "hi"],
        env_overrides={"HOME": str(tmp_path)},
    )
    assert rc == 2
    assert "must be > 0" in err
    # Invalid args must not create on-disk DB (Codex #4).
    assert not (bg_home / "bg_tasks.db").exists()


def test_enqueue_timeout_negative_rejected(bg_home, tmp_path):
    rc, _, err = _run_cli(
        ["bg", "enqueue", "--chat-id", "oc_x",
         "--on-done-prompt", "p",
         "--timeout-seconds", "-5",
         "--", "echo", "hi"],
        env_overrides={"HOME": str(tmp_path)},
    )
    assert rc == 2
    assert "must be > 0" in err


def test_enqueue_timeout_exceeds_cap_rejected(bg_home, tmp_path):
    rc, _, err = _run_cli(
        ["bg", "enqueue", "--chat-id", "oc_x",
         "--on-done-prompt", "p",
         "--timeout-seconds", "86401",
         "--", "echo", "hi"],
        env_overrides={"HOME": str(tmp_path)},
    )
    assert rc == 2
    assert "must be <= 86400" in err


def test_list_limit_negative_rejected(bg_home, tmp_path):
    rc, _, err = _run_cli(
        ["bg", "list", "--limit", "-1"],
        env_overrides={"HOME": str(tmp_path)},
    )
    assert rc == 2
    assert "must be > 0" in err


def test_list_limit_zero_rejected(bg_home, tmp_path):
    rc, _, err = _run_cli(
        ["bg", "list", "--limit", "0"],
        env_overrides={"HOME": str(tmp_path)},
    )
    assert rc == 2
    assert "must be > 0" in err


def test_list_limit_exceeds_cap_rejected(bg_home, tmp_path):
    rc, _, err = _run_cli(
        ["bg", "list", "--limit", "201"],
        env_overrides={"HOME": str(tmp_path)},
    )
    assert rc == 2
    assert "must be <= 200" in err


# Codex Round 2 — consistent JSON error for read paths when DB missing
def test_status_missing_db_returns_json_error(bg_home, tmp_path):
    # bg_home absent entirely (no enqueue ever ran in this HOME)
    assert not (bg_home / "bg_tasks.db").exists()
    rc, _, err = _run_cli(
        ["bg", "status", "deadbeefcafefacefeedfacecafef00d0"],
        env_overrides={"HOME": str(tmp_path)},
    )
    assert rc == 1
    payload = json.loads(err)
    assert "bg_tasks.db not found" in payload["error"]


def test_list_missing_db_returns_json_error(bg_home, tmp_path):
    rc, _, err = _run_cli(
        ["bg", "list"],
        env_overrides={"HOME": str(tmp_path)},
    )
    assert rc == 1
    payload = json.loads(err)
    assert "bg_tasks.db not found" in payload["error"]


def test_cancel_missing_db_returns_json_error(bg_home, tmp_path):
    rc, _, err = _run_cli(
        ["bg", "cancel", "deadbeefcafefacefeedfacecafef00d0"],
        env_overrides={"HOME": str(tmp_path)},
    )
    assert rc == 1
    payload = json.loads(err)
    assert "bg_tasks.db not found" in payload["error"]


def test_enqueue_env_overlay_parsed(bg_home, tmp_path):
    rc, out, _ = _run_cli(
        ["bg", "enqueue", "--chat-id", "oc_x",
         "--on-done-prompt", "p",
         "--env", "FOO=bar",
         "--env", "BAZ=qux=extra",  # '=' in value should survive
         "--", "echo", "hi"],
        env_overrides={"HOME": str(tmp_path)},
        expect_ok=True,
    )
    tid = json.loads(out)["task_id"]
    conn = connect(bg_home / "bg_tasks.db")
    row = BgTaskRepo(conn).get(tid)
    assert row.env_overlay == {"FOO": "bar", "BAZ": "qux=extra"}
    conn.close()


def test_enqueue_env_without_equals_rejected(bg_home, tmp_path):
    rc, _, err = _run_cli(
        ["bg", "enqueue", "--chat-id", "oc_x",
         "--on-done-prompt", "p",
         "--env", "NOEQUALS",
         "--", "echo", "hi"],
        env_overrides={"HOME": str(tmp_path)},
    )
    assert rc == 2
    assert "KEY=VAL" in err


def test_enqueue_succeeds_without_bridge_listening(bg_home, tmp_path):
    """Fail-open nudge: no UDS listener → enqueue still wins."""
    # No bridge is running in the test env, so wake.sock doesn't exist.
    rc, out, _ = _run_cli(
        ["bg", "enqueue", "--chat-id", "oc_x",
         "--on-done-prompt", "p", "--", "echo", "hi"],
        env_overrides={"HOME": str(tmp_path)},
        expect_ok=True,
    )
    payload = json.loads(out)
    assert payload["state"] == "queued"


# ---------------------------------------------------------------------------
# 3.3 — status / list / cancel
# ---------------------------------------------------------------------------

def _enqueue_one(tmp_path) -> str:
    rc, out, _ = _run_cli(
        ["bg", "enqueue", "--chat-id", "oc_x",
         "--on-done-prompt", "p", "--", "echo", "hi"],
        env_overrides={"HOME": str(tmp_path)},
        expect_ok=True,
    )
    return json.loads(out)["task_id"]


def test_status_reports_queued_row(bg_home, tmp_path):
    tid = _enqueue_one(tmp_path)
    rc, out, _ = _run_cli(
        ["bg", "status", tid],
        env_overrides={"HOME": str(tmp_path)},
        expect_ok=True,
    )
    row = json.loads(out)
    assert row["id"] == tid
    assert row["state"] == "queued"
    assert row["command_argv"] == ["echo", "hi"]


def test_status_unknown_task_exits_1(bg_home, tmp_path):
    rc, _, err = _run_cli(
        ["bg", "status", "deadbeef" * 4],
        env_overrides={"HOME": str(tmp_path)},
    )
    assert rc == 1
    assert "not found" in err


def test_list_orders_by_updated_at_desc(bg_home, tmp_path):
    t1 = _enqueue_one(tmp_path)
    # Bump updated_at on t1 so t2 (newer) still appears first.
    t2 = _enqueue_one(tmp_path)

    rc, out, _ = _run_cli(
        ["bg", "list", "--limit", "10"],
        env_overrides={"HOME": str(tmp_path)},
        expect_ok=True,
    )
    rows = json.loads(out)["tasks"]
    ids = [r["id"] for r in rows]
    assert ids[0] == t2  # newest first
    assert t1 in ids


def test_list_filters_by_chat_and_state(bg_home, tmp_path):
    _enqueue_one(tmp_path)
    rc, out, _ = _run_cli(
        ["bg", "list", "--chat-id", "oc_x", "--state", "queued"],
        env_overrides={"HOME": str(tmp_path)},
        expect_ok=True,
    )
    rows = json.loads(out)["tasks"]
    assert len(rows) >= 1
    assert all(r["chat_id"] == "oc_x" and r["state"] == "queued" for r in rows)


def test_cancel_sets_flag_and_preserves_state(bg_home, tmp_path):
    tid = _enqueue_one(tmp_path)
    rc, out, _ = _run_cli(
        ["bg", "cancel", tid],
        env_overrides={"HOME": str(tmp_path)},
        expect_ok=True,
    )
    assert json.loads(out)["cancel_requested"] is True

    conn = connect(bg_home / "bg_tasks.db")
    row = BgTaskRepo(conn).get(tid)
    assert row.state == "queued"  # state untouched
    assert row.cancel_requested_at is not None
    conn.close()


def test_cancel_terminal_task_refuses(bg_home, tmp_path):
    tid = _enqueue_one(tmp_path)
    # Force row into a terminal state directly.
    conn = connect(bg_home / "bg_tasks.db")
    conn.execute(
        "UPDATE bg_tasks SET state='completed' WHERE id=?", (tid,),
    )
    conn.commit()
    conn.close()

    rc, _, err = _run_cli(
        ["bg", "cancel", tid],
        env_overrides={"HOME": str(tmp_path)},
    )
    assert rc == 1
    assert "terminal" in err


def test_cancel_unknown_task_exits_1(bg_home, tmp_path):
    rc, _, err = _run_cli(
        ["bg", "cancel", "abcdef" * 6],
        env_overrides={"HOME": str(tmp_path)},
    )
    assert rc == 1
    assert "not found" in err


# ---------------------------------------------------------------------------
# 3.4 — UDS nudge fail-open + listener interaction
# ---------------------------------------------------------------------------

def test_nudge_delivered_to_listening_socket():
    """With a socket listener present, enqueue delivers \\x01 ping.

    Uses a /tmp-rooted short path: macOS AF_UNIX cap is 104 bytes and
    pytest's tmp_path regularly exceeds that.
    """
    import tempfile, shutil
    short_home = Path(tempfile.mkdtemp(dir="/tmp", prefix="fb-bg-"))
    try:
        bg_home = short_home / ".feishu-bridge"
        bg_home.mkdir(parents=True, exist_ok=True)
        os.chmod(bg_home, 0o700)
        sock_path = bg_home / "wake.sock"

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        srv.listen(1)
        srv.settimeout(5.0)

        received: list[bytes] = []
        def _accept():
            try:
                conn, _ = srv.accept()
                with conn:
                    data = conn.recv(64)
                    received.append(data)
            except OSError:
                pass

        t = threading.Thread(target=_accept, daemon=True)
        t.start()

        try:
            rc, out, _ = _run_cli(
                ["bg", "enqueue", "--chat-id", "oc_x",
                 "--on-done-prompt", "p", "--", "echo", "hi"],
                env_overrides={"HOME": str(short_home)},
                expect_ok=True,
            )
        finally:
            srv.close()

        t.join(timeout=3.0)
        assert received and received[0].startswith(b"\x01")
    finally:
        shutil.rmtree(short_home, ignore_errors=True)
