#!/usr/bin/env python3
"""Unit tests for LocalHTTPRunner + protocol adapters + session store."""

from __future__ import annotations

import io
import json
import threading
import time

import pytest

from feishu_bridge.runtime_local import (
    AnthropicAdapter,
    LocalHTTPRunner,
    OpenAIAdapter,
    _build_adapter,
    _HTTPCall,
    _SessionStore,
    _sse_iter,
)


# ---------------------------------------------------------------------------
# Adapter tests
# ---------------------------------------------------------------------------

def test_anthropic_adapter_build_request():
    a = AnthropicAdapter()
    url, headers, body = a.build_request(
        base_url="http://localhost:8000", model="g4", system="SYS",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=512, api_key="KEY", stream=True,
    )
    assert url == "http://localhost:8000/v1/messages"
    assert headers["x-api-key"] == "KEY"
    assert headers["anthropic-version"] == "2023-06-01"
    payload = json.loads(body)
    assert payload["model"] == "g4"
    assert payload["stream"] is True
    assert payload["system"] == "SYS"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


def test_openai_adapter_build_request_with_and_without_stream_options():
    a = OpenAIAdapter(include_usage=True)
    _, _, body = a.build_request(
        base_url="http://x", model="m", system="SYS",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=100, api_key="", stream=True,
    )
    payload = json.loads(body)
    assert payload["stream_options"] == {"include_usage": True}
    # System prepended
    assert payload["messages"][0] == {"role": "system", "content": "SYS"}

    a2 = OpenAIAdapter(include_usage=False)
    _, _, body = a2.build_request(
        base_url="http://x", model="m", system="",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=100, api_key="k", stream=True,
    )
    payload = json.loads(body)
    assert "stream_options" not in payload
    # No system
    assert payload["messages"][0]["role"] == "user"


def test_anthropic_adapter_parse_stream_event_text_and_done():
    a = AnthropicAdapter()
    state: dict = {}
    a.parse_stream_event("message_start", json.dumps({
        "type": "message_start",
        "message": {"usage": {"input_tokens": 12, "output_tokens": 0}},
    }), state)
    assert state["input_tokens"] == 12

    a.parse_stream_event("content_block_delta", json.dumps({
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": "hello"},
    }), state)
    a.parse_stream_event("content_block_delta", json.dumps({
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": " world"},
    }), state)
    assert state["text"] == "hello world"

    a.parse_stream_event("message_delta", json.dumps({
        "type": "message_delta", "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": 42},
    }), state)
    assert state["output_tokens"] == 42
    assert state["stop_reason"] == "end_turn"

    a.parse_stream_event("message_stop", json.dumps({"type": "message_stop"}), state)
    assert state["done"] is True


def test_openai_adapter_parse_stream_event_and_done_sentinel():
    a = OpenAIAdapter()
    state: dict = {}
    a.parse_stream_event("message", json.dumps({
        "choices": [{"delta": {"content": "foo"}}]
    }), state)
    a.parse_stream_event("message", json.dumps({
        "choices": [{"delta": {"content": "bar"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 4},
    }), state)
    assert state["text"] == "foobar"
    assert state["stop_reason"] == "stop"
    assert state["input_tokens"] == 8
    assert state["output_tokens"] == 4

    a.parse_stream_event("message", "[DONE]", state)
    assert state["done"] is True


def test_adapter_ignores_malformed_json():
    a = AnthropicAdapter()
    state: dict = {}
    # Must not raise
    a.parse_stream_event("message", "{ not json }", state)
    assert state == {}


def test_build_adapter_unknown_protocol_raises():
    with pytest.raises(ValueError):
        _build_adapter("grpc", True)


# ---------------------------------------------------------------------------
# SSE parser
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Iterable bytes-line response suitable for _sse_iter."""

    def __init__(self, lines: list[bytes]):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass


def test_sse_iter_basic_frames_and_heartbeat():
    lines = [
        b": heartbeat\n",
        b"event: message_start\n",
        b"data: {\"a\":1}\n",
        b"\n",
        b"data: hello\n",
        b"data: world\n",
        b"\n",
    ]
    out = list(_sse_iter(_FakeResponse(lines)))
    assert out == [
        ("message_start", '{"a":1}'),
        ("message", "hello\nworld"),
    ]


def test_sse_iter_handles_crlf_and_done_sentinel_passthrough():
    lines = [b"data: [DONE]\r\n", b"\r\n"]
    out = list(_sse_iter(_FakeResponse(lines)))
    assert out == [("message", "[DONE]")]


def test_sse_iter_partial_frame_discarded_on_eof():
    lines = [b"data: partial\n"]  # no blank terminator
    out = list(_sse_iter(_FakeResponse(lines)))
    assert out == []


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------

def test_session_store_exists_and_reset():
    s = _SessionStore()
    assert not s.exists("sid1")
    s.append("sid1", "user", "hi")
    assert s.exists("sid1")
    s.reset("sid1")
    assert not s.exists("sid1")


def test_session_store_append_accumulates_and_lru_evicts():
    s = _SessionStore(max_sessions=2, max_messages=10)
    s.append("a", "user", "a1")
    s.append("b", "user", "b1")
    s.append("c", "user", "c1")
    assert not s.exists("a")  # evicted
    assert s.exists("b") and s.exists("c")


def test_session_store_truncates_messages():
    s = _SessionStore(max_messages=4)
    for i in range(10):
        s.append("sid", "user", str(i))
    msgs = s.get("sid")
    assert len(msgs) == 4
    assert msgs[-1]["content"] == "9"
    assert msgs[0]["content"] == "6"


def test_session_store_get_returns_copy():
    s = _SessionStore()
    s.append("sid", "user", "hi")
    copy = s.get("sid")
    copy.append({"role": "assistant", "content": "tampered"})
    assert len(s.get("sid")) == 1


# ---------------------------------------------------------------------------
# _HTTPCall
# ---------------------------------------------------------------------------

def test_httpcall_cancel_and_wallclock():
    call = _HTTPCall(socket_timeout=5.0, wall_clock_timeout=0.01)
    assert call.socket_timeout <= 2.0  # capped
    assert not call.is_cancelled()
    call.cancel()
    assert call.is_cancelled()
    time.sleep(0.02)
    assert call.wall_clock_exceeded()


# ---------------------------------------------------------------------------
# LocalHTTPRunner — end-to-end with mocked _do_request / urlopen
# ---------------------------------------------------------------------------

def _make_runner(protocol="anthropic", **kw):
    return LocalHTTPRunner(
        command="local", model="gemma", workspace="/tmp", timeout=30,
        base_url="http://127.0.0.1:8000", protocol=protocol, **kw,
    )


def test_runner_stub_abstracts_are_unreachable():
    r = _make_runner()
    with pytest.raises(AssertionError):
        r.build_args("x", None, False, False)
    with pytest.raises(AssertionError):
        r.parse_streaming_line({}, None)
    with pytest.raises(AssertionError):
        r.parse_blocking_output("", None)


def test_runner_has_session_wants_auth_file_compact():
    r = _make_runner()
    assert r.has_session("nope") is False
    assert r.wants_auth_file() is False
    assert r.supports_compact() is False
    assert r.get_display_name() == "Local LLM"
    assert r.get_default_context_window() == 8192


def test_runner_run_requires_session_id():
    r = _make_runner()
    with pytest.raises(ValueError):
        r.run("hi", session_id=None)


def test_runner_run_success_populates_session(monkeypatch):
    r = _make_runner()

    def fake_do_request(messages, tag, stream, system):
        return (
            "hello back",  # text
            10, 5, "end_turn",
            False, "", False, False,
        )

    monkeypatch.setattr(r, "_do_request", fake_do_request)
    result = r.run("hi", session_id="sid-1")
    assert result["is_error"] is False
    assert result["result"] == "hello back"
    assert result["usage"]["input_tokens"] == 10
    assert result["usage"]["output_tokens"] == 5
    assert r.has_session("sid-1")

    # Second resume=True accumulates
    captured = {}

    def fake_do_request2(messages, tag, stream, system):
        captured["messages"] = messages
        return ("second", 2, 3, None, False, "", False, False)

    monkeypatch.setattr(r, "_do_request", fake_do_request2)
    r.run("follow up", session_id="sid-1", resume=True)
    # Should include prior user+assistant + new user
    assert [m["role"] for m in captured["messages"]] == ["user", "assistant", "user"]
    assert captured["messages"][-1]["content"] == "follow up"


def test_runner_run_error_does_not_append_history(monkeypatch):
    r = _make_runner()

    def fake_err(messages, tag, stream, system):
        return ("", 0, 0, None, True, "HTTP 503: down", False, False)

    monkeypatch.setattr(r, "_do_request", fake_err)
    result = r.run("hi", session_id="sid-err")
    assert result["is_error"] is True
    assert "503" in result["result"]
    assert not r.has_session("sid-err")


def test_runner_run_cancelled(monkeypatch):
    r = _make_runner()

    def fake_cancel(messages, tag, stream, system):
        return ("", 0, 0, None, False, "", True, False)

    monkeypatch.setattr(r, "_do_request", fake_cancel)
    result = r.run("hi", session_id="sid-c", tag="t1")
    assert result["cancelled"] is True
    assert result["is_error"] is False


def test_runner_cancel_sets_cancel_event():
    r = _make_runner()
    call = _HTTPCall(socket_timeout=2.0, wall_clock_timeout=10)
    r._active_calls["tag-x"] = call
    assert r.cancel("tag-x") is True
    assert call.is_cancelled()
    assert r.cancel("missing") is False


def test_runner_openai_400_fallback_retries_without_stream_options(monkeypatch):
    r = _make_runner(protocol="openai", openai_include_usage=True)
    calls: list[bool] = []

    def fake(messages, tag, stream, system):
        # First call: fail with HTTP 400, second call: succeed
        if not calls:
            calls.append(True)
            return ("", 0, 0, None, True, "HTTP 400: unknown stream_options", False, False)
        calls.append(True)
        return ("ok", 1, 2, None, False, "", False, False)

    monkeypatch.setattr(r, "_do_request", fake)
    result = r.run("hi", session_id="sid-f")
    assert result["is_error"] is False
    assert result["result"] == "ok"
    assert len(calls) == 2
    # include_usage should be flipped off after fallback
    assert r._adapter.include_usage is False


def test_runner_empty_response_is_error(monkeypatch):
    r = _make_runner()

    def fake(messages, tag, stream, system):
        return ("", 0, 0, None, False, "", False, False)

    monkeypatch.setattr(r, "_do_request", fake)
    result = r.run("hi", session_id="sid-e")
    assert result["is_error"] is True


def test_sse_iter_propagates_socket_timeout():
    """H1 regression: _sse_iter must NOT swallow socket.timeout as clean EOF.

    The previous ``except Exception`` masked wall-clock / socket timeouts,
    connection resets, and close()-from-cancel as clean EOF, causing the
    streaming loop to see zero events and fall through to the misleading
    "未返回任何内容" error branch.
    """
    import socket

    class _RaisingResponse:
        def __iter__(self):
            yield b"data: hello\n"
            yield b"\n"
            raise socket.timeout("read timed out")

    gen = _sse_iter(_RaisingResponse())
    assert next(gen) == ("message", "hello")
    with pytest.raises(socket.timeout):
        next(gen)


def test_do_request_stream_cancel_after_bind_is_reported_as_cancelled(monkeypatch):
    """H2 regression: cancel firing between bind() and first yield must
    return cancelled=True, not a transport error or empty-text result."""

    r = _make_runner()

    class _FakeResponse:
        def __init__(self):
            self.closed = False

        def __iter__(self):
            # If we got here, the cancel-after-bind check failed.
            raise AssertionError("stream iteration should not start after cancel")

        def close(self):
            self.closed = True

    resp = _FakeResponse()

    def fake_open_request(url, headers, body, call):
        # Simulate cancel firing between open_request returning and the
        # streaming loop starting.
        call.cancel()
        return resp

    monkeypatch.setattr(r, "_open_request", fake_open_request)
    result = r.run("hi", session_id="sid-h2", tag="tag-h2", on_output=lambda s: None)
    assert result["cancelled"] is True, result
    assert result["is_error"] is False


def test_do_request_stream_socket_timeout_becomes_error(monkeypatch):
    """H1 regression end-to-end: socket.timeout during stream iteration must
    surface as a structured error (is_error=True, timeout-flavoured message)
    rather than being swallowed and yielding 未返回任何内容."""
    import socket

    r = _make_runner()

    class _TimingOutResponse:
        def __iter__(self):
            raise socket.timeout("read timed out")

        def close(self):
            pass

    monkeypatch.setattr(
        r, "_open_request",
        lambda url, headers, body, call: _TimingOutResponse(),
    )
    result = r.run("hi", session_id="sid-t", on_output=lambda s: None)
    assert result["is_error"] is True
    assert "超时" in result["result"]
    # Must not be the misleading "未返回任何内容" message.
    assert "未返回任何内容" not in result["result"]


def test_runner_on_output_invoked_with_final_text(monkeypatch):
    r = _make_runner()

    def fake(messages, tag, stream, system):
        assert stream is True
        return ("streamed", 1, 1, None, False, "", False, False)

    monkeypatch.setattr(r, "_do_request", fake)
    captured = []
    r.run("hi", session_id="sid-s", on_output=captured.append)
    assert captured == ["streamed"]
