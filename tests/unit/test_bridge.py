#!/usr/bin/env python3
"""Unit tests for Feishu Bridge task/error handling."""

import json

import pytest

from feishu_bridge import commands as bridge_commands
from feishu_bridge import parsers as bridge_parsers
from feishu_bridge import runtime as bridge_runtime
from feishu_bridge import worker as bridge_worker
from feishu_bridge.api import auth as feishu_auth
from feishu_bridge import main as bridge
from feishu_bridge.api.client import FeishuAPIError
from feishu_bridge.api.tasks import FeishuTasks


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
    handler._handle_feishu_service(
        {
            "chat_id": "chat",
            "sender_id": "ou_xxx",
            "_cmd_arg": "info token",
        },
        handle,
        "sheet",
    )

    assert handle.deliveries == [("sheet:chat:ou_xxx", False)]


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


def test_blocking_runner_returns_silent_ok_when_result_empty(monkeypatch):
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

    assert result["result"] == bridge_runtime.SILENT_OK_MESSAGE
    assert result["session_id"] == "sid-789"
    assert result["is_error"] is False


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


def test_worker_refetches_card_content_when_flag_set():
    """Worker calls fetch_card_content_fn when _card_message_id is set."""
    captured = {}

    class OKRunner:
        def run(self, text, **kwargs):
            captured["text"] = text
            return {"result": text, "session_id": "s", "is_error": False}

    handle = bridge_worker.process_message(
        item={
            "bot_id": "bot",
            "chat_id": "chat",
            "thread_id": None,
            "message_id": "mid",
            "text": "[用户转发了一条卡片消息: Test Card]",
            "_card_message_id": "mid",
        },
        bot_config={"workspace": "/tmp"},
        lark_client=None,
        session_map=DummySessionMap(),
        runner=OKRunner(),
        response_handle_cls=FakeHandle,
        download_image_fn=lambda *a, **k: None,
        fetch_card_content_fn=lambda client, mid: "[转发卡片: Test Card]\nReal card content here",
        fetch_forward_messages_fn=lambda *a, **k: None,
        fetch_quoted_message_fn=lambda *a, **k: None,
        remove_typing_indicator_fn=lambda *a, **k: None,
        session_not_found_signatures=[],
    )

    # The worker should have replaced the fallback text with the re-fetched content
    assert "Real card content here" in captured["text"]


def test_worker_expands_merge_forward_when_flag_set():
    """Worker calls fetch_forward_messages_fn when _merge_forward_message_id is set."""
    captured = {}

    class OKRunner:
        def run(self, text, **kwargs):
            captured["text"] = text
            return {"result": text, "session_id": "s", "is_error": False}

    handle = bridge_worker.process_message(
        item={
            "bot_id": "bot",
            "chat_id": "chat",
            "thread_id": None,
            "message_id": "mid",
            "text": "[用户转发了一条合并消息，正在展开...]",
            "_merge_forward_message_id": "mid",
        },
        bot_config={"workspace": "/tmp"},
        lark_client=None,
        session_map=DummySessionMap(),
        runner=OKRunner(),
        response_handle_cls=FakeHandle,
        download_image_fn=lambda *a, **k: None,
        fetch_card_content_fn=lambda *a, **k: None,
        fetch_forward_messages_fn=lambda client, mid: "<forwarded_messages>\n[03-19 10:00] user1:\n  Hello world\n</forwarded_messages>",
        fetch_quoted_message_fn=lambda *a, **k: None,
        remove_typing_indicator_fn=lambda *a, **k: None,
        session_not_found_signatures=[],
    )

    assert "<forwarded_messages>" in captured["text"]
    assert "Hello world" in captured["text"]


def test_parse_interactive_content_v2_card():
    """parse_interactive_content extracts text from CardKit v2 format."""
    card = {
        "schema": "2.0",
        "body": {
            "elements": [
                {"tag": "markdown", "content": "Task completed"},
                {"tag": "markdown", "content": "All items done"},
            ]
        }
    }
    result = bridge_parsers.parse_interactive_content(card)
    assert result == "Task completed\nAll items done"


def test_parse_interactive_content_legacy_card():
    """parse_interactive_content extracts text from legacy card format."""
    card = {
        "elements": [
            {"tag": "div", "text": {"content": "Legacy content"}}
        ]
    }
    result = bridge_parsers.parse_interactive_content(card)
    assert result == "Legacy content"


def test_worker_preserves_quote_context_on_card_refetch():
    """When a card message has a quote, re-fetch replaces only the placeholder."""
    captured = {}

    class OKRunner:
        def run(self, text, **kwargs):
            captured["text"] = text
            return {"result": text, "session_id": "s", "is_error": False}

    placeholder = "[用户转发了一条卡片消息: Test]"
    handle = bridge_worker.process_message(
        item={
            "bot_id": "bot",
            "chat_id": "chat",
            "thread_id": None,
            "parent_id": "quote_mid",
            "message_id": "mid",
            "text": placeholder,
            "_card_message_id": "mid",
        },
        bot_config={"workspace": "/tmp"},
        lark_client=None,
        session_map=DummySessionMap(),
        runner=OKRunner(),
        response_handle_cls=FakeHandle,
        download_image_fn=lambda *a, **k: None,
        fetch_card_content_fn=lambda client, mid: "[转发卡片: Test]\nReal content",
        fetch_forward_messages_fn=lambda *a, **k: None,
        fetch_quoted_message_fn=lambda client, mid: {
            "content": "quoted text", "sender_type": "user",
            "sender_id": "u1", "message_id": "quote_mid",
        },
        remove_typing_indicator_fn=lambda *a, **k: None,
        session_not_found_signatures=[],
    )

    # Quote context should be preserved, placeholder replaced with real content
    assert "[引用消息" in captured["text"]
    assert "Real content" in captured["text"]
    assert placeholder not in captured["text"]


def test_worker_rfind_replaces_last_occurrence_not_quote():
    """When quote contains the same text as the placeholder, rfind targets the last one."""
    captured = {}

    class OKRunner:
        def run(self, text, **kwargs):
            captured["text"] = text
            return {"result": text, "session_id": "s", "is_error": False}

    placeholder = "[用户转发了一条卡片消息: Test]"
    handle = bridge_worker.process_message(
        item={
            "bot_id": "bot",
            "chat_id": "chat",
            "thread_id": None,
            "parent_id": "quote_mid",
            "message_id": "mid",
            "text": placeholder,
            "_card_message_id": "mid",
        },
        bot_config={"workspace": "/tmp"},
        lark_client=None,
        session_map=DummySessionMap(),
        runner=OKRunner(),
        response_handle_cls=FakeHandle,
        download_image_fn=lambda *a, **k: None,
        fetch_card_content_fn=lambda client, mid: "[转发卡片: Test]\nReal content",
        fetch_forward_messages_fn=lambda *a, **k: None,
        # Quote content contains the SAME text as the placeholder (adversarial case)
        fetch_quoted_message_fn=lambda client, mid: {
            "content": placeholder, "sender_type": "user",
            "sender_id": "u1", "message_id": "quote_mid",
        },
        remove_typing_indicator_fn=lambda *a, **k: None,
        session_not_found_signatures=[],
    )

    # The placeholder inside the quote block must be preserved (not replaced)
    assert "[引用消息" in captured["text"]
    # The quote block should still contain the original placeholder text
    assert placeholder in captured["text"]
    # The real content should also be present (replaced the LAST occurrence)
    assert "Real content" in captured["text"]


def test_worker_fallback_when_card_refetch_returns_none():
    """When fetch_card_content returns None, original placeholder text is kept."""
    captured = {}

    class OKRunner:
        def run(self, text, **kwargs):
            captured["text"] = text
            return {"result": text, "session_id": "s", "is_error": False}

    handle = bridge_worker.process_message(
        item={
            "bot_id": "bot",
            "chat_id": "chat",
            "thread_id": None,
            "message_id": "mid",
            "text": "[用户转发了一条卡片消息: Test]",
            "_card_message_id": "mid",
        },
        bot_config={"workspace": "/tmp"},
        lark_client=None,
        session_map=DummySessionMap(),
        runner=OKRunner(),
        response_handle_cls=FakeHandle,
        download_image_fn=lambda *a, **k: None,
        fetch_card_content_fn=lambda *a, **k: None,  # Returns None
        fetch_forward_messages_fn=lambda *a, **k: None,
        fetch_quoted_message_fn=lambda *a, **k: None,
        remove_typing_indicator_fn=lambda *a, **k: None,
        session_not_found_signatures=[],
    )

    assert "[用户转发了一条卡片消息: Test]" in captured["text"]


def test_forward_messages_safe_sort_with_null_create_time():
    """fetch_forward_messages handles None/missing create_time without ValueError."""
    import types

    class FakeResp:
        code = 0
        raw = types.SimpleNamespace(content=json.dumps({
            "data": {
                "items": [
                    {
                        "message_id": "root",
                        "msg_type": "merge_forward",
                        "body": {"content": "{}"},
                    },
                    {
                        "message_id": "child1",
                        "msg_type": "text",
                        "create_time": None,  # Null create_time
                        "sender": {"id": "u1"},
                        "body": {"content": '{"text": "hello"}'},
                    },
                    {
                        "message_id": "child2",
                        "msg_type": "text",
                        "create_time": "1710835200000",
                        "sender": {"id": "u2"},
                        "body": {"content": '{"text": "world"}'},
                    },
                ]
            }
        }))

    class FakeClient:
        def request(self, req):
            return FakeResp()

    result = bridge_parsers.fetch_forward_messages(FakeClient(), "root")
    # Should not raise, should contain both messages
    assert result is not None
    assert "hello" in result
    assert "world" in result


def test_fetch_card_content_returns_none_on_invalid_card_json():
    """fetch_card_content returns None when items[0].body.content is invalid JSON."""
    import types

    class FakeResp:
        code = 0
        raw = types.SimpleNamespace(content=json.dumps({
            "data": {
                "items": [{
                    "body": {"content": "NOT VALID JSON {{{"},
                }]
            }
        }))

    class FakeClient:
        def request(self, req):
            return FakeResp()

    result = bridge_parsers.fetch_card_content(FakeClient(), "om_test123")
    assert result is None


def test_context_health_alert_returns_none_below_70():
    """No alert when context usage is below 70%."""
    result = {
        "usage": {"input_tokens": 50_000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        "modelUsage": {"claude-haiku-4-5": {"contextWindow": 200_000}},
    }
    assert bridge_worker._context_health_alert(result) is None


def test_context_health_alert_yellow_at_70_pct():
    """Yellow alert when context usage reaches 70%."""
    result = {
        "usage": {"input_tokens": 10_000, "cache_read_input_tokens": 130_000, "cache_creation_input_tokens": 0},
        "modelUsage": {"claude-haiku-4-5": {"contextWindow": 200_000}},
    }
    alert = bridge_worker._context_health_alert(result)
    assert alert is not None
    assert "70%" in alert
    assert "/compact" in alert


def test_context_health_alert_red_at_85_pct():
    """Red alert when context usage reaches 85%."""
    result = {
        "usage": {"input_tokens": 10_000, "cache_read_input_tokens": 170_000, "cache_creation_input_tokens": 0},
        "modelUsage": {"claude-opus-4-6": {"contextWindow": 200_000}},
    }
    alert = bridge_worker._context_health_alert(result)
    assert alert is not None
    assert "90%" in alert  # 180k/200k = 90%
    assert "/new" in alert


def test_context_health_alert_uses_model_context_window():
    """Alert uses contextWindow from modelUsage, not hardcoded default."""
    # 50k tokens out of 60k window = 83% -> yellow
    result = {
        "usage": {"input_tokens": 10_000, "cache_read_input_tokens": 40_000, "cache_creation_input_tokens": 0},
        "modelUsage": {"custom-model": {"contextWindow": 60_000}},
    }
    alert = bridge_worker._context_health_alert(result)
    assert alert is not None
    assert "83%" in alert


def test_cost_accumulation_across_turns():
    """Cost store accumulates total_cost_usd across multiple turns."""
    cost_store = {}
    session_map = DummySessionMap()

    class OKRunner:
        call_count = 0
        def run(self, text, **kwargs):
            self.call_count += 1
            return {
                "result": f"reply-{self.call_count}",
                "session_id": "sid-1",
                "is_error": False,
                "usage": {"input_tokens": 100, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                "modelUsage": {},
                "total_cost_usd": 0.05,
            }

    runner = OKRunner()

    for i in range(3):
        bridge_worker.process_message(
            item={
                "bot_id": "bot", "chat_id": "chat", "thread_id": None,
                "message_id": f"mid-{i}", "text": f"msg-{i}",
                "_cost_store": cost_store,
            },
            bot_config={"workspace": "/tmp"},
            lark_client=None,
            session_map=session_map,
            runner=runner,
            response_handle_cls=FakeHandle,
            download_image_fn=lambda *a, **k: None,
            fetch_card_content_fn=lambda *a, **k: None,
            fetch_forward_messages_fn=lambda *a, **k: None,
            fetch_quoted_message_fn=lambda *a, **k: None,
            remove_typing_indicator_fn=lambda *a, **k: None,
            session_not_found_signatures=[],
        )

    # 3 turns x $0.05 = $0.15 accumulated
    assert cost_store["sid-1"]["accumulated_cost_usd"] == pytest.approx(0.15)
    # Latest turn cost is still $0.05
    assert cost_store["sid-1"]["total_cost_usd"] == pytest.approx(0.05)


def test_get_cached_token_never_triggers_ensure(monkeypatch):
    """get_cached_token calls get_valid_token (no scopes), never ensure_user_token."""
    from feishu_bridge.api.client import FeishuAPI

    api = FeishuAPI.__new__(FeishuAPI)
    api._token_override = None

    class FakeAuth:
        ensure_called = False
        gvt_scopes = "NOT_CALLED"
        def get_valid_token(self, user_open_id, required_scopes=None):
            self.gvt_scopes = required_scopes
            return "cached-token-123"
        def ensure_user_token(self, chat_id, user_open_id, scopes):
            self.ensure_called = True
            return "prompted-token"

    api.auth = FakeAuth()
    api.SCOPES = ["docx:document:readonly"]

    result = api.get_cached_token("user-1")
    assert result == "cached-token-123"
    assert not api.auth.ensure_called
    # get_cached_token must NOT pass scopes — any valid token is accepted
    assert api.auth.gvt_scopes is None


def test_get_cached_token_returns_none_without_prompting(monkeypatch):
    """get_cached_token returns None when no cached token — no auth card."""
    from feishu_bridge.api.client import FeishuAPI

    api = FeishuAPI.__new__(FeishuAPI)
    api._token_override = None

    class FakeAuth:
        ensure_called = False
        def get_valid_token(self, user_open_id, required_scopes=None):
            return None  # no cached token
        def ensure_user_token(self, chat_id, user_open_id, scopes):
            self.ensure_called = True
            return "prompted-token"

    api.auth = FakeAuth()
    api.SCOPES = ["docx:document:readonly"]

    result = api.get_cached_token("user-1")
    assert result is None
    assert not api.auth.ensure_called


def test_get_cached_token_returns_override_when_set():
    """get_cached_token returns token_override if set (CLI mode)."""
    from feishu_bridge.api.client import FeishuAPI

    api = FeishuAPI.__new__(FeishuAPI)
    api._token_override = "cli-token-abc"
    api.auth = None  # should not be accessed
    api.SCOPES = []

    assert api.get_cached_token("user-1") == "cli-token-abc"


def test_autofetch_skips_api_when_no_cached_token():
    """Auto-fetch includes placeholder when no cached token, never calls API."""
    api_called = {"docs": False, "sheets": False}

    class FakeDocs:
        def get_cached_token(self, user_open_id):
            return None  # no cached token
        def fetch(self, *args, **kwargs):
            api_called["docs"] = True
            return {"title": "test", "markdown": "# test"}

    class FakeSheets:
        def get_cached_token(self, user_open_id):
            return None
        def info(self, *args, **kwargs):
            api_called["sheets"] = True
            return {"spreadsheet": {"title": "test"}, "sheets": []}

    class OKRunner:
        def run(self, text, **kwargs):
            return {"result": text, "session_id": "s", "is_error": False}

    captured = {}
    class CapturingRunner:
        def run(self, text, **kwargs):
            captured["text"] = text
            return {"result": text, "session_id": "s", "is_error": False}

    bridge_worker.process_message(
        item={
            "bot_id": "bot", "chat_id": "chat", "thread_id": None,
            "message_id": "mid", "text": "check this doc",
            "_feishu_urls": [("wiki", "wkcnXXX"), ("sheets", "shtcnYYY")],
        },
        bot_config={"workspace": "/tmp"},
        lark_client=None,
        session_map=DummySessionMap(),
        runner=CapturingRunner(),
        feishu_docs=FakeDocs(),
        feishu_sheets=FakeSheets(),
        feishu_api_error_cls=FeishuAPIError,
        response_handle_cls=FakeHandle,
        download_image_fn=lambda *a, **k: None,
        fetch_card_content_fn=lambda *a, **k: None,
        fetch_forward_messages_fn=lambda *a, **k: None,
        fetch_quoted_message_fn=lambda *a, **k: None,
        remove_typing_indicator_fn=lambda *a, **k: None,
        session_not_found_signatures=[],
    )

    # API should NOT have been called
    assert not api_called["docs"]
    assert not api_called["sheets"]
    # Placeholder text should mention authorization
    assert "未授权" in captured["text"]


def test_context_health_alert_prefers_last_call_usage():
    """Alert uses last_call_usage (per-API-call) over cumulative usage."""
    result = {
        # Cumulative usage across 5 sub-calls — would show 250% (over-counted)
        "usage": {
            "input_tokens": 50_000,
            "cache_read_input_tokens": 400_000,
            "cache_creation_input_tokens": 50_000,
        },
        # Last sub-call's actual context — the real utilization
        "last_call_usage": {
            "input_tokens": 10_000,
            "cache_read_input_tokens": 80_000,
            "cache_creation_input_tokens": 10_000,
        },
        "modelUsage": {"claude-haiku-4-5": {"contextWindow": 200_000}},
    }
    alert = bridge_worker._context_health_alert(result)
    # 100K / 200K = 50% -> no alert (below 70% threshold)
    assert alert is None


def test_context_health_alert_fallback_to_usage_when_no_last_call():
    """Alert falls back to cumulative usage when last_call_usage is absent."""
    result = {
        "usage": {
            "input_tokens": 10_000,
            "cache_read_input_tokens": 140_000,
            "cache_creation_input_tokens": 0,
        },
        # No last_call_usage (blocking mode or missing)
        "modelUsage": {"claude-haiku-4-5": {"contextWindow": 200_000}},
    }
    alert = bridge_worker._context_health_alert(result)
    assert alert is not None
    assert "75%" in alert


def test_context_health_alert_compact_detected_with_peak():
    """When compact was detected, alert shows pre-compact peak percentage."""
    result = {
        "last_call_usage": {
            "input_tokens": 50_000,  # post-compact: only 25%
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        "modelUsage": {"claude-opus-4-6": {"contextWindow": 200_000}},
        "compact_detected": True,
        "peak_context_tokens": 170_000,  # pre-compact: 85%
    }
    alert = bridge_worker._context_health_alert(result)
    assert alert is not None
    assert "自动压缩" in alert
    assert "85%" in alert


def test_context_health_alert_compact_detected_no_peak():
    """When compact detected but no peak data, fall back to current usage."""
    result = {
        "last_call_usage": {
            "input_tokens": 150_000,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        "modelUsage": {"claude-opus-4-6": {"contextWindow": 200_000}},
        "compact_detected": True,
        "peak_context_tokens": 0,
    }
    alert = bridge_worker._context_health_alert(result)
    assert alert is not None
    assert "75%" in alert  # falls back to current usage (150k/200k)


def test_context_health_alert_no_compact_still_alerts():
    """Without compact, normal threshold alerts still work."""
    result = {
        "last_call_usage": {
            "input_tokens": 180_000,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        "modelUsage": {"claude-opus-4-6": {"contextWindow": 200_000}},
        "compact_detected": False,
        "peak_context_tokens": 180_000,
    }
    alert = bridge_worker._context_health_alert(result)
    assert alert is not None
    assert "🔴" in alert
    assert "90%" in alert


# ---------------------------------------------------------------------------
# Auth locking: deadlock regression & concurrent refresh
# ---------------------------------------------------------------------------

def test_ensure_user_token_no_deadlock_on_expired_token(monkeypatch, tmp_path):
    """ensure_user_token must not deadlock when cached token is expired.

    Regression test: _ensure_user_token_inner previously called
    get_valid_token() (which acquires the user lock) while already
    holding the same lock from ensure_user_token().  Now it calls
    _get_valid_token_unlocked() instead.
    """
    import threading

    monkeypatch.setattr(feishu_auth, "TOKEN_DIR", tmp_path)
    # Mock out network calls — we only care about the locking behaviour.
    # Device flow is reached because expired token + no refresh → None.
    # Return valid-looking data; _send_card will return None (no lark_client)
    # → ensure_user_token returns None without blocking.
    monkeypatch.setattr(feishu_auth, "request_device_authorization",
                        lambda *a, **k: {
                            "device_code": "dc", "user_code": "uc",
                            "verification_uri": "", "verification_uri_complete": "",
                            "expires_in": 60, "interval": 5,
                        })

    auth = feishu_auth.FeishuAuth("app1", "secret1", lark_client=None)

    # Store an expired token with no refresh token
    expired = {
        "access_token": "old",
        "refresh_token": "",  # no refresh — _get_valid_token_unlocked returns None
        "expires_in": 1,
        "refresh_expires_in": 1,
        "obtained_at": 0,
        "scope": "",
    }
    feishu_auth.save_token("app1", "user1", expired)

    result = [None]
    error = [None]

    def _call():
        try:
            result[0] = auth.ensure_user_token("chat1", "user1", ["task:task:read"])
        except Exception as e:
            error[0] = e

    t = threading.Thread(target=_call)
    t.start()
    t.join(timeout=3)  # must complete within 3s; deadlock → timeout

    assert not t.is_alive(), "ensure_user_token deadlocked!"
    assert error[0] is None
    # Token expired + no refresh → returns None (would start Device Flow,
    # but no lark_client so _send_card returns None → returns None)
    assert result[0] is None


def test_concurrent_refresh_single_rotation(monkeypatch, tmp_path):
    """Two threads calling get_valid_token with expired token: refresh once.

    The class-level lock must ensure that refresh_access_token is called
    exactly once, not twice (which would fail the second time since the
    refresh token is single-use).
    """
    import threading

    monkeypatch.setattr(feishu_auth, "TOKEN_DIR", tmp_path)

    # Store an expired token with a valid refresh token
    import time as _time
    expired = {
        "access_token": "expired-at",
        "refresh_token": "rt-single-use",
        "expires_in": 7200,
        "refresh_expires_in": 604800,
        "obtained_at": _time.time() - 8000,  # expired
        "scope": "task:task:read",
    }
    feishu_auth.save_token("app2", "user2", expired)

    refresh_calls = []
    _original_refresh = feishu_auth.refresh_access_token

    def _counting_refresh(app_id, app_secret, refresh_token):
        refresh_calls.append(refresh_token)
        return {
            "access_token": "new-at",
            "refresh_token": "new-rt",
            "expires_in": 7200,
            "refresh_expires_in": 604800,
            "scope": "task:task:read",
            "obtained_at": _time.time(),
        }

    monkeypatch.setattr(feishu_auth, "refresh_access_token", _counting_refresh)

    auth1 = feishu_auth.FeishuAuth("app2", "secret2")
    auth2 = feishu_auth.FeishuAuth("app2", "secret2")  # different instance, same app

    results = [None, None]
    barrier = threading.Barrier(2)

    def _call(idx, auth_inst):
        barrier.wait()  # synchronize start
        results[idx] = auth_inst.get_valid_token("user2", ["task:task:read"])

    t1 = threading.Thread(target=_call, args=(0, auth1))
    t2 = threading.Thread(target=_call, args=(1, auth2))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not t1.is_alive()
    assert not t2.is_alive()

    # Both threads should get a valid token
    assert results[0] == "new-at"
    assert results[1] == "new-at"

    # refresh_access_token should be called exactly once (the second thread
    # should find the already-refreshed token via double-check read)
    assert len(refresh_calls) == 1


def test_class_level_lock_shared_across_instances():
    """_get_user_lock returns the same lock for same (app_id, user) across instances."""
    auth1 = feishu_auth.FeishuAuth("app-shared", "s1")
    auth2 = feishu_auth.FeishuAuth("app-shared", "s2")

    lock1 = auth1._get_user_lock("user-x")
    lock2 = auth2._get_user_lock("user-x")

    assert lock1 is lock2, "Same (app_id, user) must share the same lock"

    # Different app_id → different lock
    auth3 = feishu_auth.FeishuAuth("app-other", "s3")
    lock3 = auth3._get_user_lock("user-x")
    assert lock3 is not lock1


def test_autofetch_never_contains_auth_card_text():
    """Auto-fetch placeholder must never contain '已发送授权卡片'."""
    class FakeDocs:
        def get_cached_token(self, user_open_id):
            return None

    captured = {}

    class CapturingRunner:
        def run(self, text, **kwargs):
            captured["text"] = text
            return {"result": text, "session_id": "s", "is_error": False}

    bridge_worker.process_message(
        item={
            "bot_id": "bot", "chat_id": "chat", "thread_id": None,
            "message_id": "mid", "text": "look at this",
            "_feishu_urls": [("doc", "doxcnABC"), ("wiki", "wkcnDEF")],
        },
        bot_config={"workspace": "/tmp"},
        lark_client=None,
        session_map=DummySessionMap(),
        runner=CapturingRunner(),
        feishu_docs=FakeDocs(),
        feishu_sheets=None,
        feishu_api_error_cls=FeishuAPIError,
        response_handle_cls=FakeHandle,
        download_image_fn=lambda *a, **k: None,
        fetch_card_content_fn=lambda *a, **k: None,
        fetch_forward_messages_fn=lambda *a, **k: None,
        fetch_quoted_message_fn=lambda *a, **k: None,
        remove_typing_indicator_fn=lambda *a, **k: None,
        session_not_found_signatures=[],
    )

    assert "已发送授权卡片" not in captured["text"]
    assert "未授权" in captured["text"]
