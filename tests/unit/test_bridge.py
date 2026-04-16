#!/usr/bin/env python3
"""Unit tests for Feishu Bridge task/error handling."""

import json
import os
import sys
import types

import pytest

from feishu_bridge import commands as bridge_commands
from feishu_bridge import parsers as bridge_parsers
from feishu_bridge import runtime as bridge_runtime
from feishu_bridge import worker as bridge_worker
from feishu_bridge.api import auth as feishu_auth
from feishu_bridge import main as bridge
from feishu_bridge.api.client import FeishuAPIError
from feishu_bridge.api.tasks import FeishuTasks


@pytest.fixture(autouse=True)
def _stub_cryptography_when_missing(monkeypatch):
    """Provide a test-only AESGCM stub when cryptography isn't installed."""
    try:
        import cryptography  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    import hashlib

    class FakeAESGCM:
        def __init__(self, key):
            self._key = key

        def encrypt(self, nonce, data, associated_data):
            aad = associated_data or b""
            tag = hashlib.sha256(self._key + nonce + aad + data).digest()
            return tag + data

        def decrypt(self, nonce, data, associated_data):
            aad = associated_data or b""
            tag, payload = data[:32], data[32:]
            expected = hashlib.sha256(self._key + nonce + aad + payload).digest()
            if tag != expected:
                raise ValueError("invalid tag")
            return payload

    cryptography_mod = types.ModuleType("cryptography")
    hazmat_mod = types.ModuleType("cryptography.hazmat")
    primitives_mod = types.ModuleType("cryptography.hazmat.primitives")
    ciphers_mod = types.ModuleType("cryptography.hazmat.primitives.ciphers")
    aead_mod = types.ModuleType("cryptography.hazmat.primitives.ciphers.aead")
    aead_mod.AESGCM = FakeAESGCM

    monkeypatch.setitem(sys.modules, "cryptography", cryptography_mod)
    monkeypatch.setitem(sys.modules, "cryptography.hazmat", hazmat_mod)
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives", primitives_mod)
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.ciphers", ciphers_mod)
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.ciphers.aead", aead_mod)


class FakeHandle:
    """Minimal ResponseHandle stub for worker-unit tests."""

    def __init__(self, client, chat_id, thread_id, message_id, bot_id=None):
        self.client = client
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.source_message_id = message_id
        self.bot_id = bot_id
        self.deliveries = []
        self._terminated = False
        self._card_fallback_timer = None
        self._typing_reaction_id = None

    def send_processing_indicator(self):
        return True

    def stream_update(self, content):
        self.last_stream = content

    def deliver(self, content, is_error=False, total_tokens=0):
        self.deliveries.append((content, is_error, total_tokens))


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
        False, 0,
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

    assert handle.deliveries == [("sheet:chat:ou_xxx", False, 0)]


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

    assert "未返回任何内容" in result["result"]
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
    assert handle.deliveries == [("内部错误，请稍后重试。如持续出现请联系管理员。", True, 0)]


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


def test_context_health_alert_red_at_80_pct():
    """Red alert when context usage reaches 80%."""
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
    # 50k tokens out of 60k window = 83% -> red (>=80%)
    result = {
        "usage": {"input_tokens": 10_000, "cache_read_input_tokens": 40_000, "cache_creation_input_tokens": 0},
        "modelUsage": {"custom-model": {"contextWindow": 60_000}},
    }
    alert = bridge_worker._context_health_alert(result)
    assert alert is not None
    assert "83%" in alert


def test_cost_accumulation_across_turns():
    """Cost store tracks session-cumulative total_cost_usd and per-turn delta."""
    cost_store = {}
    session_map = DummySessionMap()

    class OKRunner:
        call_count = 0
        def run(self, text, **kwargs):
            self.call_count += 1
            # total_cost_usd from Claude CLI is session-cumulative
            return {
                "result": f"reply-{self.call_count}",
                "session_id": "sid-1",
                "is_error": False,
                "usage": {"input_tokens": 100, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                "modelUsage": {},
                "total_cost_usd": 0.05 * self.call_count,
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

    # Session cost = latest cumulative value ($0.15)
    assert cost_store["sid-1"]["session_cost_usd"] == pytest.approx(0.15)
    # Turn cost = delta of last two turns ($0.15 - $0.10 = $0.05)
    assert cost_store["sid-1"]["turn_cost_usd"] == pytest.approx(0.05)


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


def test_compact_detected_via_token_drop():
    """Auto-compact detected when ctx_tokens drops >50% from peak
    (peak counts input + cache_read only, excluding cache_creation)."""
    runner = bridge_runtime.ClaudeRunner(
        command="claude", model="claude-opus-4-6", timeout=60, workspace="/tmp",
    )
    state = bridge_runtime.StreamState()
    # Pre-compact: input + cache_read = 100K (>= 50K floor)
    runner.parse_streaming_line({
        "type": "assistant",
        "message": {"usage": {
            "input_tokens": 10_000,
            "cache_read_input_tokens": 90_000,
            "cache_creation_input_tokens": 10_000,
        }, "content": []},
    }, state)
    assert state.peak_context_tokens == 100_000
    assert not state.compact_detected

    # Post-compact drops to 15K (>50% drop); cache_creation ignored.
    runner.parse_streaming_line({
        "type": "assistant",
        "message": {"usage": {
            "input_tokens": 5_000,
            "cache_read_input_tokens": 10_000,
            "cache_creation_input_tokens": 30_000,
        }, "content": []},
    }, state)
    assert state.compact_detected
    assert state.peak_context_tokens == 100_000


def test_compact_not_detected_on_small_drop():
    """Normal token fluctuation (<30% drop) does not trigger compact detection."""
    runner = bridge_runtime.ClaudeRunner(
        command="claude", model="claude-opus-4-6", timeout=60, workspace="/tmp",
    )
    state = bridge_runtime.StreamState()
    runner.parse_streaming_line({
        "type": "assistant",
        "message": {"usage": {
            "input_tokens": 1_000,
            "cache_read_input_tokens": 80_000,
            "cache_creation_input_tokens": 19_000,
        }, "content": []},
    }, state)
    assert state.peak_context_tokens == 81_000  # 1K + 80K cache_read

    # Small drop — still > 50% of peak
    runner.parse_streaming_line({
        "type": "assistant",
        "message": {"usage": {
            "input_tokens": 1_000,
            "cache_read_input_tokens": 70_000,
            "cache_creation_input_tokens": 9_000,
        }, "content": []},
    }, state)
    assert not state.compact_detected


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


# ---------------------------------------------------------------------------
# Group chat gate: _check_group_gate + helpers
# ---------------------------------------------------------------------------

class _NS:
    """Simple namespace for mocking SDK objects."""
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _make_bot(default_mode=None, owner=None, overrides=None, bot_open_id=None):
    """Create a minimal FeishuBot stub for gate testing."""
    bot = object.__new__(bridge.FeishuBot)
    bot._group_default_mode = default_mode
    bot._group_owner = owner
    bot._group_overrides = overrides or {}
    bot.bot_open_id = bot_open_id
    return bot


def _mention(open_id, key="@_user_1"):
    """Create a mock SDK MentionEvent."""
    return _NS(id=_NS(open_id=open_id), key=key)


# --- p2p / compat mode ---

def test_gate_p2p_always_passes():
    bot = _make_bot(default_mode="disabled")
    assert bot._check_group_gate("p2p", "ou_any", [], "chat") is True


def test_gate_no_policy_passes_all():
    bot = _make_bot(default_mode=None)
    assert bot._check_group_gate("group", "ou_any", [], "chat") is True


# --- auto-reply ---

def test_gate_auto_reply_passes():
    bot = _make_bot(default_mode="auto-reply")
    assert bot._check_group_gate("group", "ou_any", [], "chat") is True


# --- disabled ---

def test_gate_disabled_rejects():
    bot = _make_bot(default_mode="disabled")
    assert bot._check_group_gate("group", "ou_any", [], "chat") is False


# --- mention-all ---

def test_gate_mention_all_with_bot_mention():
    bot = _make_bot(default_mode="mention-all", bot_open_id="ob_bot")
    mentions = [_mention("ob_bot")]
    assert bot._check_group_gate("group", "ou_any", mentions, "chat") is True


def test_gate_mention_all_without_mention():
    bot = _make_bot(default_mode="mention-all", bot_open_id="ob_bot")
    assert bot._check_group_gate("group", "ou_any", [], "chat") is False


def test_gate_mention_all_wrong_mention():
    bot = _make_bot(default_mode="mention-all", bot_open_id="ob_bot")
    mentions = [_mention("ob_other_user")]
    assert bot._check_group_gate("group", "ou_any", mentions, "chat") is False


def test_gate_mention_all_no_bot_open_id_passthrough():
    """When bot_open_id is None, mention-all degrades to pass-through."""
    bot = _make_bot(default_mode="mention-all", bot_open_id=None)
    assert bot._check_group_gate("group", "ou_any", [], "chat") is True


# --- owner-only ---

def test_gate_owner_only_owner_with_mention():
    bot = _make_bot(default_mode="owner-only", owner="ou_owner",
                    bot_open_id="ob_bot")
    mentions = [_mention("ob_bot")]
    assert bot._check_group_gate("group", "ou_owner", mentions, "chat") is True


def test_gate_owner_only_owner_without_mention():
    """Owner must @bot — AND semantics."""
    bot = _make_bot(default_mode="owner-only", owner="ou_owner",
                    bot_open_id="ob_bot")
    assert bot._check_group_gate("group", "ou_owner", [], "chat") is False


def test_gate_owner_only_not_owner():
    bot = _make_bot(default_mode="owner-only", owner="ou_owner",
                    bot_open_id="ob_bot")
    mentions = [_mention("ob_bot")]
    assert bot._check_group_gate("group", "ou_other", mentions, "chat") is False


def test_gate_owner_only_no_owner_configured():
    """No owner set → fail-closed (reject all)."""
    bot = _make_bot(default_mode="owner-only", owner=None,
                    bot_open_id="ob_bot")
    mentions = [_mention("ob_bot")]
    assert bot._check_group_gate("group", "ou_any", mentions, "chat") is False


def test_gate_owner_only_no_bot_open_id_owner_passes():
    """bot_open_id=None degradation: skip @bot check, keep sender=owner."""
    bot = _make_bot(default_mode="owner-only", owner="ou_owner",
                    bot_open_id=None)
    assert bot._check_group_gate("group", "ou_owner", [], "chat") is True


def test_gate_owner_only_no_bot_open_id_non_owner_rejects():
    bot = _make_bot(default_mode="owner-only", owner="ou_owner",
                    bot_open_id=None)
    assert bot._check_group_gate("group", "ou_other", [], "chat") is False


# --- per-group overrides ---

def test_gate_per_group_override():
    overrides = {"oc_special": {"mode": "auto-reply"}}
    bot = _make_bot(default_mode="disabled", overrides=overrides)
    assert bot._check_group_gate("group", "ou_any", [], "oc_special") is True
    assert bot._check_group_gate("group", "ou_any", [], "oc_other") is False


def test_gate_per_group_override_with_mention():
    overrides = {"oc_mention": {"mode": "mention-all"}}
    bot = _make_bot(default_mode="disabled", overrides=overrides,
                    bot_open_id="ob_bot")
    mentions = [_mention("ob_bot")]
    assert bot._check_group_gate("group", "ou_any", mentions, "oc_mention") is True
    assert bot._check_group_gate("group", "ou_any", [], "oc_mention") is False


# --- non-text messages (empty mentions) in mention-required modes ---

def test_gate_mention_required_no_mentions_rejects():
    """Non-text messages have no mentions → rejected in mention-all/owner-only."""
    bot = _make_bot(default_mode="mention-all", bot_open_id="ob_bot")
    assert bot._check_group_gate("group", "ou_any", None, "chat") is False

    bot2 = _make_bot(default_mode="owner-only", owner="ou_owner",
                     bot_open_id="ob_bot")
    assert bot2._check_group_gate("group", "ou_owner", None, "chat") is False


# --- unknown chat_type treated as group ---

def test_gate_unknown_chat_type_treated_as_group():
    bot = _make_bot(default_mode="disabled")
    assert bot._check_group_gate("group_chat", "ou_any", [], "chat") is False
    assert bot._check_group_gate(None, "ou_any", [], "chat") is False


# --- helpers ---

def test_strip_mentions():
    mentions = [_NS(key="@_user_1"), _NS(key="@_user_2")]
    result = bridge._strip_mentions("@_user_1 hello @_user_2 world", mentions)
    assert result == "hello  world"


def test_strip_mentions_empty():
    assert bridge._strip_mentions("hello", []) == "hello"
    assert bridge._strip_mentions("hello", None) == "hello"


def test_is_bridge_command():
    assert bridge._is_bridge_command("/help") is True
    assert bridge._is_bridge_command("/restart") is True
    assert bridge._is_bridge_command("/restart-all") is True
    assert bridge._is_bridge_command("/feishu-tasks list") is True
    assert bridge._is_bridge_command("  /help  ") is True
    assert bridge._is_bridge_command("/stop all") is True
    assert bridge._is_bridge_command("/HELP") is True  # case-insensitive
    assert bridge._is_bridge_command("/unknown-cmd") is False
    assert bridge._is_bridge_command("hello /help") is False
    assert bridge._is_bridge_command("") is False
    # Prefix-collision attacks must NOT bypass gate (R6 HIGH)
    assert bridge._is_bridge_command("/helpful message") is False
    assert bridge._is_bridge_command("/stopwatch 10min") is False
    assert bridge._is_bridge_command("/restartx the server") is False
    assert bridge._is_bridge_command("/cancel-my-order") is False
    assert bridge._is_bridge_command("/newbie question") is False


# ---------------------------------------------------------------------------
# Integration: _on_message with group gate
# ---------------------------------------------------------------------------

def _make_full_bot(default_mode=None, owner=None, overrides=None,
                   bot_open_id=None, allowed_users=None, tmp_path=None):
    """Create a FeishuBot stub with enough internals for _on_message."""
    from pathlib import Path as _Path

    bot = object.__new__(bridge.FeishuBot)
    bot.bot_id = "test-bot"
    bot._all_bot_names = ["test-bot"]
    bot.workspace = "/tmp"
    bot.allowed_users = allowed_users or ["*"]
    bot.allowed_chats = ["*"]
    bot._todo_auto_drive = False
    bot._startup_ms = "0"
    bot.bot_open_id = bot_open_id
    bot._session_cost = {}

    # Group policy
    bot._group_default_mode = default_mode
    bot._group_owner = owner
    bot._group_overrides = overrides or {}

    # Minimal dedup (never dedup in tests)
    bot.dedup = bridge.MessageDedup(ttl=1, max_entries=10)

    # Track enqueued items
    bot._enqueued_items = []

    class FakeChatQueue:
        def enqueue(self, key, item):
            bot._enqueued_items.append(item)
            return 'active'
        def drain(self, key):
            return []

    class FakeWorkQueue:
        def put_nowait(self, item):
            bot._enqueued_items.append(item)

    bot._chat_queue = FakeChatQueue()
    bot._work_queue = FakeWorkQueue()

    # Track _reject_not_owner calls
    bot._reject_calls = []

    class FakeExecutor:
        def submit(self, fn, *args, **kwargs):
            if fn is bridge._reject_not_owner:
                bot._reject_calls.append(args)
            # Don't actually execute I/O
    bot._io_executor = FakeExecutor()

    # Dummy runner (cancel is a no-op)
    class FakeRunner:
        def cancel(self, tag):
            return False
    bot.runner = FakeRunner()

    # Dummy command handler
    class FakeCmdHandler:
        def reply_queue_full(self, *a):
            pass
        def add_queued_reaction_to_item(self, *a):
            pass
    bot.command_handler = FakeCmdHandler()

    # session_map needs a real Path (it calls .parent.mkdir)
    _sm_path = _Path(tmp_path or "/tmp") / "test-sessions.json"
    _sm_path.parent.mkdir(parents=True, exist_ok=True)
    bot.session_map = bridge.SessionMap(_sm_path)

    # lark_client stub (used by _reject_not_owner)
    bot.lark_client = None

    return bot


def _make_event_data(text, chat_type="group", sender_id="ou_user",
                     msg_type="text", mentions=None, message_id=None):
    """Build a mock SDK event data object for _on_message.

    Uses _NS (namespace) instances to avoid class-variable scoping issues.
    """
    _mid = message_id or f"om_{abs(hash(text))}"
    content = json.dumps({"text": text}) if msg_type == "text" else json.dumps({})
    mention_objs = mentions or []

    sender_id_obj = _NS(open_id=sender_id)
    sender_obj = _NS(sender_id=sender_id_obj)
    msg_obj = _NS(
        message_id=_mid,
        chat_id="oc_group1",
        message_type=msg_type,
        thread_id=None,
        parent_id=None,
        content=content,
        create_time="9999999999999",
        mentions=mention_objs,
        chat_type=chat_type,
    )
    event_obj = _NS(message=msg_obj, sender=sender_obj)
    return _NS(event=event_obj)


def test_on_message_gate_rejects_disabled_group():
    """Group message in disabled mode is silently dropped (not enqueued)."""
    bot = _make_full_bot(default_mode="disabled")
    data = _make_event_data("hello world", chat_type="group")
    bot._on_message(data)
    assert len(bot._enqueued_items) == 0


def test_on_message_gate_passes_auto_reply():
    """Group message in auto-reply mode is enqueued."""
    bot = _make_full_bot(default_mode="auto-reply")
    data = _make_event_data("hello world", chat_type="group")
    bot._on_message(data)
    assert len(bot._enqueued_items) == 1
    assert bot._enqueued_items[0]["text"] == "hello world"


def test_on_message_gate_passes_p2p_even_when_disabled():
    """DM messages always pass regardless of group policy."""
    bot = _make_full_bot(default_mode="disabled")
    data = _make_event_data("hello from dm", chat_type="p2p")
    bot._on_message(data)
    assert len(bot._enqueued_items) == 1


def test_on_message_command_bypasses_gate_in_disabled_group():
    """Bridge command /help in disabled group should bypass gate and enqueue."""
    bot = _make_full_bot(default_mode="disabled")
    data = _make_event_data("/help", chat_type="group")
    bot._on_message(data)
    assert len(bot._enqueued_items) == 1
    assert bot._enqueued_items[0].get("_bridge_command") == "help"


def test_on_message_crafted_prefix_does_not_bypass_gate():
    """/helpful should NOT be treated as /help command — gate applies."""
    bot = _make_full_bot(default_mode="disabled")
    data = _make_event_data("/helpful tips", chat_type="group")
    bot._on_message(data)
    assert len(bot._enqueued_items) == 0


def test_on_message_mention_all_with_bot_mention():
    """mention-all group passes when @bot is present."""
    bot = _make_full_bot(default_mode="mention-all", bot_open_id="ob_bot")
    mentions = [_NS(id=_NS(open_id="ob_bot"), key="@_user_1")]
    data = _make_event_data("@_user_1 hello", chat_type="group",
                            mentions=mentions)
    bot._on_message(data)
    assert len(bot._enqueued_items) == 1


def test_on_message_mention_all_without_mention_rejects():
    """mention-all group rejects when no @bot."""
    bot = _make_full_bot(default_mode="mention-all", bot_open_id="ob_bot")
    data = _make_event_data("hello without mention", chat_type="group")
    bot._on_message(data)
    assert len(bot._enqueued_items) == 0


def test_on_message_owner_guard_rejects_non_owner_restart():
    """Non-owner sending /restart in group with group_policy is rejected."""
    bot = _make_full_bot(default_mode="auto-reply", owner="ou_owner")
    data = _make_event_data("/restart", chat_type="group",
                            sender_id="ou_non_owner")
    bot._on_message(data)
    # Not enqueued (restart is inline, not via queue)
    assert len(bot._enqueued_items) == 0
    # Rejection message should have been submitted
    assert len(bot._reject_calls) == 1


def test_on_message_owner_guard_allows_owner_restart(monkeypatch):
    """Owner sending /restart in group with group_policy proceeds."""
    bot = _make_full_bot(default_mode="auto-reply", owner="ou_owner")
    # Prevent actual sys.exit
    exit_called = []
    monkeypatch.setattr(bridge, "ResponseHandle", FakeHandle)
    monkeypatch.setattr(bridge.threading, "Timer",
                        lambda *a, **k: type('T', (), {'start': lambda s: None})())
    data = _make_event_data("/restart", chat_type="group",
                            sender_id="ou_owner")
    bot._on_message(data)
    # Should NOT have rejection
    assert len(bot._reject_calls) == 0


def test_on_message_compat_mode_no_gate():
    """Without group_policy (compat mode), group messages pass through."""
    bot = _make_full_bot(default_mode=None)
    data = _make_event_data("hello compat", chat_type="group")
    bot._on_message(data)
    assert len(bot._enqueued_items) == 1


def test_on_message_per_group_override_auto_reply_in_disabled_default():
    """Per-group override allows a specific group even when default is disabled."""
    overrides = {"oc_group1": {"mode": "auto-reply"}}
    bot = _make_full_bot(default_mode="disabled", overrides=overrides)
    data = _make_event_data("hello override", chat_type="group")
    bot._on_message(data)
    assert len(bot._enqueued_items) == 1


# ---------------------------------------------------------------------------
# load_config validation
# ---------------------------------------------------------------------------

def _base_bot_config(**extra):
    """Build minimal valid bot config dict, merging extras."""
    base = {
        "name": "test", "app_id": "a", "app_secret": "s",
        "workspace": "/tmp", "allowed_users": ["*"],
    }
    base.update(extra)
    return base


def test_load_config_missing_default_mode_exits(tmp_path):
    """group_policy without default_mode causes sys.exit(1)."""
    config = {
        "bots": [_base_bot_config(group_policy={})],
        "claude": {"command": "python3"}
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(config))

    with pytest.raises(SystemExit):
        bridge.load_config(str(cfg_file), "test")


def test_load_config_invalid_mode_exits(tmp_path):
    """group_policy with invalid default_mode causes sys.exit(1)."""
    config = {
        "bots": [_base_bot_config(
            group_policy={"default_mode": "invalid-mode"})],
        "claude": {"command": "python3"}
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(config))

    with pytest.raises(SystemExit):
        bridge.load_config(str(cfg_file), "test")


def test_load_config_valid_group_policy(tmp_path):
    """Valid group_policy is accepted and returned in config."""
    config = {
        "bots": [_base_bot_config(group_policy={
            "default_mode": "mention-all",
            "owner": "ou_owner",
            "groups": {"oc_g1": {"mode": "auto-reply"}}
        })],
        "claude": {"command": "python3"}
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(config))

    result = bridge.load_config(str(cfg_file), "test")
    gp = result["bot"]["group_policy"]
    assert gp["default_mode"] == "mention-all"
    assert gp["owner"] == "ou_owner"


def test_load_config_invalid_per_group_mode_falls_back(tmp_path):
    """Per-group override with invalid mode is normalized to default_mode."""
    config = {
        "bots": [_base_bot_config(group_policy={
            "default_mode": "auto-reply",
            "groups": {"oc_g1": {"mode": "bogus"}}
        })],
        "claude": {"command": "python3"}
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(config))

    result = bridge.load_config(str(cfg_file), "test")
    # Invalid mode should be normalized to default
    assert result["bot"]["group_policy"]["groups"]["oc_g1"]["mode"] == "auto-reply"


def test_autofetch_never_contains_auth_card_text():
    """Auto-fetch placeholder must never contain '已发送授权卡片'."""
    class FakeDocs:
        def get_cached_token(self, user_open_id):
            return None
        def cleanup_auth_card(self, user_open_id):
            return False

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


# ---------------------------------------------------------------------------
# BaseRunner ABC, RunResult, StreamState
# ---------------------------------------------------------------------------

def test_run_result_to_dict_filters_none_and_renames():
    """RunResult.to_dict() filters None values and renames model_usage → modelUsage."""
    r = bridge_runtime.RunResult(
        result="hello",
        session_id="sid-1",
        is_error=False,
        model_usage={"claude-opus-4-6": {"contextWindow": 200_000}},
        total_cost_usd=0.05,
    )
    d = r.to_dict()
    # model_usage renamed to modelUsage
    assert "modelUsage" in d
    assert "model_usage" not in d
    assert d["modelUsage"] == {"claude-opus-4-6": {"contextWindow": 200_000}}
    # None fields filtered out
    assert "usage" not in d
    assert "last_call_usage" not in d
    # Non-None fields preserved
    assert d["result"] == "hello"
    assert d["session_id"] == "sid-1"
    assert d["total_cost_usd"] == 0.05
    # Default non-None values kept
    assert d["default_context_window"] == 200_000
    assert d["is_error"] is False


def test_run_result_to_dict_omits_none_session_id():
    """RunResult.to_dict() omits session_id when it is None."""
    r = bridge_runtime.RunResult(result="ok")
    d = r.to_dict()
    assert "session_id" not in d


def test_base_runner_abc_cannot_instantiate():
    """BaseRunner cannot be instantiated directly (abstract methods)."""
    import pytest
    with pytest.raises(TypeError):
        bridge_runtime.BaseRunner(
            command="test", model="m", workspace="/tmp", timeout=30,
        )


def test_base_runner_subclass_missing_default_model():
    """Concrete BaseRunner subclass without DEFAULT_MODEL raises TypeError."""
    import pytest
    with pytest.raises(TypeError, match="must define DEFAULT_MODEL"):
        class BadRunner(bridge_runtime.BaseRunner):
            def build_args(self, *a, **k): pass
            def parse_streaming_line(self, *a, **k): pass
            def parse_blocking_output(self, *a, **k): pass
            def get_model_aliases(self): return {}
            def get_default_context_window(self): return 100_000


def test_claude_runner_session_not_found_signatures():
    """ClaudeRunner.get_session_not_found_signatures() returns expected list."""
    runner = bridge_runtime.ClaudeRunner(
        command="claude", model="claude-opus-4-6",
        workspace="/tmp", timeout=30,
    )
    sigs = runner.get_session_not_found_signatures()
    # Must include both original main.py entries and new entries
    assert "session not found" in sigs
    assert "no such session" in sigs
    assert "session does not exist" in sigs
    assert "ENOENT" in sigs


def test_claude_runner_get_display_name():
    """ClaudeRunner.get_display_name() returns 'Claude'."""
    runner = bridge_runtime.ClaudeRunner(
        command="claude", model="claude-opus-4-6",
        workspace="/tmp", timeout=30,
    )
    assert runner.get_display_name() == "Claude"


def test_claude_runner_get_model_aliases():
    """ClaudeRunner provides opus/sonnet/haiku aliases."""
    runner = bridge_runtime.ClaudeRunner(
        command="claude", model="claude-opus-4-6",
        workspace="/tmp", timeout=30,
    )
    aliases = runner.get_model_aliases()
    assert aliases["opus"] == "claude-opus-4-6"
    assert aliases["sonnet"] == "claude-sonnet-4-6"
    assert aliases["haiku"] == "claude-haiku-4-5"


def test_stream_state_pending_output_default_empty():
    """StreamState.pending_output defaults to empty list (not shared)."""
    s1 = bridge_runtime.StreamState()
    s2 = bridge_runtime.StreamState()
    s1.pending_output.append("test")
    assert s2.pending_output == []  # not shared


# ---------------------------------------------------------------------------
# CodexRunner
# ---------------------------------------------------------------------------

def _make_codex_runner(**kwargs):
    defaults = dict(command="codex", model="gpt-5.2-codex",
                    workspace="/tmp", timeout=30)
    defaults.update(kwargs)
    return bridge_runtime.CodexRunner(**defaults)


def test_codex_runner_build_args_new_session():
    """CodexRunner build_args for new session ignores caller session_id."""
    runner = _make_codex_runner()
    args = runner.build_args("hello", session_id="sid-ignored", resume=False, streaming=True)
    assert args[0] == "codex"
    assert args[1] == "exec"
    assert "--dangerously-bypass-approvals-and-sandbox" in args
    assert "--json" in args
    assert args[-1] == "hello"
    # session_id must NOT appear in args for new sessions
    assert "sid-ignored" not in args


def test_codex_runner_build_args_resume():
    """CodexRunner build_args for resume includes thread_id."""
    runner = _make_codex_runner()
    args = runner.build_args("follow up", session_id="thread-abc", resume=True, streaming=True)
    # resume sub-command with thread_id before prompt
    resume_idx = args.index("resume")
    assert args[resume_idx + 1] == "thread-abc"
    assert args[-1] == "follow up"


def test_codex_runner_build_args_includes_instructions_from_tls():
    """build_args includes -c model_instructions_file when TLS path is set."""
    runner = _make_codex_runner()
    runner._tls.instructions_path = "/tmp/test-instructions.md"
    args = runner.build_args("hello", session_id=None, resume=False, streaming=True)
    assert "-c" in args
    ci = args.index("-c")
    assert args[ci + 1] == "model_instructions_file=/tmp/test-instructions.md"
    runner._tls.instructions_path = None  # cleanup


def test_codex_runner_parse_thread_started():
    """thread.started event sets session_id on state."""
    runner = _make_codex_runner()
    state = bridge_runtime.StreamState()
    runner.parse_streaming_line(
        {"type": "thread.started", "thread_id": "thread-xyz"},
        state,
    )
    assert state.session_id == "thread-xyz"
    assert not state.done


def test_codex_runner_parse_agent_message():
    """item.completed with agent_message appends text."""
    runner = _make_codex_runner()
    state = bridge_runtime.StreamState()
    runner.parse_streaming_line(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Hello world"}},
        state,
    )
    assert state.accumulated_text == "Hello world"
    assert state.pending_output == ["Hello world"]
    assert not state.done


def test_codex_runner_parse_command_execution_ignored():
    """item.completed with command_execution is silently ignored."""
    runner = _make_codex_runner()
    state = bridge_runtime.StreamState()
    runner.parse_streaming_line(
        {"type": "item.completed", "item": {
            "type": "command_execution", "command": "ls",
            "aggregated_output": "file1\nfile2", "exit_code": 0,
        }},
        state,
    )
    assert state.accumulated_text == ""
    assert state.pending_output == []
    assert not state.done


def test_codex_runner_parse_turn_completed():
    """turn.completed extracts usage and marks done."""
    runner = _make_codex_runner()
    state = bridge_runtime.StreamState()
    runner.parse_streaming_line(
        {"type": "turn.completed", "usage": {
            "input_tokens": 1000, "cached_input_tokens": 500, "output_tokens": 200,
        }},
        state,
    )
    assert state.done
    # Verify usage normalization: cached_input_tokens → cache_read_input_tokens
    assert state.last_call_usage["cache_read_input_tokens"] == 500
    assert state.last_call_usage["input_tokens"] == 1000
    assert state.last_call_usage["cache_creation_input_tokens"] == 0
    assert state.peak_context_tokens == 1500  # 1000 + 500


def test_codex_runner_parse_turn_failed():
    """turn.failed sets error text, is_error, and marks done."""
    runner = _make_codex_runner()
    state = bridge_runtime.StreamState()
    runner.parse_streaming_line(
        {"type": "turn.failed", "error": {"message": "rate limit exceeded"}},
        state,
    )
    assert state.done
    assert state.is_error
    assert "rate limit exceeded" in state.accumulated_text


def test_codex_runner_parse_top_level_error():
    """Top-level error event sets error text, is_error, and marks done."""
    runner = _make_codex_runner()
    state = bridge_runtime.StreamState()
    runner.parse_streaming_line(
        {"type": "error", "message": "invalid API key"},
        state,
    )
    assert state.done
    assert state.is_error
    assert "invalid API key" in state.accumulated_text


def test_codex_runner_full_streaming_flow():
    """End-to-end streaming: thread.started → agent_message → turn.completed."""
    runner = _make_codex_runner()

    class FakeProc:
        def __init__(self):
            self.stdout = iter([
                '{"type":"thread.started","thread_id":"t-001"}\n',
                '{"type":"turn.started"}\n',
                '{"type":"item.completed","item":{"type":"command_execution","command":"ls","aggregated_output":"a.py","exit_code":0}}\n',
                '{"type":"item.completed","item":{"type":"agent_message","text":"Here is the file listing."}}\n',
                '{"type":"turn.completed","usage":{"input_tokens":500,"cached_input_tokens":100,"output_tokens":50}}\n',
            ])
            self.stderr = iter([])
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    streamed = []
    result = runner._run_streaming(
        FakeProc(),
        session_id="ignored-sid",
        tag=None,
        on_output=streamed.append,
    )

    assert result["session_id"] == "t-001"
    assert result["result"] == "Here is the file listing."
    assert result["is_error"] is False
    assert streamed == ["Here is the file listing."]
    # Usage normalized
    assert result["last_call_usage"]["cache_read_input_tokens"] == 100
    assert result["peak_context_tokens"] == 600  # 500 + 100


def test_codex_runner_streaming_no_on_output():
    """CodexRunner works when on_output is None (ALWAYS_STREAMING path)."""
    runner = _make_codex_runner()

    class FakeProc:
        def __init__(self):
            self.stdout = iter([
                '{"type":"thread.started","thread_id":"t-002"}\n',
                '{"type":"item.completed","item":{"type":"agent_message","text":"Done."}}\n',
                '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":0,"output_tokens":10}}\n',
            ])
            self.stderr = iter([])
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    result = runner._run_streaming(
        FakeProc(),
        session_id="sid-fallback",
        tag=None,
        on_output=None,  # ALWAYS_STREAMING but no callback
    )

    assert result["session_id"] == "t-002"
    assert result["result"] == "Done."
    assert result["is_error"] is False


def test_codex_runner_streaming_error_flow():
    """turn.failed produces error result."""
    runner = _make_codex_runner()

    class FakeProc:
        def __init__(self):
            self.stdout = iter([
                '{"type":"thread.started","thread_id":"t-err"}\n',
                '{"type":"turn.failed","error":{"message":"quota exceeded"}}\n',
            ])
            self.stderr = iter([])
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    result = runner._run_streaming(
        FakeProc(), session_id=None, tag=None, on_output=lambda _: None,
    )

    assert result["session_id"] == "t-err"
    assert "quota exceeded" in result["result"]
    assert result["is_error"] is True


def test_codex_runner_ignores_max_budget():
    """CodexRunner logs warning and ignores max_budget_usd."""
    runner = bridge_runtime.CodexRunner(
        command="codex", model="gpt-5.2-codex",
        workspace="/tmp", timeout=30,
        max_budget_usd=5.0,
    )
    assert runner.max_budget_usd is None


def test_codex_runner_display_name_and_compact():
    """CodexRunner display name and compact support."""
    runner = _make_codex_runner()
    assert runner.get_display_name() == "Codex"
    assert runner.supports_compact() is False
    assert runner.get_session_not_found_signatures() == []


def test_codex_runner_model_aliases():
    """CodexRunner provides codex/codex-mini aliases."""
    runner = _make_codex_runner()
    aliases = runner.get_model_aliases()
    assert aliases["codex"] == "gpt-5.2-codex"
    assert aliases["codex-mini"] == "gpt-5.1-codex-mini"


def test_codex_runner_multiple_agent_messages():
    """Multiple agent_message items accumulate text."""
    runner = _make_codex_runner()

    class FakeProc:
        def __init__(self):
            self.stdout = iter([
                '{"type":"thread.started","thread_id":"t-multi"}\n',
                '{"type":"item.completed","item":{"type":"agent_message","text":"Part 1. "}}\n',
                '{"type":"item.completed","item":{"type":"agent_message","text":"Part 2."}}\n',
                '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":0,"output_tokens":20}}\n',
            ])
            self.stderr = iter([])
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    streamed = []
    result = runner._run_streaming(
        FakeProc(), session_id=None, tag=None, on_output=streamed.append,
    )

    assert result["result"] == "Part 1. Part 2."
    # Each agent_message appends accumulated text to pending_output
    assert streamed == ["Part 1. ", "Part 1. Part 2."]


def test_codex_runner_temp_file_lifecycle(monkeypatch):
    """run() creates and cleans up temp file for system prompt injection."""
    runner = bridge_runtime.CodexRunner(
        command="codex", model="gpt-5.2-codex",
        workspace="/tmp", timeout=30,
        extra_system_prompts=["Test prompt injection"],
    )

    created_files = []
    deleted_files = []

    class FakeProc:
        def __init__(self):
            self.stdout = iter([
                '{"type":"thread.started","thread_id":"t-tmp"}\n',
                '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n',
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}\n',
            ])
            self.stderr = iter([])
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    original_popen = bridge_runtime.subprocess.Popen

    def fake_popen(args, **kwargs):
        # Check that -c model_instructions_file=<path> is in args
        for i, arg in enumerate(args):
            if arg == "-c" and i + 1 < len(args) and "model_instructions_file=" in args[i + 1]:
                path = args[i + 1].split("=", 1)[1]
                created_files.append(path)
                # Verify file exists and contains our prompt
                assert os.path.exists(path)
                content = open(path).read()
                assert "Test prompt injection" in content
        return FakeProc()

    original_unlink = os.unlink

    def tracking_unlink(path):
        if "codex-instructions-" in path:
            deleted_files.append(path)
        return original_unlink(path)

    monkeypatch.setattr(bridge_runtime.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(os, "unlink", tracking_unlink)

    result = runner.run("test prompt")

    assert result["result"] == "ok"
    # Temp file was created
    assert len(created_files) == 1
    # Temp file was cleaned up
    assert len(deleted_files) == 1
    assert created_files[0] == deleted_files[0]


def test_codex_runner_item_completed_null_item():
    """item.completed with item=null should not crash."""
    runner = _make_codex_runner()
    state = bridge_runtime.StreamState()
    # JSON: {"type": "item.completed", "item": null}
    runner.parse_streaming_line(
        {"type": "item.completed", "item": None},
        state,
    )
    # Should not crash, and no text should be accumulated
    assert state.accumulated_text == ""
    assert not state.done


def test_codex_runner_item_error_propagates():
    """item.completed with type=error propagates error text and sets is_error."""
    runner = _make_codex_runner()
    state = bridge_runtime.StreamState()
    runner.parse_streaming_line(
        {"type": "item.completed", "item": {"type": "error", "text": "sandbox failure"}},
        state,
    )
    assert state.is_error
    assert "sandbox failure" in state.accumulated_text


def test_codex_runner_streaming_result_is_error_propagated():
    """_build_streaming_result propagates is_error from state."""
    runner = _make_codex_runner()

    class FakeProc:
        def __init__(self):
            self.stdout = iter([
                '{"type":"thread.started","thread_id":"t-ie"}\n',
                '{"type":"turn.failed","error":{"message":"auth expired"}}\n',
            ])
            self.stderr = iter([])
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    result = runner._run_streaming(
        FakeProc(), session_id=None, tag=None, on_output=lambda _: None,
    )

    assert result["is_error"] is True
    assert "auth expired" in result["result"]
    # usage should be empty dict, not None
    assert result["usage"] == {}


def test_stream_state_is_error_default():
    """StreamState.is_error defaults to False."""
    state = bridge_runtime.StreamState()
    assert state.is_error is False


def test_codex_runner_item_error_without_turn_completed():
    """BUG-1 regression: item.completed(type=error) as last event before exit
    must preserve is_error=True in the final result, even without turn.completed."""
    runner = _make_codex_runner()

    class FakeProc:
        def __init__(self):
            self.stdout = iter([
                '{"type":"thread.started","thread_id":"t-err"}\n',
                '{"type":"item.completed","item":{"type":"error","text":"rate limit"}}\n',
                # No turn.completed/turn.failed — process exits cleanly
            ])
            self.stderr = iter([])
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    result = runner._run_streaming(
        FakeProc(), session_id=None, tag=None, on_output=lambda _: None,
    )

    # Must be flagged as error even though state.done was never set
    assert result["is_error"] is True
    assert "rate limit" in result["result"]


def test_codex_runner_thread_started_missing_thread_id():
    """BUG-2 regression: thread.started with null/missing thread_id must NOT
    overwrite state.session_id, preventing caller placeholder from being
    persisted as a real Codex thread id."""
    runner = _make_codex_runner()
    state = bridge_runtime.StreamState()

    # Missing thread_id entirely
    runner.parse_streaming_line({"type": "thread.started"}, state)
    assert state.session_id is None

    # Explicit null
    runner.parse_streaming_line({"type": "thread.started", "thread_id": None}, state)
    assert state.session_id is None

    # Empty string
    runner.parse_streaming_line({"type": "thread.started", "thread_id": ""}, state)
    assert state.session_id is None

    # Valid thread_id works
    runner.parse_streaming_line({"type": "thread.started", "thread_id": "t-real"}, state)
    assert state.session_id == "t-real"


def test_codex_runner_parse_top_level_error_null_message():
    """Top-level error event with message=None uses fallback, not 'None' string."""
    runner = _make_codex_runner()
    state = bridge_runtime.StreamState()
    runner.parse_streaming_line({"type": "error", "message": None}, state)
    assert state.done
    assert state.is_error
    # Must NOT render "None" as text — should use fallback
    assert "None" not in state.accumulated_text
    assert "Unknown error" in state.accumulated_text


def test_codex_runner_parse_turn_completed_no_usage():
    """turn.completed without usage field still marks done; last_call_usage stays None."""
    runner = _make_codex_runner()
    state = bridge_runtime.StreamState()
    runner.parse_streaming_line({"type": "turn.completed"}, state)
    assert state.done
    assert state.last_call_usage is None
    assert state.peak_context_tokens == 0


# ============================================================
# Phase 3: Config migration, runner factory, session namespace
# ============================================================

def test_load_config_migrates_claude_to_agent(tmp_path):
    """Old 'claude' config key is migrated to 'agent' with type=claude."""
    config = {
        "bots": [_base_bot_config()],
        "claude": {"command": "python3", "timeout_seconds": 600},
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(config))

    result = bridge.load_config(str(cfg_file), "test")
    assert "agent" in result
    assert result["agent"]["type"] == "claude"
    assert result["agent"]["timeout_seconds"] == 600
    assert "_resolved_command" in result["agent"]


def test_load_config_new_agent_format(tmp_path):
    """New 'agent' format is loaded directly without migration."""
    config = {
        "bots": [_base_bot_config()],
        "agent": {"type": "claude", "command": "python3", "timeout_seconds": 300},
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(config))

    result = bridge.load_config(str(cfg_file), "test")
    assert result["agent"]["type"] == "claude"
    assert result["agent"]["timeout_seconds"] == 300


def test_load_config_normalizes_agent_args_and_env(tmp_path):
    """Agent config normalizes per-type args/env and current-type shorthands."""
    config = {
        "bots": [_base_bot_config()],
        "agent": {
            "type": "claude",
            "command": "python3",
            "args": ["--verbose"],
            "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:11434"},
            "args_by_type": {"codex": ["--oss", "--local-provider", "ollama"]},
            "env_by_type": {"codex": {"OPENAI_BASE_URL": "http://127.0.0.1:11434/v1"}},
        },
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(config))

    result = bridge.load_config(str(cfg_file), "test")

    assert result["agent"]["args_by_type"] == {
        "claude": ["--verbose"],
        "codex": ["--oss", "--local-provider", "ollama"],
    }
    assert result["agent"]["env_by_type"] == {
        "claude": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:11434"},
        "codex": {"OPENAI_BASE_URL": "http://127.0.0.1:11434/v1"},
    }


def test_load_config_normalizes_provider_profiles(tmp_path):
    """Provider profiles are normalized and active provider is preserved."""
    config = {
        "bots": [_base_bot_config()],
        "agent": {
            "type": "claude",
            "command": "python3",
            "provider": "ollama",
            "prompt": {
                "safety": "minimal",
                "feishu_cli": False,
            },
            "providers": {
                "ollama": {
                    "env_by_type": {
                        "claude": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:11434"},
                    },
                    "models": {"claude": "qwen3.5"},
                    "prompt": {"cron_mgr": False},
                }
            },
        },
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(config))

    result = bridge.load_config(str(cfg_file), "test")

    assert result["agent"]["provider"] == "ollama"
    assert result["agent"]["providers"]["ollama"]["env_by_type"] == {
        "claude": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:11434"},
    }
    assert result["agent"]["providers"]["ollama"]["models"] == {
        "claude": "qwen3.5",
    }
    assert result["agent"]["prompt"] == {
        "safety": "minimal",
        "feishu_cli": False,
        "cron_mgr": True,
    }
    assert result["agent"]["providers"]["ollama"]["prompt"] == {
        "cron_mgr": False,
    }


def test_load_config_missing_agent_type_exits(tmp_path):
    """agent config without 'type' causes sys.exit."""
    config = {
        "bots": [_base_bot_config()],
        "agent": {"command": "python3"},
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(config))

    with pytest.raises(SystemExit):
        bridge.load_config(str(cfg_file), "test")


def test_create_runner_claude():
    """Factory creates ClaudeRunner for type=claude."""
    import shutil
    cmd = shutil.which("python3")
    agent_cfg = {
        "type": "claude",
        "_resolved_command": cmd,
        "timeout_seconds": 30,
        "args_by_type": {"claude": ["--verbose"]},
        "env_by_type": {"claude": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:11434"}},
    }
    bot_cfg = {"workspace": "/tmp", "model": "claude-sonnet-4-6"}
    runner = bridge.create_runner(agent_cfg, bot_cfg, [])
    assert isinstance(runner, bridge_runtime.ClaudeRunner)
    assert runner.model == "claude-sonnet-4-6"
    assert runner.build_args("hi", None, False, False)[2:3] == ["--verbose"]
    assert runner.get_extra_env()["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:11434"


def test_create_runner_claude_prompt_profile():
    """Claude runner applies resolved safety prompt mode and optional prompt append."""
    import shutil
    cmd = shutil.which("python3")
    agent_cfg = {
        "type": "claude",
        "_resolved_command": cmd,
        "timeout_seconds": 30,
        "prompt": {"safety": "off", "feishu_cli": False, "cron_mgr": False},
        "providers": {"default": {}},
    }
    bot_cfg = {"workspace": "/tmp", "model": "claude-sonnet-4-6"}

    runner = bridge.create_runner(agent_cfg, bot_cfg, [])

    assert isinstance(runner, bridge_runtime.ClaudeRunner)
    assert "--append-system-prompt" not in runner.build_args("hi", None, False, False)

    agent_cfg["prompt"] = {"safety": "minimal", "feishu_cli": False, "cron_mgr": False}
    runner = bridge.create_runner(agent_cfg, bot_cfg, [])
    assert runner._build_system_prompt() == (
        "CRITICAL: You are running as a subprocess of feishu-bridge. "
        "NEVER restart, stop, or reload feishu-bridge itself."
    )


def test_create_runner_codex():
    """Factory creates CodexRunner for type=codex."""
    import shutil
    cmd = shutil.which("python3")
    agent_cfg = {
        "type": "codex",
        "_resolved_command": cmd,
        "timeout_seconds": 30,
        "args_by_type": {"codex": ["--oss", "--local-provider", "ollama"]},
        "env_by_type": {"codex": {"OPENAI_BASE_URL": "http://127.0.0.1:11434/v1"}},
    }
    bot_cfg = {"workspace": "/tmp"}
    runner = bridge.create_runner(agent_cfg, bot_cfg, [])
    assert isinstance(runner, bridge_runtime.CodexRunner)
    assert runner.model is None  # no explicit model → let CLI decide
    args = runner.build_args("hi", None, False, True)
    assert args[:6] == [
        cmd, "exec", "--oss", "--local-provider", "ollama",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    assert runner.get_extra_env()["OPENAI_BASE_URL"] == "http://127.0.0.1:11434/v1"


def test_create_runner_unknown_type_raises():
    """Factory raises KeyError for unknown agent type (validated in load_config)."""
    agent_cfg = {"type": "unknown", "_resolved_command": "python3"}
    bot_cfg = {"workspace": "/tmp"}
    with pytest.raises(KeyError):
        bridge.create_runner(agent_cfg, bot_cfg, [])


def test_switch_agent_rebuilds_runner_and_clears_sessions(monkeypatch, tmp_path):
    """Bot-level agent switch rebuilds runner and clears incompatible sessions."""
    bot = object.__new__(bridge.FeishuBot)
    bot.bot_id = "test-bot"
    bot.bot_config = {"workspace": "/tmp", "model": "claude-opus-4-6"}
    bot.agent_config = {
        "type": "claude",
        "command": "claude",
        "provider": "default",
        "providers": {"default": {}, "ollama": {}},
        "commands": {"claude": "claude"},
        "args_by_type": {
            "claude": ["--verbose"],
            "codex": ["--oss", "--local-provider", "ollama"],
        },
        "env_by_type": {
            "claude": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:11434"},
            "codex": {"OPENAI_BASE_URL": "http://127.0.0.1:11434/v1"},
        },
        "_resolved_command": bridge.shutil.which("python3"),
        "timeout_seconds": 30,
    }
    bot.runner = bridge_runtime.ClaudeRunner(
        command="claude", model="claude-opus-4-6", workspace="/tmp", timeout=30
    )
    bot._extra_prompts = []
    bot._session_cost = {"sid-old": {"usage": {}}}
    bot._session_map_path = tmp_path / "sessions.json"
    bot._session_map_path.write_text(json.dumps({
        "_agent_type": "claude",
        "chat-key": "sid-old",
    }))
    bot.session_map = bridge.SessionMap(bot._session_map_path, agent_type="claude")

    monkeypatch.setattr(
        bridge,
        "resolve_agent_command",
        lambda agent_cfg, agent_type: (bridge.shutil.which("python3"), "python3"),
    )
    monkeypatch.setattr(bridge, "build_extra_prompts", lambda agent_cfg: [])

    ok, message, resolved = bot.switch_agent("codex")

    assert ok is True
    assert "codex" in message
    assert resolved == bridge.shutil.which("python3")
    assert isinstance(bot.runner, bridge_runtime.CodexRunner)
    assert bot.runner.model == "claude-opus-4-6"  # from bot_config fallback
    assert bot.agent_config["type"] == "codex"
    assert bot.runner.build_args("hi", None, False, True)[2:5] == [
        "--oss", "--local-provider", "ollama"
    ]
    assert bot.runner.get_extra_env()["OPENAI_BASE_URL"] == "http://127.0.0.1:11434/v1"
    assert bot._session_cost == {}
    assert bot.session_map.get(("chat-key",)) is None
    data = json.loads(bot._session_map_path.read_text())
    assert data["_agent_type"] == "codex"


def test_switch_provider_rebuilds_runner_and_clears_sessions(monkeypatch, tmp_path):
    """Provider switch rebuilds runner and clears incompatible sessions."""
    bot = object.__new__(bridge.FeishuBot)
    bot.bot_id = "test-bot"
    bot.bot_config = {"workspace": "/tmp", "model": "claude-opus-4-6"}
    bot.agent_config = {
        "type": "claude",
        "provider": "default",
        "command": "claude",
        "commands": {"claude": "claude"},
        "providers": {
            "default": {},
            "ollama": {
                "env_by_type": {
                    "claude": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:11434"},
                },
                "models": {"claude": "qwen3.5"},
            },
        },
        "args_by_type": {"claude": []},
        "env_by_type": {"claude": {}},
        "_resolved_command": bridge.shutil.which("python3"),
        "timeout_seconds": 30,
    }
    bot.runner = bridge_runtime.ClaudeRunner(
        command="claude", model="claude-opus-4-6", workspace="/tmp", timeout=30
    )
    bot._extra_prompts = []
    bot._session_cost = {"sid-old": {"usage": {}}}
    bot._session_map_path = tmp_path / "sessions.json"
    bot._session_map_path.write_text(json.dumps({
        "_agent_type": "claude",
        "chat-key": "sid-old",
    }))
    bot.session_map = bridge.SessionMap(bot._session_map_path, agent_type="claude")

    monkeypatch.setattr(
        bridge,
        "resolve_effective_agent_command",
        lambda agent_cfg, agent_type: (bridge.shutil.which("python3"), "python3"),
    )
    monkeypatch.setattr(
        bridge,
        "build_extra_prompts",
        lambda agent_cfg: (
            ["default tools"]
            if bridge.resolve_provider_name(agent_cfg) == "default"
            else ["ollama-lite"]
        ),
    )

    ok, message = bot.switch_provider("ollama")

    assert ok is True
    assert "ollama" in message
    assert bot.agent_config["provider"] == "ollama"
    assert isinstance(bot.runner, bridge_runtime.ClaudeRunner)
    assert bot.runner.model == "qwen3.5"
    assert bot._extra_prompts == ["ollama-lite"]
    assert bot.runner._extra_system_prompts == ["ollama-lite"]
    assert bot.runner.get_extra_env()["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:11434"
    assert bot._session_cost == {}
    assert bot.session_map.get(("chat-key",)) is None
    data = json.loads(bot._session_map_path.read_text())
    assert data["_agent_type"] == "claude:ollama"


def test_session_map_agent_type_reconcile_same(tmp_path):
    """SessionMap preserves sessions when agent_type matches."""
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"_agent_type": "claude", "k1": "s1"}))

    sm = bridge_runtime.SessionMap(path, agent_type="claude")
    assert sm.get(("k1",)) == "s1"


def test_session_map_agent_type_reconcile_changed(tmp_path):
    """SessionMap clears sessions when agent_type changes."""
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"_agent_type": "claude", "k1": "s1"}))

    sm = bridge_runtime.SessionMap(path, agent_type="codex")
    assert sm.get(("k1",)) is None  # cleared
    # Verify new type is stored
    data = json.loads(path.read_text())
    assert data["_agent_type"] == "codex"


def test_session_map_legacy_claude_preserved(tmp_path):
    """Legacy sessions (no _agent_type) preserved when type=claude."""
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"k1": "s1"}))

    sm = bridge_runtime.SessionMap(path, agent_type="claude")
    assert sm.get(("k1",)) == "s1"  # preserved
    data = json.loads(path.read_text())
    assert data["_agent_type"] == "claude"


def test_session_map_legacy_non_claude_cleared(tmp_path):
    """Legacy sessions (no _agent_type) cleared when type!=claude."""
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"k1": "s1"}))

    sm = bridge_runtime.SessionMap(path, agent_type="codex")
    assert sm.get(("k1",)) is None  # cleared
    data = json.loads(path.read_text())
    assert data["_agent_type"] == "codex"


# ---------------------------------------------------------------------------
# Auth card IPC — save / read / remove / cleanup
# ---------------------------------------------------------------------------

class TestAuthCardIPC:
    """Tests for file-based auth card IPC (save/read/remove)."""

    def test_save_read_roundtrip(self, tmp_path, monkeypatch):
        """save → read returns the same msg_id."""
        monkeypatch.setattr(feishu_auth, "TOKEN_DIR", tmp_path)
        feishu_auth.save_auth_card_id("app1", "user1", "om_msg_abc123")
        assert feishu_auth.read_auth_card_id("app1", "user1") == "om_msg_abc123"

    def test_save_file_permissions(self, tmp_path, monkeypatch):
        """IPC file must have 0600 permissions."""
        monkeypatch.setattr(feishu_auth, "TOKEN_DIR", tmp_path)
        feishu_auth.save_auth_card_id("app1", "user1", "msg123")
        path = feishu_auth._auth_card_path("app1", "user1")
        mode = oct(path.stat().st_mode & 0o777)
        assert mode == "0o600"

    def test_read_missing_returns_none(self, tmp_path, monkeypatch):
        """Reading non-existent IPC file returns None."""
        monkeypatch.setattr(feishu_auth, "TOKEN_DIR", tmp_path)
        assert feishu_auth.read_auth_card_id("app1", "nobody") is None

    def test_read_empty_returns_none(self, tmp_path, monkeypatch):
        """Reading an empty IPC file returns None."""
        monkeypatch.setattr(feishu_auth, "TOKEN_DIR", tmp_path)
        path = feishu_auth._auth_card_path("app1", "user1")
        path.write_text("")
        assert feishu_auth.read_auth_card_id("app1", "user1") is None

    def test_remove_deletes_file(self, tmp_path, monkeypatch):
        """remove_auth_card_id deletes the IPC file."""
        monkeypatch.setattr(feishu_auth, "TOKEN_DIR", tmp_path)
        feishu_auth.save_auth_card_id("app1", "user1", "msg456")
        feishu_auth.remove_auth_card_id("app1", "user1")
        assert feishu_auth.read_auth_card_id("app1", "user1") is None

    def test_remove_missing_is_noop(self, tmp_path, monkeypatch):
        """remove on non-existent file does not raise."""
        monkeypatch.setattr(feishu_auth, "TOKEN_DIR", tmp_path)
        feishu_auth.remove_auth_card_id("app1", "nobody")  # should not raise

    def test_save_overwrites_previous(self, tmp_path, monkeypatch):
        """Second save overwrites first."""
        monkeypatch.setattr(feishu_auth, "TOKEN_DIR", tmp_path)
        feishu_auth.save_auth_card_id("app1", "user1", "old_msg")
        feishu_auth.save_auth_card_id("app1", "user1", "new_msg")
        assert feishu_auth.read_auth_card_id("app1", "user1") == "new_msg"


class TestCleanupAuthCard:
    """Tests for cleanup_auth_card — delete API before unlinking file."""

    def test_cleanup_deletes_file_on_api_success(self, tmp_path, monkeypatch):
        """File is removed only after successful API delete."""
        monkeypatch.setattr(feishu_auth, "TOKEN_DIR", tmp_path)
        feishu_auth.save_auth_card_id("app1", "user1", "msg_ok")

        auth = feishu_auth.FeishuAuth.__new__(feishu_auth.FeishuAuth)
        auth.app_id = "app1"
        auth.lark_client = None
        monkeypatch.setattr(auth, "_delete_message", lambda msg_id: True)

        assert auth.cleanup_auth_card("user1") is True
        assert feishu_auth.read_auth_card_id("app1", "user1") is None

    def test_cleanup_keeps_file_on_api_failure(self, tmp_path, monkeypatch):
        """File is NOT removed when API delete fails — allows retry."""
        monkeypatch.setattr(feishu_auth, "TOKEN_DIR", tmp_path)
        feishu_auth.save_auth_card_id("app1", "user1", "msg_fail")

        auth = feishu_auth.FeishuAuth.__new__(feishu_auth.FeishuAuth)
        auth.app_id = "app1"
        auth.lark_client = None
        monkeypatch.setattr(auth, "_delete_message", lambda msg_id: False)

        assert auth.cleanup_auth_card("user1") is False
        # File still exists for future retry
        assert feishu_auth.read_auth_card_id("app1", "user1") == "msg_fail"

    def test_cleanup_no_file_returns_false(self, tmp_path, monkeypatch):
        """No IPC file → returns False, no API call."""
        monkeypatch.setattr(feishu_auth, "TOKEN_DIR", tmp_path)
        delete_called = []

        auth = feishu_auth.FeishuAuth.__new__(feishu_auth.FeishuAuth)
        auth.app_id = "app1"
        auth.lark_client = None
        monkeypatch.setattr(auth, "_delete_message",
                            lambda msg_id: delete_called.append(msg_id) or True)

        assert auth.cleanup_auth_card("user1") is False
        assert delete_called == []  # never called


class TestWorkerCleanupAuthCard:
    """Tests for _cleanup_auth_card worker helper."""

    def test_noop_when_no_module(self):
        """No crash when feishu_mod is None."""
        bridge_worker._cleanup_auth_card(None, "user1")

    def test_noop_when_no_sender(self):
        """No crash when sender_id is empty."""
        bridge_worker._cleanup_auth_card(object(), "")

    def test_swallows_exceptions(self):
        """Exceptions are swallowed — best-effort cleanup."""
        class BadMod:
            def cleanup_auth_card(self, uid):
                raise RuntimeError("boom")
        bridge_worker._cleanup_auth_card(BadMod(), "user1")  # should not raise


# ============================================================
# /btw command tests
# ============================================================

def test_is_bridge_command_btw():
    """Verify /btw is recognized as a bridge command."""
    assert bridge._is_bridge_command("/btw what is this?") is True
    assert bridge._is_bridge_command("/btw") is True
    # Must NOT match prefix-collision
    assert bridge._is_bridge_command("/btweet something") is False


def test_is_bridge_command_agent():
    """Verify /agent is recognized as a bridge command."""
    assert bridge._is_bridge_command("/agent codex") is True
    assert bridge._is_bridge_command("/agent") is True
    assert bridge._is_bridge_command("/agency") is False


def test_is_bridge_command_provider():
    """Verify /provider is recognized as a bridge command."""
    assert bridge._is_bridge_command("/provider ollama") is True
    assert bridge._is_bridge_command("/provider") is True
    assert bridge._is_bridge_command("/providers") is False


def test_claude_runner_build_args_fork_session():
    """ClaudeRunner build_args with fork_session=True adds correct flags."""
    runner = bridge_runtime.ClaudeRunner(
        command="claude", model="sonnet", workspace="/tmp", timeout=30)
    args = runner.build_args(
        "question", session_id="sid-123", resume=True,
        streaming=False, fork_session=True)
    assert "--fork-session" in args
    assert "--disallowed-tools" in args
    dt_idx = args.index("--disallowed-tools")
    assert args[dt_idx + 1] == "*"
    assert "--resume" in args
    assert "--output-format" in args
    of_idx = args.index("--output-format")
    assert args[of_idx + 1] == "json"


def test_claude_runner_build_args_no_fork_by_default():
    """ClaudeRunner build_args without fork_session has no fork flags."""
    runner = bridge_runtime.ClaudeRunner(
        command="claude", model="sonnet", workspace="/tmp", timeout=30)
    args = runner.build_args(
        "hello", session_id="sid-123", resume=True, streaming=False)
    assert "--fork-session" not in args
    assert "--disallowed-tools" not in args


def test_claude_runner_build_args_include_extra_cli_args():
    """ClaudeRunner prepends configured extra args before fixed flags."""
    runner = bridge_runtime.ClaudeRunner(
        command="claude",
        model="sonnet",
        workspace="/tmp",
        timeout=30,
        extra_cli_args=["--verbose"],
    )
    args = runner.build_args("hello", session_id=None, resume=False, streaming=False)
    assert args[:3] == ["claude", "-p", "--verbose"]
    assert "--model" in args


def test_codex_runner_build_args_ignores_fork_session():
    """CodexRunner accepts fork_session but ignores it."""
    runner = _make_codex_runner()
    args = runner.build_args(
        "hello", session_id="sid-123", resume=True,
        streaming=False, fork_session=True)
    assert "--fork-session" not in args


def test_codex_runner_build_args_include_extra_cli_args():
    """CodexRunner inserts configured extra args after exec."""
    runner = _make_codex_runner(extra_cli_args=["--oss", "--local-provider", "ollama"])
    args = runner.build_args(
        "hello", session_id="sid-123", resume=True,
        streaming=False, fork_session=True)
    assert args[:5] == ["codex", "exec", "--oss", "--local-provider", "ollama"]


def test_btw_empty_arg_shows_usage():
    """/btw with no argument returns usage hint."""
    bot = object.__new__(bridge.FeishuBot)
    bot.runner = bridge_runtime.ClaudeRunner(
        command="claude", model="sonnet", workspace="/tmp", timeout=30)
    bot.session_map = DummySessionMap()
    handler = bridge_commands.BridgeCommandHandler(bot)
    handle = FakeHandle(None, "chat", None, "mid")
    handler._handle_btw(
        {"bot_id": "b", "chat_id": "chat", "thread_id": None},
        "", handle)
    assert len(handle.deliveries) == 1
    assert "/btw" in handle.deliveries[0][0]


def test_btw_no_session_returns_hint():
    """/btw with no active session returns helpful message."""
    bot = object.__new__(bridge.FeishuBot)
    bot.runner = bridge_runtime.ClaudeRunner(
        command="claude", model="sonnet", workspace="/tmp", timeout=30)
    bot.session_map = DummySessionMap()  # always returns None
    handler = bridge_commands.BridgeCommandHandler(bot)
    handle = FakeHandle(None, "chat", None, "mid")
    handler._handle_btw(
        {"bot_id": "b", "chat_id": "chat", "thread_id": None},
        "what is 42?", handle)
    assert len(handle.deliveries) == 1
    assert "无活跃会话" in handle.deliveries[0][0]


def test_btw_non_claude_runner_returns_unsupported():
    """/btw with non-ClaudeRunner returns unsupported message."""
    bot = object.__new__(bridge.FeishuBot)
    bot.runner = _make_codex_runner()
    bot.session_map = DummySessionMap()
    handler = bridge_commands.BridgeCommandHandler(bot)
    handle = FakeHandle(None, "chat", None, "mid")
    handler._handle_btw(
        {"bot_id": "b", "chat_id": "chat", "thread_id": None},
        "question", handle)
    assert len(handle.deliveries) == 1
    assert "不支持" in handle.deliveries[0][0]


def test_btw_success_with_session(monkeypatch):
    """/btw with active session calls runner.run with fork_session=True."""
    bot = object.__new__(bridge.FeishuBot)
    bot.runner = bridge_runtime.ClaudeRunner(
        command="claude", model="sonnet", workspace="/tmp", timeout=30)

    class SessionMapWithSid:
        def get(self, key):
            return "existing-sid-abc"
    bot.session_map = SessionMapWithSid()

    run_calls = []
    def fake_run(prompt, session_id=None, resume=False, fork_session=False, **kw):
        run_calls.append({
            "prompt": prompt, "session_id": session_id,
            "resume": resume, "fork_session": fork_session,
        })
        return {"result": "The answer is 42.", "is_error": False, "session_id": "fork-sid"}

    monkeypatch.setattr(bot.runner, "run", fake_run)
    handler = bridge_commands.BridgeCommandHandler(bot)
    handle = FakeHandle(None, "chat", None, "mid")
    handler._handle_btw(
        {"bot_id": "b", "chat_id": "chat", "thread_id": None},
        "what is 42?", handle)

    assert len(run_calls) == 1
    assert run_calls[0]["fork_session"] is True
    assert run_calls[0]["resume"] is True
    assert run_calls[0]["session_id"] == "existing-sid-abc"
    assert len(handle.deliveries) == 1
    assert "[/btw]" in handle.deliveries[0][0]
    assert "42" in handle.deliveries[0][0]


def test_btw_runner_error_delivers_error(monkeypatch):
    """/btw delivers error when runner returns is_error."""
    bot = object.__new__(bridge.FeishuBot)
    bot.runner = bridge_runtime.ClaudeRunner(
        command="claude", model="sonnet", workspace="/tmp", timeout=30)

    class SessionMapWithSid:
        def get(self, key):
            return "sid-err"
    bot.session_map = SessionMapWithSid()

    monkeypatch.setattr(bot.runner, "run",
        lambda *a, **kw: {"result": "timeout", "is_error": True})
    handler = bridge_commands.BridgeCommandHandler(bot)
    handle = FakeHandle(None, "chat", None, "mid")
    handler._handle_btw(
        {"bot_id": "b", "chat_id": "chat", "thread_id": None},
        "question", handle)

    assert len(handle.deliveries) == 1
    assert handle.deliveries[0][1] is True  # is_error
    assert "[/btw]" in handle.deliveries[0][0]


def test_btw_runner_exception_delivers_error(monkeypatch):
    """/btw delivers error when runner.run() raises an exception."""
    bot = object.__new__(bridge.FeishuBot)
    bot.runner = bridge_runtime.ClaudeRunner(
        command="claude", model="sonnet", workspace="/tmp", timeout=30)

    class SessionMapWithSid:
        def get(self, key):
            return "sid-exc"
    bot.session_map = SessionMapWithSid()

    monkeypatch.setattr(bot.runner, "run",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    handler = bridge_commands.BridgeCommandHandler(bot)
    handle = FakeHandle(None, "chat", None, "mid")
    handler._handle_btw(
        {"bot_id": "b", "chat_id": "chat", "thread_id": None},
        "question", handle)

    assert len(handle.deliveries) == 1
    assert handle.deliveries[0][1] is True  # is_error
    assert "调用失败" in handle.deliveries[0][0]


def test_agent_empty_arg_shows_current_agent():
    """/agent with no argument returns current agent info."""
    bot = object.__new__(bridge.FeishuBot)
    bot.agent_config = {"type": "claude", "command": "claude"}
    handler = bridge_commands.BridgeCommandHandler(bot)
    handle = FakeHandle(None, "chat", None, "mid")

    handler._handle_agent("", handle)

    assert len(handle.deliveries) == 1
    assert "当前 Agent" in handle.deliveries[0][0]
    assert "claude" in handle.deliveries[0][0]


def test_agent_switch_success_reports_command():
    """/agent reports success and resolved command path."""
    bot = object.__new__(bridge.FeishuBot)
    bot.agent_config = {"type": "claude", "command": "claude"}
    bot.switch_agent = lambda target: (True, "Agent 已切换为 `codex`。", "/usr/bin/codex")
    handler = bridge_commands.BridgeCommandHandler(bot)
    handle = FakeHandle(None, "chat", None, "mid")

    handler._handle_agent("codex", handle)

    assert len(handle.deliveries) == 1
    assert "Agent 已切换为 `codex`" in handle.deliveries[0][0]
    assert "/usr/bin/codex" in handle.deliveries[0][0]


def test_agent_switch_failure_is_error():
    """/agent surfaces switch errors to the user."""
    bot = object.__new__(bridge.FeishuBot)
    bot.agent_config = {"type": "claude", "command": "claude"}
    bot.switch_agent = lambda target: (False, "切换失败", None)
    handler = bridge_commands.BridgeCommandHandler(bot)
    handle = FakeHandle(None, "chat", None, "mid")

    handler._handle_agent("codex", handle)

    assert handle.deliveries == [("切换失败", True, 0)]


def test_provider_empty_arg_shows_current_provider():
    """/provider with no argument returns current provider info."""
    bot = object.__new__(bridge.FeishuBot)
    bot.agent_config = {"provider": "default", "providers": {"default": {}, "ollama": {}}}
    handler = bridge_commands.BridgeCommandHandler(bot)
    handle = FakeHandle(None, "chat", None, "mid")

    handler._handle_provider("", handle)

    assert len(handle.deliveries) == 1
    assert "当前 Provider" in handle.deliveries[0][0]
    assert "default" in handle.deliveries[0][0]
    assert "ollama" in handle.deliveries[0][0]


def test_provider_switch_success_reports_message():
    """/provider reports success."""
    bot = object.__new__(bridge.FeishuBot)
    bot.agent_config = {"provider": "default", "providers": {"default": {}, "ollama": {}}}
    bot.switch_provider = lambda target: (True, "Provider 已切换为 `ollama`。")
    handler = bridge_commands.BridgeCommandHandler(bot)
    handle = FakeHandle(None, "chat", None, "mid")

    handler._handle_provider("ollama", handle)

    assert handle.deliveries == [("Provider 已切换为 `ollama`。", False, 0)]


def test_provider_switch_failure_is_error():
    """/provider surfaces switch errors to the user."""
    bot = object.__new__(bridge.FeishuBot)
    bot.agent_config = {"provider": "default", "providers": {"default": {}, "ollama": {}}}
    bot.switch_provider = lambda target: (False, "切换失败")
    handler = bridge_commands.BridgeCommandHandler(bot)
    handle = FakeHandle(None, "chat", None, "mid")

    handler._handle_provider("ollama", handle)

    assert handle.deliveries == [("切换失败", True, 0)]


# ---------------------------------------------------------------------------
# local-http-runner integration (T6.6)
# ---------------------------------------------------------------------------

from feishu_bridge.runtime_local import LocalHTTPRunner  # noqa: E402


def test_load_config_local_type(tmp_path):
    """type=local is a valid agent type; PATH check is skipped."""
    config = {
        "bots": [_base_bot_config()],
        "agent": {
            "type": "local",
            "endpoint": {
                "base_url": "http://127.0.0.1:8000",
                "protocol": "anthropic",
            },
        },
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(config))
    result = bridge.load_config(str(cfg_file), "test")
    assert result["agent"]["type"] == "local"
    # No ${...} PATH-resolution required; resolved falls back to sentinel
    assert result["agent"]["_resolved_command"] == "local"


def test_load_config_local_prompt_defaults_are_minimal(tmp_path):
    """type=local uses minimal prompt defaults (no feishu_cli / cron_mgr)."""
    config = {
        "bots": [_base_bot_config()],
        "agent": {
            "type": "local",
            "endpoint": {"base_url": "http://127.0.0.1:8000", "protocol": "openai"},
        },
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(config))
    result = bridge.load_config(str(cfg_file), "test")
    prompt = result["agent"].get("prompt", {})
    assert prompt.get("feishu_cli") is False
    assert prompt.get("cron_mgr") is False
    assert prompt.get("safety") == "minimal"


def test_create_runner_local_builds_http_runner():
    """Factory builds LocalHTTPRunner from default provider endpoint."""
    agent_cfg = {
        "type": "local",
        "_resolved_command": "local",
        "timeout_seconds": 30,
        "providers": {
            "default": {
                "endpoint": {
                    "base_url": "http://127.0.0.1:8000",
                    "protocol": "anthropic",
                    "api_key": "KEY",
                },
            },
        },
    }
    bot_cfg = {"workspace": "/tmp", "model": "gemma-4-26b"}
    runner = bridge.create_runner(agent_cfg, bot_cfg, [])
    assert isinstance(runner, LocalHTTPRunner)
    assert runner._base_url == "http://127.0.0.1:8000"
    assert runner._protocol == "anthropic"
    assert runner._api_key == "KEY"
    assert runner.model == "gemma-4-26b"


def test_local_runner_wants_auth_file_is_false():
    """LocalHTTPRunner opts out of /tmp/feishu_auth_*.json."""
    agent_cfg = {
        "type": "local",
        "_resolved_command": "local",
        "providers": {"default": {"endpoint": {
            "base_url": "http://127.0.0.1:8000", "protocol": "openai",
        }}},
        "timeout_seconds": 30,
    }
    bot_cfg = {"workspace": "/tmp", "model": "gemma-4-26b"}
    runner = bridge.create_runner(agent_cfg, bot_cfg, [])
    assert runner.wants_auth_file() is False
    assert runner.supports_compact() is False


def test_local_build_extra_prompts_empty():
    """With local defaults (feishu_cli/cron_mgr disabled), extra prompts are empty."""
    agent_cfg = {
        "type": "local",
        "_resolved_command": "local",
        "providers": {"default": {}},
        "prompt": {"safety": "minimal", "feishu_cli": False, "cron_mgr": False},
    }
    prompts = bridge.build_extra_prompts(agent_cfg)
    assert prompts == []


def test_context_health_alert_local_runner_omits_compact_hint():
    """Local runner (supports_compact=False) → alert uses /new, not /compact."""
    agent_cfg = {
        "type": "local",
        "_resolved_command": "local",
        "providers": {"default": {"endpoint": {
            "base_url": "http://127.0.0.1:8000", "protocol": "openai",
        }}},
        "timeout_seconds": 30,
    }
    bot_cfg = {"workspace": "/tmp", "model": "gemma-4-26b"}
    runner = bridge.create_runner(agent_cfg, bot_cfg, [])
    result = {
        "usage": {"input_tokens": 10_000, "cache_read_input_tokens": 130_000,
                  "cache_creation_input_tokens": 0},
        "modelUsage": {"gemma-4-26b": {"contextWindow": 200_000}},
    }
    alert = bridge_worker._context_health_alert(result, runner=runner)
    assert alert is not None
    assert "/compact" not in alert
    assert "/new" in alert


def test_switch_agent_claude_to_local_applies_local_defaults(monkeypatch, tmp_path):
    """C1 regression: switching from claude → local must drop Claude's
    materialized prompt defaults (feishu_cli=True, cron_mgr=True, safety=full)
    and pick up Local's (False/False/minimal). Previous code re-normalized
    from the *materialized* prompt dict, which treated the Claude defaults
    as explicit user input and preserved them across the switch."""
    config = {
        "bots": [_base_bot_config()],
        "agent": {"type": "claude"},  # No prompt block — user relies on defaults
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(config))

    # load_config will try to resolve `claude` on PATH; stub it away.
    monkeypatch.setattr(
        bridge, "resolve_effective_agent_command",
        lambda agent_cfg, agent_type: (
            ("claude-stub", "claude") if agent_type == "claude"
            else ("local", "local")
        ),
    )
    monkeypatch.setattr(bridge, "build_extra_prompts", lambda agent_cfg: [])

    result = bridge.load_config(str(cfg_file), "test")
    # Sanity: claude materialized to full defaults.
    assert result["agent"]["prompt"]["feishu_cli"] is True
    assert result["agent"]["prompt"]["cron_mgr"] is True
    assert result["agent"]["prompt"]["safety"] == "full"
    # Raw prompt must be stashed as None (user never supplied one).
    assert result["agent"].get("_prompt_raw") is None

    # Build a FeishuBot shell using load_config output, then switch to local.
    bot = object.__new__(bridge.FeishuBot)
    bot.bot_id = "test-bot"
    bot.bot_config = {"workspace": "/tmp", "model": "gemma-4-26b"}
    bot.agent_config = result["agent"]
    bot.runner = bridge_runtime.ClaudeRunner(
        command="claude", model="claude-opus-4-6", workspace="/tmp", timeout=30
    )
    bot._extra_prompts = []
    bot._session_cost = {}
    bot._session_map_path = tmp_path / "sessions.json"
    bot.session_map = bridge.SessionMap(bot._session_map_path, agent_type="claude")

    ok, msg, _ = bot.switch_agent("local")
    assert ok is True, msg
    prompt = bot.agent_config["prompt"]
    assert prompt["feishu_cli"] is False, prompt
    assert prompt["cron_mgr"] is False, prompt
    assert prompt["safety"] == "minimal", prompt


def test_switch_agent_to_local(monkeypatch, tmp_path):
    """Bot-level switch from claude to local rebuilds LocalHTTPRunner."""
    bot = object.__new__(bridge.FeishuBot)
    bot.bot_id = "test-bot"
    bot.bot_config = {"workspace": "/tmp", "model": "gemma-4-26b"}
    bot.agent_config = {
        "type": "claude",
        "command": "claude",
        "commands": {"claude": "claude", "local": "local"},
        "provider": "default",
        "providers": {
            "default": {},
            "local_endpoint": {
                "type": "local",
                "endpoint": {"base_url": "http://127.0.0.1:8000", "protocol": "openai"},
            },
        },
        "args_by_type": {"claude": []},
        "env_by_type": {"claude": {}},
        "endpoint": {"base_url": "http://127.0.0.1:8000", "protocol": "openai"},
        "_resolved_command": bridge.shutil.which("python3"),
        "timeout_seconds": 30,
    }
    bot.runner = bridge_runtime.ClaudeRunner(
        command="claude", model="claude-opus-4-6", workspace="/tmp", timeout=30
    )
    bot._extra_prompts = []
    bot._session_cost = {"sid-old": {"usage": {}}}
    bot._session_map_path = tmp_path / "sessions.json"
    bot._session_map_path.write_text(json.dumps({
        "_agent_type": "claude",
        "chat-key": "sid-old",
    }))
    bot.session_map = bridge.SessionMap(bot._session_map_path, agent_type="claude")

    monkeypatch.setattr(
        bridge, "resolve_effective_agent_command",
        lambda agent_cfg, agent_type: ("local", "local"),
    )
    monkeypatch.setattr(bridge, "build_extra_prompts", lambda agent_cfg: [])

    ok, message, _resolved = bot.switch_agent("local")
    assert ok is True
    assert "local" in message
    assert isinstance(bot.runner, LocalHTTPRunner)
    assert bot.agent_config["type"] == "local"
    # Prompt re-normalized to local defaults
    assert bot.agent_config["prompt"]["feishu_cli"] is False
    assert bot.agent_config["prompt"]["cron_mgr"] is False
    # Session cleared because agent_type changed
    assert bot._session_cost == {}
    assert bot.session_map.get(("chat-key",)) is None



class _RichHandle(FakeHandle):
    """FakeHandle variant that accepts the worker's full deliver() kwargs."""

    def deliver(self, content, is_error=False, **kwargs):
        self.deliveries.append((content, is_error, kwargs))

    def tool_status_update(self, *a, **k):
        pass

    def todo_list_update(self, *a, **k):
        pass

    def agent_list_update(self, *a, **k):
        pass


def test_process_message_stale_local_sid():
    """Stale sid → runner.has_session False → demote resume=False + prepend rebuild notice."""

    class StaleSessionMap:
        def __init__(self):
            self.saved = []

        def get(self, key):
            return "stale-sid-abc"

        def put(self, key, session_id):
            self.saved.append((key, session_id))

        def delete(self, key):
            pass

    run_calls = []

    class StaleLocalRunner:
        workspace = "/tmp"

        def has_session(self, sid):
            return False

        def wants_auth_file(self):
            return False

        def supports_compact(self):
            return False

        def get_session_not_found_signatures(self):
            return []

        def run(self, text, **kwargs):
            run_calls.append(kwargs)
            return {
                "result": "canned reply",
                "session_id": kwargs["session_id"],
                "is_error": False,
            }

    runner = StaleLocalRunner()
    handle = bridge_worker.process_message(
        item={
            "bot_id": "bot", "chat_id": "chat", "thread_id": None,
            "message_id": "mid-1", "text": "hi",
        },
        bot_config={"workspace": "/tmp"},
        lark_client=None,
        session_map=StaleSessionMap(),
        runner=runner,
        response_handle_cls=_RichHandle,
        download_image_fn=lambda *a, **k: None,
        fetch_card_content_fn=lambda *a, **k: None,
        fetch_forward_messages_fn=lambda *a, **k: None,
        fetch_quoted_message_fn=lambda *a, **k: None,
        remove_typing_indicator_fn=lambda *a, **k: None,
    )

    assert len(run_calls) == 1
    # Stale sid detected → resume demoted to False
    assert run_calls[0]["resume"] is False
    # Reused existing sid rather than minting new one (worker reuses existing_sid)
    assert run_calls[0]["session_id"] == "stale-sid-abc"
    # Delivered text must be prefixed with rebuild notice
    assert handle.deliveries, "expected a delivery"
    delivered_text = handle.deliveries[0][0]
    assert delivered_text.startswith("⚠️ 会话已重建"), delivered_text


def test_cost_store_no_ops_for_local():
    """Runner result without usage/total_cost_usd → cost_store untouched (no-op by design)."""

    class LocalStyleRunner:
        workspace = "/tmp"

        def has_session(self, sid):
            return True

        def wants_auth_file(self):
            return False

        def supports_compact(self):
            return False

        def get_session_not_found_signatures(self):
            return []

        def run(self, text, **kwargs):
            # Local runner: no usage, no total_cost_usd
            return {
                "result": "local reply",
                "session_id": kwargs["session_id"],
                "is_error": False,
            }

    cost_store = {}

    # Exercise without exception
    bridge_worker.process_message(
        item={
            "bot_id": "bot", "chat_id": "chat", "thread_id": None,
            "message_id": "mid-1", "text": "hi",
            "_cost_store": cost_store,
        },
        bot_config={"workspace": "/tmp"},
        lark_client=None,
        session_map=DummySessionMap(),
        runner=LocalStyleRunner(),
        response_handle_cls=_RichHandle,
        download_image_fn=lambda *a, **k: None,
        fetch_card_content_fn=lambda *a, **k: None,
        fetch_forward_messages_fn=lambda *a, **k: None,
        fetch_quoted_message_fn=lambda *a, **k: None,
        remove_typing_indicator_fn=lambda *a, **k: None,
    )

    # Cost update guarded by `result.get("usage") or result.get("total_cost_usd")` —
    # both absent means no entry is recorded (no-op by design).
    assert cost_store == {}


# --- Context window / threshold / ledger tests ---

def test_context_window_for_model_sonnet_is_1m():
    assert bridge_commands._context_window_for_model("claude-sonnet-4-6") == 1_000_000
    assert bridge_commands._context_window_for_model("claude-opus-4-6") == 1_000_000
    assert bridge_commands._context_window_for_model("claude-haiku-4-5") == 200_000


def test_context_window_for_model_unknown_defaults_200k():
    assert bridge_commands._context_window_for_model("some-other-model") == 200_000


def test_runner_default_context_window_sonnet():
    runner = bridge_runtime.ClaudeRunner(
        command="claude", model="claude-sonnet-4-6", timeout=60, workspace="/tmp",
    )
    assert runner.get_default_context_window() == 1_000_000


def test_compact_alert_thresholds_default(monkeypatch):
    monkeypatch.delenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", raising=False)
    red, yellow = bridge_worker._compact_alert_thresholds()
    assert red == 85 and yellow == 70


def test_compact_alert_thresholds_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "85")
    red, yellow = bridge_worker._compact_alert_thresholds()
    assert red == 78 and yellow == 63


def test_compact_alert_thresholds_invalid_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "not-an-int")
    red, yellow = bridge_worker._compact_alert_thresholds()
    assert red == 85 and yellow == 70


def test_ledger_roundtrip(tmp_path):
    from feishu_bridge.ledger import Ledger

    db = Ledger.open(tmp_path / "t.db")
    common = dict(
        bot_id="b", chat_id="c", thread_id=None, model="m",
        cost_usd=0.01, duration_ms=100,
    )
    db.record_turn(
        session_id="s1",
        usage={
            "input_tokens": 1000, "cache_read_input_tokens": 9000,
            "cache_creation_input_tokens": 5000, "output_tokens": 200,
        },
        compact_event=False, **common,
    )
    db.record_turn(
        session_id="s1",
        usage={
            "input_tokens": 500, "cache_read_input_tokens": 2000,
            "cache_creation_input_tokens": 0, "output_tokens": 50,
        },
        compact_event=True, **common,
    )
    # prev_ctx returns the turn BEFORE the latest -> 1000 + 9000
    assert db.prev_ctx_tokens("s1") == 10_000
    assert db.compact_count("s1") == 1
    assert db.prev_ctx_tokens("missing") == 0


def test_ledger_record_turn_never_raises(tmp_path):
    """Malformed usage dict must be swallowed, not propagated."""
    from feishu_bridge.ledger import Ledger

    db = Ledger.open(tmp_path / "t.db")
    # usage=None would raise AttributeError on .get() — must be contained.
    db.record_turn(
        session_id="s1", bot_id="b", chat_id="c", thread_id=None, model="m",
        usage=None, cost_usd=0.0, compact_event=False, duration_ms=0,
    )
    # compact_count still works after the failed write.
    assert db.compact_count("s1") == 0


def test_ledger_prev_ctx_rowid_tiebreak(tmp_path):
    """Rapid back-to-back writes with colliding ts must still return
    the previous turn deterministically (rowid tiebreak)."""
    from feishu_bridge.ledger import Ledger
    import time as _time

    db = Ledger.open(tmp_path / "t.db")
    # Force identical ts by monkeypatching time.time
    frozen = 1_700_000_000.0
    orig = _time.time
    try:
        _time.time = lambda: frozen
        for i, ctx in enumerate([1000, 2000, 3000]):
            db.record_turn(
                session_id="s1", bot_id="b", chat_id="c", thread_id=None,
                model="m",
                usage={"input_tokens": ctx, "cache_read_input_tokens": 0,
                       "cache_creation_input_tokens": 0, "output_tokens": 0},
                cost_usd=0.0, compact_event=False, duration_ms=0,
            )
    finally:
        _time.time = orig
    # Latest = 3000 (third insert), prev = 2000 (second insert).
    assert db.prev_ctx_tokens("s1") == 2000
