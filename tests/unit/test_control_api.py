"""Unit tests for the Control API (Unix socket JSON-RPC server)."""

import json
import logging
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers: build a minimal fake FeishuBot with the shapes control_api reads
# ---------------------------------------------------------------------------

def _make_fake_bot(tmpdir: Path):
    """Build a lightweight FeishuBot stand-in for the Control API."""
    from collections import deque
    import queue as _queue

    bot = SimpleNamespace()
    bot.bot_id = "test-bot"
    bot.workspace = str(tmpdir)
    bot.bot_config = {
        "name": "test-bot",
        "app_id": "cli_xxx",
        "app_secret": "s3cret_value",
        "workspace": str(tmpdir),
    }
    bot.agent_config = {
        "type": "claude",
        "providers": {"default": {}, "omlx": {"api_key": "key123"}},
    }
    bot._state_lock = threading.RLock()
    bot._start_time = time.time()

    # -- SessionMap stand-in --
    sm = SimpleNamespace()
    sm._lock = threading.RLock()
    sm._AGENT_TYPE_KEY = "_agent_type"
    sm._data = {
        "_agent_type": "claude",
        "test-bot:oc_abc:": "sess-001",
        "test-bot:oc_def:thread1": "sess-002",
    }
    bot.session_map = sm

    # -- ChatTaskQueue stand-in --
    cq = SimpleNamespace()
    cq._lock = threading.Lock()
    cq._pending = {"test-bot:oc_abc:": deque(["item1"])}
    cq._active = {"test-bot:oc_abc:"}
    bot._chat_queue = cq

    # -- QuotaPoller stand-in --
    from feishu_bridge.quota import QuotaSnapshot, QuotaWindow
    snap = QuotaSnapshot(
        timestamp=time.time(),
        windows={
            "five_hour": QuotaWindow(utilization=12.5, resets_at="2026-05-24T18:00:00Z"),
            "seven_day": QuotaWindow(utilization=45.2, resets_at="2026-05-28T00:00:00Z"),
        },
        extra_usage_enabled=True,
    )
    qp = SimpleNamespace()
    qp.snapshot = snap
    bot._quota_poller = qp

    # -- Runner stand-in --
    runner = MagicMock()
    runner.cancel.return_value = True
    runner.supports_compact.return_value = True
    bot.runner = runner

    # -- Model status --
    bot.get_model_status = MagicMock(return_value=("claude-sonnet-4-20250514", False))
    bot.switch_provider = MagicMock(return_value=(True, "Provider 已切换为 `omlx`。"))
    bot.set_model = MagicMock(return_value=("claude-opus-4-6", False))
    bot.switch_agent = MagicMock(return_value=(True, "Agent 已切换为 `codex`。", "/usr/local/bin/codex"))

    # -- bg_supervisor --
    bot._bg_supervisor = None

    return bot


def _rpc(sock_path: str, token: str, method: str, params: dict | None = None,
         req_id: int = 1) -> dict:
    """Send a single JSON-RPC request and return the parsed response."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(sock_path)
        req = json.dumps({
            "method": method,
            "params": params or {},
            "id": req_id,
            "token": token,
        }) + "\n"
        s.sendall(req.encode("utf-8"))
        # Read response (single line)
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.strip())
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def control_env(tmp_path):
    """Start a ControlAPI server with a fake bot; yield (sock_path, token, bot)."""
    import tempfile
    from feishu_bridge.control_api import ControlAPI
    from feishu_bridge.log_buffer import LogRingBuffer

    bot = _make_fake_bot(tmp_path)

    # Use a short /tmp dir for the socket — AF_UNIX path limit is 104 bytes on macOS
    short_dir = tempfile.mkdtemp(prefix="fb_")
    sock_path = Path(short_dir) / "ctrl.sock"
    token_path = Path(short_dir) / "ctrl.token"

    log_buf = LogRingBuffer(capacity=100)
    log_buf.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logging.getLogger("feishu-bridge").addHandler(log_buf)

    api = ControlAPI(bot, sock_path, token_path, log_buffer=log_buf)
    api.start()

    # Read auto-generated token
    token = token_path.read_text().strip()

    yield str(sock_path), token, bot

    api.stop()
    logging.getLogger("feishu-bridge").removeHandler(log_buf)
    # Cleanup short dir
    import shutil
    shutil.rmtree(short_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_ok(self, control_env):
        sock, token, _bot = control_env
        resp = _rpc(sock, token, "health")
        assert resp["result"]["ok"] is True
        assert resp["id"] == 1
        assert resp["api_version"] == 1
        assert "capabilities" in resp


class TestAuth:
    def test_bad_token_rejected(self, control_env):
        sock, _token, _bot = control_env
        resp = _rpc(sock, "wrong-token", "health")
        assert "error" in resp
        assert resp["error"]["code"] == 401

    def test_missing_token_rejected(self, control_env):
        sock, _token, _bot = control_env
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(sock)
            req = json.dumps({"method": "health", "params": {}, "id": 1}) + "\n"
            s.sendall(req.encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            resp = json.loads(buf.strip())
            assert resp["error"]["code"] == 401
        finally:
            s.close()


class TestStatus:
    def test_status_fields(self, control_env):
        sock, token, _bot = control_env
        resp = _rpc(sock, token, "status")
        r = resp["result"]
        assert r["version"]
        assert r["uptime_seconds"] >= 0
        assert r["agent"]["type"] == "claude"
        assert r["agent"]["provider"] == "default"
        assert r["bot"]["name"] == "test-bot"
        assert r["sessions"]["active_count"] == 2
        assert len(r["sessions"]["keys"]) == 2
        assert r["queue"]["pending_total"] == 1
        assert r["quota"]["available"] is True
        assert "default" in r["providers"]
        assert "claude" in r["agents"]


class TestConfig:
    def test_config_masks_secrets(self, control_env):
        sock, token, _bot = control_env
        resp = _rpc(sock, token, "config")
        r = resp["result"]
        assert r["bot"]["app_secret"] == "***"
        # Provider key should be masked
        omlx = r["agent"]["providers"]["omlx"]
        assert omlx.get("api_key") == "***"


class TestSessions:
    def test_sessions_list(self, control_env):
        sock, token, _bot = control_env
        resp = _rpc(sock, token, "sessions")
        sessions = resp["result"]["sessions"]
        assert len(sessions) == 2
        keys = [s["session_key"] for s in sessions]
        assert "test-bot:oc_abc:" in keys


class TestQuota:
    def test_quota_snapshot(self, control_env):
        sock, token, _bot = control_env
        resp = _rpc(sock, token, "quota")
        r = resp["result"]
        assert r["available"] is True
        assert r["windows"]["five_hour"]["utilization"] == 12.5


class TestLogs:
    def test_logs_returns_entries(self, control_env):
        sock, token, _bot = control_env
        # Emit a log line to the buffer
        logger = logging.getLogger("feishu-bridge")
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.info("test log entry for control api")
        logger.setLevel(old_level)
        resp = _rpc(sock, token, "logs", {"n": 10, "level": "INFO"})
        entries = resp["result"]["entries"]
        assert any("test log entry" in e["msg"] for e in entries)


class TestWriteMethods:
    def test_set_provider(self, control_env):
        sock, token, bot = control_env
        resp = _rpc(sock, token, "set_provider", {"name": "omlx"})
        assert resp["result"]["ok"] is True
        bot.switch_provider.assert_called_once_with("omlx")

    def test_set_model(self, control_env):
        sock, token, bot = control_env
        resp = _rpc(sock, token, "set_model", {"name": "opus"})
        assert resp["result"]["ok"] is True
        bot.set_model.assert_called_once_with("opus")

    def test_set_agent(self, control_env):
        sock, token, bot = control_env
        resp = _rpc(sock, token, "set_agent", {"type": "codex"})
        assert resp["result"]["ok"] is True
        bot.switch_agent.assert_called_once_with("codex")

    def test_set_provider_missing_param(self, control_env):
        sock, token, _bot = control_env
        resp = _rpc(sock, token, "set_provider", {})
        assert resp["error"]["code"] == 400

    def test_set_provider_failure(self, control_env):
        sock, token, bot = control_env
        bot.switch_provider.return_value = (False, "未知 Provider")
        resp = _rpc(sock, token, "set_provider", {"name": "bad"})
        assert resp["error"]["code"] == 400
        assert "未知" in resp["error"]["message"]


class TestSessionMethods:
    def test_stop_session(self, control_env):
        sock, token, bot = control_env
        resp = _rpc(sock, token, "stop_session",
                     {"session_key": "test-bot:oc_abc:"})
        assert resp["result"]["ok"] is True
        assert resp["result"]["cancelled"] is True
        bot.runner.cancel.assert_called_once_with("test-bot:oc_abc:")

    def test_stop_session_bad_format(self, control_env):
        sock, token, _bot = control_env
        resp = _rpc(sock, token, "stop_session",
                     {"session_key": "bad-key"})
        assert resp["error"]["code"] == 400


class TestUnknownMethod:
    def test_unknown_method_404(self, control_env):
        sock, token, _bot = control_env
        resp = _rpc(sock, token, "nonexistent_method")
        assert resp["error"]["code"] == 404


class TestTokenPersistence:
    def test_token_created_and_reusable(self, tmp_path):
        """Token file is created on first start and reused on second."""
        from feishu_bridge.control_api import load_or_create_token

        token_path = tmp_path / "test.token"
        t1 = load_or_create_token(token_path)
        assert len(t1) > 10
        assert token_path.exists()
        # File permissions
        mode = oct(token_path.stat().st_mode & 0o777)
        assert mode == "0o600"
        # Second call returns same token
        t2 = load_or_create_token(token_path)
        assert t1 == t2


class TestLogRingBuffer:
    def test_ring_buffer_emit_and_recent(self):
        from feishu_bridge.log_buffer import LogRingBuffer

        buf = LogRingBuffer(capacity=5)
        buf.setFormatter(logging.Formatter("%(message)s"))

        logger = logging.getLogger("test-ring-buf")
        logger.addHandler(buf)
        logger.setLevel(logging.DEBUG)

        for i in range(7):
            logger.info("msg-%d", i)

        entries = buf.recent(n=10, level="INFO")
        # Capacity is 5, so oldest 2 should be evicted
        assert len(entries) == 5
        assert entries[0]["msg"] == "msg-2"
        assert entries[-1]["msg"] == "msg-6"

        logger.removeHandler(buf)

    def test_ring_buffer_level_filter(self):
        from feishu_bridge.log_buffer import LogRingBuffer

        buf = LogRingBuffer(capacity=100)
        buf.setFormatter(logging.Formatter("%(message)s"))

        logger = logging.getLogger("test-ring-level")
        logger.addHandler(buf)
        logger.setLevel(logging.DEBUG)

        logger.debug("dbg")
        logger.info("inf")
        logger.warning("wrn")
        logger.error("err")

        warnings = buf.recent(level="WARNING")
        assert len(warnings) == 2
        assert warnings[0]["msg"] == "wrn"
        assert warnings[1]["msg"] == "err"

        logger.removeHandler(buf)


class TestMultipleRequests:
    def test_multiple_requests_single_connection(self, control_env):
        """Multiple JSON-RPC requests on one persistent connection."""
        sock_path, token, _bot = control_env
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(sock_path)
            for i in range(3):
                req = json.dumps({
                    "method": "health",
                    "params": {},
                    "id": i + 1,
                    "token": token,
                }) + "\n"
                s.sendall(req.encode())
                buf = b""
                while b"\n" not in buf:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                resp = json.loads(buf.strip())
                assert resp["result"]["ok"] is True
                assert resp["id"] == i + 1
        finally:
            s.close()
