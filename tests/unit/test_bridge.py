#!/usr/bin/env python3
"""Unit tests for Feishu Claude bridge task/error handling."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bridge_commands
import bridge_runtime
import bridge_worker
import feishu_auth
import feishu_bridge as bridge
from feishu_api import FeishuAPIError
from feishu_tasks import FeishuTasks


class FakeHandle:
    """Minimal ResponseHandle stub for worker-unit tests."""

    def __init__(self, client, chat_id, thread_id, message_id):
        self.client = client
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.source_message_id = message_id
        self.deliveries = []
        self._terminated = False
        self._card_fallback_timer = None
        self._typing_reaction_id = None

    def send_processing_indicator(self):
        return True

    def stream_update(self, content):
        self.last_stream = content

    def deliver(self, content, is_error=False):
        self.deliveries.append((content, is_error))


class DummySessionMap:
    def __init__(self):
        self.saved = []

    def get(self, key):
        return None

    def put(self, key, session_id):
        self.saved.append((key, session_id))


class DummyRunner:
    def run(self, *args, **kwargs):
        raise AssertionError("runner.run should not be called in this test")


def test_list_all_tasks_result_propagates_auth_failed():
    tasks = object.__new__(FeishuTasks)
    tasks.list_tasks = lambda *args, **kwargs: {"error": "auth_failed"}

    result = tasks.list_all_tasks_result("chat", "user")

    assert result == {"items": [], "error": "auth_failed", "truncated": False}


def test_find_task_by_id_reports_truncation():
    pages = [
        {"items": [{"guid": "g-1", "task_id": "t-1"}], "has_more": True, "page_token": "p2"},
        {"items": [{"guid": "g-2", "task_id": "t-2"}], "has_more": True, "page_token": "p3"},
    ]
    tasks = object.__new__(FeishuTasks)

    def fake_list_tasks(*args, **kwargs):
        return pages.pop(0)

    tasks.list_tasks = fake_list_tasks

    result = tasks.find_task_by_id("chat", "user", "missing", max_pages=2)

    assert result == {"task": None, "error": None, "truncated": True}


def test_task_list_returns_auth_prompt_when_pagination_auth_fails():
    bot = object.__new__(bridge.FeishuBot)

    class FakeTasks:
        @staticmethod
        def list_all_tasks_result(chat_id, sender_id, completed=None):
            return {"items": [], "error": "auth_failed", "truncated": False}

        @staticmethod
        def _auth_failed_message():
            return "🔐 已发送授权卡片，请完成授权后重试。"

    bot.feishu_tasks = FakeTasks()

    result = bot._task_list("", "chat", "sender")

    assert result == "🔐 已发送授权卡片，请完成授权后重试。"


def test_process_message_todo_fallback_reports_truncation(monkeypatch):
    monkeypatch.setattr(bridge, "ResponseHandle", FakeHandle)

    class FakeTasks:
        @staticmethod
        def get_task(chat_id, sender_id, todo_task_id):
            raise FeishuAPIError(404, "not found", "/tasks/missing")

        @staticmethod
        def find_task_by_id(chat_id, sender_id, todo_task_id, completed=None):
            return {"task": None, "error": None, "truncated": True}

        @staticmethod
        def _auth_failed_message():
            return "auth failed"

    item = {
        "bot_id": "bot",
        "chat_id": "chat",
        "thread_id": None,
        "message_id": "mid",
        "sender_id": "ou_xxx",
        "text": "ignored",
        "_todo_task_id": "todo-123",
    }

    handle = bridge.process_message(
        item=item,
        bot_config={"workspace": "/tmp"},
        lark_client=None,
        session_map=DummySessionMap(),
        runner=DummyRunner(),
        feishu_tasks=FakeTasks(),
    )

    assert handle.deliveries == [(
        "未能在搜索上限内定位此任务，请稍后重试，或改用 `/feishu-tasks get <guid>` 直接查询。",
        False,
    )]


def test_save_token_preserves_original_replace_error(monkeypatch, tmp_path):
    monkeypatch.setattr(feishu_auth, "TOKEN_DIR", tmp_path)
    monkeypatch.setattr(feishu_auth, "_derive_key", lambda *_: b"0" * 32)

    original_replace = feishu_auth.os.replace

    def fail_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(feishu_auth.os, "replace", fail_replace)

    with pytest.raises(OSError, match="disk full"):
        feishu_auth.save_token("app", "user", {"access_token": "token"})

    assert list(tmp_path.glob("*.enc")) == []
    assert list(tmp_path.glob("tmp*")) == []
    monkeypatch.setattr(feishu_auth.os, "replace", original_replace)


def test_handle_feishu_service_sheet_uses_feishu_sheets_attr(monkeypatch):
    bot = object.__new__(bridge.FeishuBot)
    bot.feishu_sheets = object()

    handler = bridge_commands.BridgeCommandHandler(bot)

    class InlineThread:
        def __init__(self, target=None, daemon=None, name=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(bridge_commands.threading, "Thread", InlineThread)
    monkeypatch.setattr(
        handler,
        "dispatch_feishu_service",
        lambda service, arg, chat_id, sender_id: f"{service}:{chat_id}:{sender_id}",
    )

    handle = FakeHandle(None, "chat", None, "mid")
    handler.handle_feishu_service(
        {
            "chat_id": "chat",
            "sender_id": "ou_xxx",
            "_cmd_arg": "info token",
        },
        handle,
        "sheet",
    )

    assert handle.deliveries == [("sheet:chat:ou_xxx", False)]


def test_ensure_bridge_modules_available_reports_local_dependency_chain(monkeypatch):
    err = ModuleNotFoundError("No module named 'feishu_api'")
    err.name = "feishu_api"
    monkeypatch.setattr(bridge, "_BRIDGE_MODULE_IMPORT_ERROR", err)

    with pytest.raises(SystemExit, match="不能只单独复制 `feishu_bridge.py` 或仅复制 `bridge_\\*\\.py`"):
        bridge.ensure_bridge_modules_available()


def test_deploy_doc_mentions_bridge_modules():
    deploy_doc = (Path(__file__).resolve().parent / "DEPLOY.md").read_text()

    assert "cp bridge_commands.py ~/.claude/scripts/" in deploy_doc
    assert "cp bridge_parsers.py ~/.claude/scripts/" in deploy_doc
    assert "cp bridge_runtime.py ~/.claude/scripts/" in deploy_doc
    assert "cp bridge_ui.py ~/.claude/scripts/" in deploy_doc
    assert "cp bridge_worker.py ~/.claude/scripts/" in deploy_doc
    assert "cp feishu_api.py ~/.claude/scripts/" in deploy_doc
    assert "cp feishu_auth.py ~/.claude/scripts/" in deploy_doc


def test_streaming_runner_falls_back_to_accumulated_text_when_final_result_empty():
    runner = bridge_runtime.ClaudeRunner(
        command="claude",
        model="claude-opus-4-6",
        workspace="/tmp",
        timeout=30,
    )

    class FakeProc:
        def __init__(self):
            self.stdout = iter(
                [
                    '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"hello"}}}\n',
                    '{"type":"result","result":"","session_id":"sid-123","is_error":false}\n',
                ]
            )
            self.stderr = iter([])
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    streamed = []
    result = runner._run_streaming(
        FakeProc(),
        session_id="sid-123",
        tag=None,
        on_output=streamed.append,
    )

    assert streamed == ["hello"]
    assert result["result"] == "hello"
    assert result["session_id"] == "sid-123"
    assert result["is_error"] is False


def test_streaming_runner_returns_explicit_error_when_no_text_or_result():
    runner = bridge_runtime.ClaudeRunner(
        command="claude",
        model="claude-opus-4-6",
        workspace="/tmp",
        timeout=30,
    )

    class FakeProc:
        def __init__(self):
            self.stdout = iter([])
            self.stderr = iter([])
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    result = runner._run_streaming(
        FakeProc(),
        session_id="sid-456",
        tag=None,
        on_output=lambda _text: None,
    )

    assert result["result"] == bridge_runtime.EMPTY_RESULT_MESSAGE
    assert result["session_id"] == "sid-456"
    assert result["is_error"] is True


def test_blocking_runner_returns_explicit_error_when_result_empty(monkeypatch):
    runner = bridge_runtime.ClaudeRunner(
        command="claude",
        model="claude-opus-4-6",
        workspace="/tmp",
        timeout=30,
    )

    class FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return ('{"result":"","session_id":"sid-789","is_error":false}', "")

    monkeypatch.setattr(runner, "_cleanup_tag", lambda _tag: False)

    result = runner._run_blocking(FakeProc(), session_id="sid-789", tag=None)

    assert result["result"] == bridge_runtime.EMPTY_RESULT_MESSAGE
    assert result["session_id"] == "sid-789"
    assert result["is_error"] is True


def test_runner_passes_prompt_argument_to_claude(monkeypatch):
    runner = bridge_runtime.ClaudeRunner(
        command="claude",
        model="claude-opus-4-6",
        workspace="/tmp",
        timeout=30,
    )

    captured = {}

    class FakeProc:
        def __init__(self):
            self.stdout = iter([])
            self.stderr = iter([])
            self.returncode = 0

        def communicate(self, timeout=None):
            return ('{"result":"ok","session_id":"sid-000","is_error":false}', "")

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(bridge_runtime.subprocess, "Popen", fake_popen)

    result = runner.run("hello world", session_id="sid-000", resume=True, tag=None)

    assert captured["args"][-1] == "hello world"
    assert result["result"] == "ok"
    assert result["is_error"] is False


def test_bridge_worker_returns_handle_on_exception():
    class RaisingRunner:
        def run(self, *args, **kwargs):
            raise RuntimeError("boom")

    handle = bridge_worker.process_message(
        item={
            "bot_id": "bot",
            "chat_id": "chat",
            "thread_id": None,
            "message_id": "mid",
            "text": "hello",
        },
        bot_config={"workspace": "/tmp"},
        lark_client=None,
        session_map=DummySessionMap(),
        runner=RaisingRunner(),
        response_handle_cls=FakeHandle,
        download_image_fn=lambda *args, **kwargs: None,
        fetch_quoted_message_fn=lambda *args, **kwargs: None,
        remove_typing_indicator_fn=lambda *args, **kwargs: None,
        session_not_found_signatures=[],
    )

    assert isinstance(handle, FakeHandle)
    assert handle.deliveries == [("内部错误，请稍后重试。如持续出现请联系管理员。", True)]
