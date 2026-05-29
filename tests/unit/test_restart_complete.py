"""Unit tests for _notify_restart_complete: the post-restart card patch.

Regression coverage for the proxy-503-at-boot failure that left the card
stuck on "正在重启..." — the patch must retry transient failures and must not
delay the restart-state cleanup.
"""

import json
import threading
import types
from pathlib import Path

import feishu_bridge.main as main


def _wait_until(pred, timeout=5.0):
    import time as _t
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        if pred():
            return True
        _t.sleep(0.005)
    return pred()


def _patch_thread_running():
    return any(t.name == "restart-complete" for t in threading.enumerate())


class _Resp:
    def __init__(self, ok, code=0, msg="ok"):
        self._ok = ok
        self.code = code
        self.msg = msg

    def success(self):
        return self._ok


def _make_bot(tmp_path, patch_fn):
    message = types.SimpleNamespace(patch=patch_fn)
    v1 = types.SimpleNamespace(message=message)
    im = types.SimpleNamespace(v1=v1)
    lark_client = types.SimpleNamespace(im=im)
    return types.SimpleNamespace(
        workspace=str(tmp_path), bot_id="testbot", lark_client=lark_client)


def _write_restart_file(tmp_path, bot_id="testbot", **payload):
    state_dir = Path(tmp_path) / "state" / "feishu-bridge"
    state_dir.mkdir(parents=True, exist_ok=True)
    f = state_dir / f"restart-{bot_id}.json"
    f.write_text(json.dumps({"message_id": "om_test", **payload}))
    return f


def test_retries_transient_failure_then_succeeds(tmp_path, monkeypatch):
    """A proxy 503 (raised exception) on the first attempts must not give up;
    the card is patched once outbound recovers."""
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def patch(req):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("Tunnel connection failed: 503")
        return _Resp(True)

    bot = _make_bot(tmp_path, patch)
    rf = _write_restart_file(tmp_path)

    main._notify_restart_complete(bot)
    assert _wait_until(lambda: calls["n"] == 3)
    assert not rf.exists()  # state file consumed regardless


def test_state_file_removed_before_thread_completes(tmp_path, monkeypatch):
    """Cleanup of the single-use state file happens synchronously, not gated
    on the (possibly slow / failing) patch."""
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)
    started = threading.Event()
    release = threading.Event()

    def patch(req):
        started.set()
        release.wait(2.0)
        return _Resp(True)

    bot = _make_bot(tmp_path, patch)
    rf = _write_restart_file(tmp_path)

    main._notify_restart_complete(bot)
    assert started.wait(2.0)
    assert not rf.exists()  # already gone while patch thread is still running
    release.set()
    _wait_until(lambda: not _patch_thread_running())


def test_gives_up_after_bounded_retries(tmp_path, monkeypatch):
    """Persistent failure must not loop forever; bounded at 5 attempts."""
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def patch(req):
        calls["n"] += 1
        return _Resp(False, code=503, msg="Service Unavailable")

    bot = _make_bot(tmp_path, patch)
    _write_restart_file(tmp_path)

    main._notify_restart_complete(bot)
    assert _wait_until(lambda: calls["n"] == 5)
    # bounded — never exceeds the cap even given more time
    assert not _wait_until(lambda: calls["n"] > 5, timeout=0.1)


def test_no_restart_file_is_noop(tmp_path):
    """Absent state file: no patch attempt, no thread."""
    calls = {"n": 0}

    def patch(req):
        calls["n"] += 1
        return _Resp(True)

    bot = _make_bot(tmp_path, patch)
    main._notify_restart_complete(bot)
    assert calls["n"] == 0
    assert not _patch_thread_running()


def test_missing_message_id_skips_patch(tmp_path):
    """State file without a message_id: cleaned up, no patch attempt."""
    calls = {"n": 0}

    def patch(req):
        calls["n"] += 1
        return _Resp(True)

    bot = _make_bot(tmp_path, patch)
    state_dir = Path(tmp_path) / "state" / "feishu-bridge"
    state_dir.mkdir(parents=True, exist_ok=True)
    rf = state_dir / "restart-testbot.json"
    rf.write_text(json.dumps({"version": "1.2.3"}))

    main._notify_restart_complete(bot)
    assert calls["n"] == 0
    assert not rf.exists()
