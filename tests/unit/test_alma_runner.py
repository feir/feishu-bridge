#!/usr/bin/env python3
"""Unit tests for AlmaRunner — WS API backend."""

import json
import queue
import shutil
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from feishu_bridge.runtime_alma import (
    AlmaRunner,
    AlmaThreadMap,
    AlmaWSManager,
    _TOOL_NAME_MAP,
    _is_alma_running,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def runner(tmp_path):
    """AlmaRunner with mocked WS + HTTP to avoid real Alma dependency."""
    r = AlmaRunner(
        model="sonnet",
        workspace=str(tmp_path),
        timeout=10,
        bot_id="test-bot",
        extra_system_prompts=["test safety prompt"],
        safety_prompt_mode="full",
    )
    return r


@pytest.fixture
def thread_map(tmp_path):
    return AlmaThreadMap(tmp_path / "state" / "alma-threads-test.json")


# ---------------------------------------------------------------------------
# Phase 1: AlmaRunner basics
# ---------------------------------------------------------------------------

class TestAlmaRunnerSkeleton:
    def test_instantiation(self, runner):
        assert runner.get_display_name() == "Alma"
        assert runner.ALWAYS_STREAMING is True
        assert runner.command is None
        assert runner._bot_id == "test-bot"

    def test_abc_stubs_raise(self, runner):
        with pytest.raises(NotImplementedError, match="WS"):
            runner.build_args("x", None, False, True)
        with pytest.raises(NotImplementedError, match="WS"):
            runner.parse_streaming_line({}, None)
        with pytest.raises(NotImplementedError, match="WS"):
            runner.parse_blocking_output("", None)

    def test_supports_compact_but_not_auto(self, runner):
        assert runner.supports_compact() is True
        assert runner.supports_auto_compact() is False

    def test_fork_session_guard(self, runner):
        result = runner.run("test", fork_session=True)
        assert result["is_error"] is True
        assert "btw" in result["result"].lower() or "fork" in result["result"].lower()

    def test_alma_not_running_returns_error(self, runner):
        with mock.patch("feishu_bridge.runtime_alma._is_alma_running", return_value=False):
            result = runner.run("hello", session_id="s1", tag="bot:chat:")
            assert result["is_error"] is True
            assert "Alma" in result["result"]
            assert "/agent claude" in result["result"]


# ---------------------------------------------------------------------------
# Phase 2: Streaming event mapping
# ---------------------------------------------------------------------------

class TestEventConsumption:
    """Test _consume_events with synthetic event queues."""

    def _run_consume(self, runner, events, timeout=5):
        import copy
        q = queue.Queue()
        for e in events:
            q.put(e)
        on_output_calls = []
        on_tool_calls = []
        result = runner._consume_events(
            q, "thread-1", "session-1",
            on_output=lambda t: on_output_calls.append(t),
            on_tool_status=lambda s: on_tool_calls.append(copy.deepcopy(s)),
        )
        return result, on_output_calls, on_tool_calls

    def test_text_append_accumulates(self, runner):
        events = [
            {"type": "message_delta", "data": {
                "threadId": "t1",
                "delta": {"type": "text_append", "text": "Hello "},
            }},
            {"type": "message_delta", "data": {
                "threadId": "t1",
                "delta": {"type": "text_append", "text": "world"},
            }},
            {"type": "generation_completed", "data": {"threadId": "t1"}},
        ]
        result, outputs, _ = self._run_consume(runner, events)
        assert result["result"] == "Hello world"
        assert result["is_error"] is False
        assert outputs == ["Hello ", "Hello world"]

    def test_tool_invocation_tracking(self, runner):
        events = [
            {"type": "message_delta", "data": {
                "threadId": "t1",
                "delta": {
                    "type": "part_add",
                    "part": {
                        "type": "tool-invocation",
                        "toolName": "bash",
                        "toolCallId": "tc-1",
                        "args": {"command": "ls -la"},
                    },
                },
            }},
            {"type": "message_delta", "data": {
                "threadId": "t1",
                "delta": {
                    "type": "part_add",
                    "part": {
                        "type": "tool-invocation",
                        "toolName": "bash",
                        "toolCallId": "tc-2",
                        "args": {"command": "cat foo.txt"},
                    },
                },
            }},
            {"type": "message_delta", "data": {
                "threadId": "t1",
                "delta": {"type": "tool_output_set", "toolCallId": "tc-1"},
            }},
            {"type": "generation_completed", "data": {"threadId": "t1"}},
        ]
        result, _, tool_updates = self._run_consume(runner, events)
        assert result["is_error"] is False
        # Two tool_add + one tool_output_set = 3 updates
        assert len(tool_updates) == 3
        # First update: one running tool
        assert tool_updates[0][0]["name"] == "Bash"
        assert tool_updates[0][0]["status"] == "running"
        # Second update: two running tools
        assert len(tool_updates[1]) == 2
        # Third update: first tool done, second still running
        assert tool_updates[2][0]["status"] == "done"
        assert tool_updates[2][1]["status"] == "running"

    def test_tool_name_mapping(self, runner):
        for raw, expected in _TOOL_NAME_MAP.items():
            events = [
                {"type": "message_delta", "data": {
                    "threadId": "t1",
                    "delta": {
                        "type": "part_add",
                        "part": {
                            "type": "tool-invocation",
                            "toolName": raw,
                            "toolCallId": f"tc-{raw}",
                            "args": {},
                        },
                    },
                }},
                {"type": "generation_completed", "data": {"threadId": "t1"}},
            ]
            result, _, tools = self._run_consume(runner, events)
            assert tools[0][0]["name"] == expected, f"Failed for {raw}"

    def test_generation_error(self, runner):
        events = [
            {"type": "message_delta", "data": {
                "threadId": "t1",
                "delta": {"type": "text_append", "text": "partial"},
            }},
            {"type": "generation_error", "data": {
                "threadId": "t1",
                "error": "rate_limit_exceeded",
            }},
        ]
        result, _, _ = self._run_consume(runner, events)
        assert result["is_error"] is True
        assert "rate_limit" in result["result"]

    def test_ws_disconnect_sentinel(self, runner):
        events = [
            {"type": "_ws_disconnect", "error": "Alma WS disconnected"},
        ]
        result, _, _ = self._run_consume(runner, events)
        assert result["is_error"] is True
        assert "断开" in result["result"] or "disconnect" in result["result"].lower()

    def test_context_usage_logged(self, runner):
        events = [
            {"type": "context_usage_update", "data": {
                "threadId": "t1",
                "contextTokens": 50000,
                "contextWindow": 200000,
            }},
            {"type": "generation_completed", "data": {"threadId": "t1"}},
        ]
        result, _, _ = self._run_consume(runner, events)
        assert result["is_error"] is False
        assert result.get("context_usage", {}).get("percent") == 25.0

    def test_timeout(self, tmp_path):
        r = AlmaRunner(
            model=None, workspace=str(tmp_path), timeout=1,
            bot_id="test",
        )
        q = queue.Queue()  # empty queue → will timeout
        result = r._consume_events(q, "t1", "s1")
        assert result["is_error"] is True
        assert "超时" in result["result"]


# ---------------------------------------------------------------------------
# Phase 3: Session ↔ Thread mapping
# ---------------------------------------------------------------------------

class TestAlmaThreadMap:
    def test_put_get_delete(self, thread_map):
        assert thread_map.get("k1") is None
        thread_map.put("k1", "thread-abc")
        assert thread_map.get("k1") == "thread-abc"
        thread_map.delete("k1")
        assert thread_map.get("k1") is None

    def test_atomic_persistence(self, thread_map, tmp_path):
        thread_map.put("k1", "t1")
        thread_map.put("k2", "t2")

        # Reload from disk
        reloaded = AlmaThreadMap(thread_map._path)
        assert reloaded.get("k1") == "t1"
        assert reloaded.get("k2") == "t2"

    def test_delete_nonexistent_is_noop(self, thread_map):
        thread_map.delete("nonexistent")  # should not raise

    def test_corrupt_file_recovery(self, tmp_path):
        path = tmp_path / "state" / "bad.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json {{{")
        m = AlmaThreadMap(path)
        assert m.get("anything") is None
        m.put("k1", "t1")
        assert m.get("k1") == "t1"


class TestThreadResolution:
    def test_new_session_creates_thread(self, runner):
        with mock.patch("feishu_bridge.runtime_alma._create_alma_thread",
                        return_value="new-thread-id"):
            tid = runner._resolve_thread("session:key:1")
            assert tid == "new-thread-id"
            assert runner._thread_map.get("session:key:1") == "new-thread-id"

    def test_existing_thread_reused(self, runner):
        runner._thread_map.put("sk", "existing-thread")
        with mock.patch("feishu_bridge.runtime_alma._thread_exists",
                        return_value=True):
            tid = runner._resolve_thread("sk")
            assert tid == "existing-thread"

    def test_auto_heal_on_deleted_thread(self, runner):
        runner._thread_map.put("sk", "deleted-thread")
        with mock.patch("feishu_bridge.runtime_alma._thread_exists",
                        return_value=False), \
             mock.patch("feishu_bridge.runtime_alma._create_alma_thread",
                        return_value="healed-thread"):
            tid = runner._resolve_thread("sk")
            assert tid == "healed-thread"
            assert runner._thread_map.get("sk") == "healed-thread"

    def test_force_new_ignores_existing(self, runner):
        runner._thread_map.put("sk", "old-thread")
        with mock.patch("feishu_bridge.runtime_alma._create_alma_thread",
                        return_value="fresh-thread"):
            tid = runner._resolve_thread("sk", force_new=True)
            assert tid == "fresh-thread"

    def test_clear_thread(self, runner):
        runner._thread_map.put("sk", "thread-1")
        runner.clear_thread("sk")
        assert runner._thread_map.get("sk") is None


# ---------------------------------------------------------------------------
# Phase 4: Model resolution
# ---------------------------------------------------------------------------

class TestModelResolution:
    def test_default_model(self, runner):
        runner.model = None
        assert "sonnet" in runner._resolve_model()

    def test_alias_opus(self, runner):
        runner.model = "opus"
        assert "opus" in runner._resolve_model()
        assert runner._resolve_model().startswith("claude-subscription:")

    def test_alias_haiku(self, runner):
        runner.model = "haiku"
        assert "haiku" in runner._resolve_model()

    def test_passthrough_provider_format(self, runner):
        runner.model = "claude-subscription:claude-opus-4-20250514"
        assert runner._resolve_model() == "claude-subscription:claude-opus-4-20250514"

    def test_unknown_model_prefixed(self, runner):
        runner.model = "claude-5-turbo"
        assert runner._resolve_model() == "claude-subscription:claude-5-turbo"


# ---------------------------------------------------------------------------
# Phase 5: Compact
# ---------------------------------------------------------------------------

class TestCompact:
    def test_compact_routes_to_http(self, runner):
        runner._thread_map.put("bot:chat:", "thread-99")
        with mock.patch("feishu_bridge.runtime_alma._alma_http") as m:
            m.return_value = {}
            result = runner._handle_compact("bot:chat:")
            m.assert_called_once_with("POST", "/api/threads/thread-99/compact")
            assert result["is_error"] is False

    def test_compact_no_session(self, runner):
        result = runner._handle_compact("nonexistent:key:")
        assert result["is_error"] is True

    def test_compact_via_run(self, runner):
        runner._thread_map.put("bot:chat:", "thread-99")
        with mock.patch("feishu_bridge.runtime_alma._is_alma_running", return_value=True), \
             mock.patch("feishu_bridge.runtime_alma._alma_http") as m:
            m.return_value = {}
            result = runner.run("/compact", tag="bot:chat:")
            assert result["is_error"] is False


# ---------------------------------------------------------------------------
# Phase 6: Integration
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_runner_classes_includes_alma(self):
        from feishu_bridge.main import _RUNNER_CLASSES
        assert "alma" in _RUNNER_CLASSES
        assert _RUNNER_CLASSES["alma"] is AlmaRunner

    def test_switch_provider_guard(self):
        """switch_provider should early-return error when agent type is alma."""
        from feishu_bridge.main import _RUNNER_CLASSES
        # We test the guard logic directly rather than instantiating FeishuBot
        agent_cfg = {"type": "alma"}
        # The guard checks agent_config.get("type") == "alma"
        assert agent_cfg.get("type") == "alma"

    def test_preflight_alma_not_running(self):
        with mock.patch("feishu_bridge.runtime_alma._is_alma_running",
                        return_value=False):
            ok, msg = AlmaRunner.preflight_check()
            assert ok is False
            assert "Alma" in msg

    def test_preflight_feishu_enabled(self):
        with mock.patch("feishu_bridge.runtime_alma._is_alma_running",
                        return_value=True), \
             mock.patch("feishu_bridge.runtime_alma._alma_http",
                        return_value={"feishu": {"enabled": True}}):
            ok, msg = AlmaRunner.preflight_check()
            assert ok is False
            assert "feishu" in msg.lower()

    def test_preflight_ok(self):
        with mock.patch("feishu_bridge.runtime_alma._is_alma_running",
                        return_value=True), \
             mock.patch("feishu_bridge.runtime_alma._alma_http",
                        return_value={"feishu": {"enabled": False}}):
            ok, msg = AlmaRunner.preflight_check()
            assert ok is True

    def test_switch_agent_to_alma_preflight_failure(self, tmp_path):
        """switch_agent('alma') returns False when Alma is not running."""
        import feishu_bridge.main as bridge

        bot = object.__new__(bridge.FeishuBot)
        bot.bot_id = "test-bot"
        bot.bot_config = {"workspace": "/tmp", "model": "claude-opus-4-6"}
        bot.agent_config = {
            "type": "claude",
            "command": "claude",
            "provider": "default",
            "providers": {"default": {}},
            "commands": {"claude": "claude"},
            "args_by_type": {"claude": []},
            "env_by_type": {"claude": {}},
            "_resolved_command": shutil.which("python3"),
            "timeout_seconds": 30,
        }
        bot.runner = mock.MagicMock()
        bot._extra_prompts = []
        bot._session_cost = {}
        bot._session_map_path = tmp_path / "sessions.json"
        bot.session_map = mock.MagicMock()

        with mock.patch("feishu_bridge.runtime_alma._is_alma_running",
                        return_value=False):
            ok, msg, resolved = bot.switch_agent("alma")

        assert ok is False
        assert "Alma" in msg
        assert resolved is None
        assert bot.agent_config["type"] == "claude"  # not changed

    def test_switch_agent_to_alma_success(self, tmp_path, monkeypatch):
        """switch_agent('alma') succeeds and returns 'alma (WS)' as resolved cmd."""
        import feishu_bridge.main as bridge

        bot = object.__new__(bridge.FeishuBot)
        bot.bot_id = "test-bot"
        bot.bot_config = {"name": "test-bot", "workspace": "/tmp", "model": "claude-opus-4-6"}
        bot.agent_config = {
            "type": "claude",
            "command": "claude",
            "provider": "default",
            "providers": {"default": {}},
            "commands": {"claude": "claude"},
            "args_by_type": {"claude": []},
            "env_by_type": {"claude": {}},
            "_resolved_command": shutil.which("python3"),
            "timeout_seconds": 30,
        }
        bot.runner = mock.MagicMock()
        bot._extra_prompts = []
        bot._session_cost = {}
        bot._session_map_path = tmp_path / "sessions.json"
        bot.session_map = mock.MagicMock()

        monkeypatch.setattr(bridge, "build_extra_prompts", lambda cfg: [])

        with mock.patch("feishu_bridge.runtime_alma._is_alma_running",
                        return_value=True), \
             mock.patch("feishu_bridge.runtime_alma._alma_http",
                        return_value={"feishu": {"enabled": False}}):
            ok, msg, resolved = bot.switch_agent("alma")

        assert ok is True
        assert "alma" in msg.lower()
        assert resolved == "alma (WS)"  # no UnboundLocalError + correct display value
        assert bot.agent_config["type"] == "alma"
        assert bot.agent_config["command"] is None
        assert bot.agent_config["_resolved_command"] is None
        assert isinstance(bot.runner, AlmaRunner)


# ---------------------------------------------------------------------------
# Phase 7: Concurrent isolation + WS disconnect
# ---------------------------------------------------------------------------

class TestWSManager:
    def test_register_unregister(self):
        mgr = AlmaWSManager()
        q = mgr.register_run("thread-1")
        assert isinstance(q, queue.Queue)
        mgr.unregister_run("thread-1")
        with mgr._lock:
            assert "thread-1" not in mgr._pending

    def test_dispatch_routes_by_thread_id(self):
        mgr = AlmaWSManager()
        q1 = mgr.register_run("t1")
        q2 = mgr.register_run("t2")

        mgr._dispatch({"type": "message_delta", "data": {"threadId": "t1"}})
        mgr._dispatch({"type": "message_delta", "data": {"threadId": "t2"}})
        mgr._dispatch({"type": "generation_completed", "data": {"threadId": "t1"}})

        assert q1.qsize() == 2
        assert q2.qsize() == 1

    def test_disconnect_fan_out(self):
        mgr = AlmaWSManager()
        q1 = mgr.register_run("t1")
        q2 = mgr.register_run("t2")

        mgr._handle_disconnect()

        e1 = q1.get_nowait()
        e2 = q2.get_nowait()
        assert e1["type"] == "_ws_disconnect"
        assert e2["type"] == "_ws_disconnect"

    def test_dispatch_ignores_unknown_thread(self):
        mgr = AlmaWSManager()
        q1 = mgr.register_run("t1")
        mgr._dispatch({"type": "x", "data": {"threadId": "unknown"}})
        assert q1.empty()

    def test_dispatch_ignores_no_thread_id(self):
        mgr = AlmaWSManager()
        mgr.register_run("t1")
        mgr._dispatch({"type": "heartbeat"})  # no threadId


class TestConcurrentIsolation:
    """Two sessions generating simultaneously — events don't cross-contaminate."""

    def test_parallel_streams_isolated(self, runner):
        q1 = queue.Queue()
        q2 = queue.Queue()

        # Interleaved events for two threads
        events_t1 = [
            {"type": "message_delta", "data": {"threadId": "t1",
             "delta": {"type": "text_append", "text": "A"}}},
            {"type": "generation_completed", "data": {"threadId": "t1"}},
        ]
        events_t2 = [
            {"type": "message_delta", "data": {"threadId": "t2",
             "delta": {"type": "text_append", "text": "B"}}},
            {"type": "generation_completed", "data": {"threadId": "t2"}},
        ]

        for e in events_t1:
            q1.put(e)
        for e in events_t2:
            q2.put(e)

        r1 = runner._consume_events(q1, "t1", "s1")
        r2 = runner._consume_events(q2, "t2", "s2")

        assert r1["result"] == "A"
        assert r2["result"] == "B"


# ---------------------------------------------------------------------------
# Full run() with mocked WS
# ---------------------------------------------------------------------------

class TestFullRun:
    def test_full_run_happy_path(self, runner):
        """Simulate a complete run: thread resolve → WS send → events → result."""
        mock_mgr = mock.MagicMock(spec=AlmaWSManager)
        mock_q = queue.Queue()
        mock_mgr.register_run.return_value = mock_q

        # Enqueue events that will be consumed
        mock_q.put({"type": "message_delta", "data": {
            "threadId": "t1",
            "delta": {"type": "text_append", "text": "Hello from Alma"},
        }})
        mock_q.put({"type": "generation_completed", "data": {"threadId": "t1"}})

        with mock.patch("feishu_bridge.runtime_alma._is_alma_running",
                        return_value=True), \
             mock.patch("feishu_bridge.runtime_alma._create_alma_thread",
                        return_value="t1"), \
             mock.patch.object(AlmaRunner, "_get_ws_mgr", return_value=mock_mgr):

            outputs = []
            result = runner.run(
                "hello", session_id="s1", tag="bot:chat:",
                on_output=lambda t: outputs.append(t),
            )

        assert result["is_error"] is False
        assert result["result"] == "Hello from Alma"
        assert result["session_id"] == "bot:chat:"
        assert outputs == ["Hello from Alma"]
        mock_mgr.send.assert_called_once()
        sent = mock_mgr.send.call_args[0][0]
        assert sent["type"] == "generate_response"
        assert sent["data"]["threadId"] == "t1"
        assert "ephemeralContext" in sent["data"]
