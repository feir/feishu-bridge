"""Local HTTP LLM runner for feishu-bridge.

Implements a generic HTTP runner that talks directly to local LLM endpoints
(omlx, ollama, vllm, …) via anthropic or openai compatible protocols, bypassing
the Claude CLI framework entirely. This avoids the ~26K-token prefill overhead
that the CLI's system prompt + tools schema imposes on small local models.

The three BaseRunner abstract methods (``build_args``, ``parse_streaming_line``,
``parse_blocking_output``) are **never reached** — ``run()`` is fully overridden
for the HTTP path. They are stubbed and never exercised; the unit tests assert
this via a dedicated reachability check. If you subclass this runner and want to
bring back CLI behaviour, override ``run()`` to restore the super() dispatch.
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any, Callable, Iterable, Optional

from feishu_bridge.runtime import BaseRunner

log = logging.getLogger("feishu-bridge")


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------

class _SessionStore:
    """In-memory per-session message history. Process-local, LRU-evicting."""

    MAX_SESSIONS = 100
    MAX_MESSAGES_PER_SESSION = 50

    def __init__(self, max_sessions: int = MAX_SESSIONS,
                 max_messages: int = MAX_MESSAGES_PER_SESSION):
        self._sessions: "OrderedDict[str, list[dict]]" = OrderedDict()
        self._lock = threading.Lock()
        self._max_sessions = max_sessions
        self._max_messages = max_messages

    def exists(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._sessions

    def get(self, session_id: str) -> list[dict]:
        with self._lock:
            msgs = self._sessions.get(session_id)
            if msgs is not None:
                self._sessions.move_to_end(session_id)
                return list(msgs)
            return []

    def append(self, session_id: str, role: str, content: str) -> None:
        with self._lock:
            msgs = self._sessions.get(session_id)
            if msgs is None:
                msgs = []
                self._sessions[session_id] = msgs
                # Evict oldest
                while len(self._sessions) > self._max_sessions:
                    self._sessions.popitem(last=False)
            msgs.append({"role": role, "content": content})
            # Truncate oldest messages; preserve most recent exchanges.
            if len(msgs) > self._max_messages:
                del msgs[: len(msgs) - self._max_messages]
            self._sessions.move_to_end(session_id)

    def reset(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)


# ---------------------------------------------------------------------------
# HTTP call state (cancel + timeout)
# ---------------------------------------------------------------------------

class _HTTPCall:
    """Tracks a single in-flight HTTP request for cancel / timeout handling."""

    def __init__(self, socket_timeout: float, wall_clock_timeout: float):
        self.socket_timeout = min(socket_timeout, 2.0) if socket_timeout else 2.0
        self.wall_clock_timeout = wall_clock_timeout
        self._cancel_event = threading.Event()
        self._response: Optional[http.client.HTTPResponse] = None
        self._t0 = time.monotonic()

    def bind(self, response) -> None:
        self._response = response

    def cancel(self) -> None:
        self._cancel_event.set()
        resp = self._response
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def wall_clock_exceeded(self) -> bool:
        return (time.monotonic() - self._t0) > self.wall_clock_timeout


# ---------------------------------------------------------------------------
# SSE parser — generic, protocol-agnostic
# ---------------------------------------------------------------------------

def _sse_iter(response: Any) -> Iterable[tuple[str, str]]:
    """Yield ``(event_type, data)`` tuples from an SSE ``HTTPResponse``.

    Follows the W3C SSE spec closely enough for anthropic / openai streams:
    - Frame boundary is a blank line; multiple ``data:`` lines join with ``\n``.
    - Lines starting with ``:`` are heartbeats (ignored).
    - ``event:`` sets the event type for the next frame.
    - ``id:`` / ``retry:`` are ignored (we never reconnect).
    - Malformed / partial frames at EOF are dropped silently.
    """
    event_type = ""
    data_lines: list[str] = []
    # Note: we intentionally do NOT catch transport-level exceptions here.
    # socket.timeout / OSError / ConnectionResetError / close()-from-cancel
    # must propagate to the caller so it can distinguish wall-clock timeout,
    # user cancel (close-from-cancel manifests as OSError), and actual I/O
    # errors — all were previously masked as clean EOF.
    for raw in response:
        if not raw:
            # Blank bytes line from iterator → frame boundary
            if data_lines or event_type:
                yield event_type or "message", "\n".join(data_lines)
                event_type = ""
                data_lines = []
            continue
        # bytes → str, strip CRLF/LF
        if isinstance(raw, bytes):
            try:
                line = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
        else:
            line = raw
        line = line.rstrip("\r\n")
        if line == "":
            if data_lines or event_type:
                yield event_type or "message", "\n".join(data_lines)
                event_type = ""
                data_lines = []
            continue
        if line.startswith(":"):
            continue  # heartbeat / comment
        if line.startswith("event:"):
            event_type = line[len("event:"):].lstrip()
            continue
        if line.startswith("data:"):
            # Spec: strip a single leading space only
            rest = line[len("data:"):]
            if rest.startswith(" "):
                rest = rest[1:]
            data_lines.append(rest)
            continue
        # id: / retry: / unknown — ignore
    # Partial frame at EOF is discarded.


# ---------------------------------------------------------------------------
# Protocol adapter ABC
# ---------------------------------------------------------------------------

class ProtocolAdapter(ABC):
    """Protocol-specific request + SSE event mapping."""

    name: str = ""

    @abstractmethod
    def build_request(self, *, base_url: str, model: str, system: str,
                      messages: list[dict], max_tokens: int, api_key: str,
                      stream: bool) -> tuple[str, dict, bytes]:
        """Return ``(url, headers, body_bytes)`` for the HTTP request."""

    @abstractmethod
    def parse_stream_event(self, event_type: str, data: str,
                           state: dict) -> None:
        """Mutate ``state`` in place from one SSE frame.

        State keys used:
          - ``text``: accumulated assistant text
          - ``input_tokens`` / ``output_tokens`` (optional)
          - ``done``: bool, set on terminal event
          - ``stop_reason``: optional string
        """

    @abstractmethod
    def parse_blocking_response(self, data: dict) -> dict:
        """Return ``{text, input_tokens, output_tokens, stop_reason}`` from a
        non-streaming response body."""


class AnthropicAdapter(ProtocolAdapter):
    name = "anthropic"

    def build_request(self, *, base_url, model, system, messages, max_tokens,
                      api_key, stream):
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            "stream": bool(stream),
        }
        if system:
            body["system"] = system
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if api_key:
            headers["x-api-key"] = api_key
        url = f"{base_url.rstrip('/')}/v1/messages"
        return url, headers, json.dumps(body).encode("utf-8")

    def parse_stream_event(self, event_type, data, state):
        if not data:
            return
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            log.debug("anthropic sse: malformed json: %s", data[:120])
            return
        etype = payload.get("type") or event_type
        if etype == "message_start":
            usage = (payload.get("message") or {}).get("usage") or {}
            if "input_tokens" in usage:
                state["input_tokens"] = usage["input_tokens"]
            if "output_tokens" in usage:
                state["output_tokens"] = usage["output_tokens"]
        elif etype == "content_block_delta":
            delta = payload.get("delta") or {}
            if delta.get("type") == "text_delta":
                state["text"] = state.get("text", "") + delta.get("text", "")
        elif etype == "message_delta":
            usage = payload.get("usage") or {}
            if "output_tokens" in usage:
                state["output_tokens"] = usage["output_tokens"]
            delta = payload.get("delta") or {}
            if delta.get("stop_reason"):
                state["stop_reason"] = delta["stop_reason"]
        elif etype == "message_stop":
            state["done"] = True

    def parse_blocking_response(self, data):
        content = data.get("content") or []
        text = "".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        usage = data.get("usage") or {}
        return {
            "text": text,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "stop_reason": data.get("stop_reason"),
        }


class OpenAIAdapter(ProtocolAdapter):
    name = "openai"

    def __init__(self, include_usage: bool = True):
        self.include_usage = bool(include_usage)

    def build_request(self, *, base_url, model, system, messages, max_tokens,
                      api_key, stream):
        msgs: list[dict] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": msgs,
            "stream": bool(stream),
        }
        if stream and self.include_usage:
            body["stream_options"] = {"include_usage": True}
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        url = f"{base_url.rstrip('/')}/v1/chat/completions"
        return url, headers, json.dumps(body).encode("utf-8")

    def parse_stream_event(self, event_type, data, state):
        if not data:
            return
        if data.strip() == "[DONE]":
            state["done"] = True
            return
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            log.debug("openai sse: malformed json: %s", data[:120])
            return
        choices = payload.get("choices") or []
        for choice in choices:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                state["text"] = state.get("text", "") + content
            finish = choice.get("finish_reason")
            if finish:
                state["stop_reason"] = finish
        usage = payload.get("usage")
        if usage:
            if "prompt_tokens" in usage:
                state["input_tokens"] = usage["prompt_tokens"]
            if "completion_tokens" in usage:
                state["output_tokens"] = usage["completion_tokens"]

    def parse_blocking_response(self, data):
        choices = data.get("choices") or []
        text = ""
        stop_reason = None
        if choices:
            msg = choices[0].get("message") or {}
            text = msg.get("content", "") or ""
            stop_reason = choices[0].get("finish_reason")
        usage = data.get("usage") or {}
        return {
            "text": text,
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "stop_reason": stop_reason,
        }


def _build_adapter(protocol: str, include_usage: bool) -> ProtocolAdapter:
    p = (protocol or "").strip().lower()
    if p == "anthropic":
        return AnthropicAdapter()
    if p == "openai":
        return OpenAIAdapter(include_usage=include_usage)
    raise ValueError(f"Unknown protocol: {protocol!r} (expected 'anthropic' or 'openai')")


# ---------------------------------------------------------------------------
# LocalHTTPRunner
# ---------------------------------------------------------------------------

class LocalHTTPRunner(BaseRunner):
    """Generic HTTP runner for local LLM endpoints.

    Stubbed abstract methods (never reached — ``run()`` is fully overridden):
      - ``build_args``
      - ``parse_streaming_line``
      - ``parse_blocking_output``
    """

    DEFAULT_MODEL = "gemma-4-26b"
    ALWAYS_STREAMING = False

    def __init__(self, command, model, workspace, timeout,
                 max_budget_usd=None,
                 extra_system_prompts=None,
                 extra_cli_args=None,
                 fixed_env=None,
                 safety_prompt_mode: str = "minimal",
                 setting_sources=None,
                 *,
                 base_url: str,
                 protocol: str,
                 api_key: str = "",
                 max_tokens: int = 4096,
                 context_window: int = 8192,
                 openai_include_usage: bool = True,
                 model_aliases: Optional[dict[str, str]] = None):
        super().__init__(
            command=command, model=model, workspace=workspace, timeout=timeout,
            max_budget_usd=None,  # local inference: no monetary cost tracking
            extra_system_prompts=extra_system_prompts,
            extra_cli_args=extra_cli_args,
            fixed_env=fixed_env,
            safety_prompt_mode=safety_prompt_mode,
            setting_sources=setting_sources,
        )
        if not base_url:
            raise ValueError("LocalHTTPRunner requires base_url")
        self._base_url = base_url.rstrip("/")
        self._protocol = (protocol or "anthropic").strip().lower()
        self._api_key = api_key or ""
        self._max_tokens = int(max_tokens)
        self._context_window = int(context_window)
        self._openai_include_usage = bool(openai_include_usage)
        self._model_aliases = dict(model_aliases or {})
        self._adapter: ProtocolAdapter = _build_adapter(
            self._protocol, self._openai_include_usage
        )
        self._sessions = _SessionStore()
        self._active_calls: dict[str, _HTTPCall] = {}
        self._active_lock = threading.Lock()

    # ── Stub abstracts (never reached) ──
    def build_args(self, *a, **kw):  # pragma: no cover - unreachable
        raise AssertionError("LocalHTTPRunner.build_args must never be called")

    def parse_streaming_line(self, *a, **kw):  # pragma: no cover - unreachable
        raise AssertionError("LocalHTTPRunner.parse_streaming_line must never be called")

    def parse_blocking_output(self, *a, **kw):  # pragma: no cover - unreachable
        raise AssertionError("LocalHTTPRunner.parse_blocking_output must never be called")

    # ── Runner capability hooks ──
    def get_model_aliases(self) -> dict[str, str]:
        return dict(self._model_aliases)

    def get_default_context_window(self) -> int:
        return self._context_window

    def get_display_name(self) -> str:
        return "Local LLM"

    def supports_compact(self) -> bool:
        return False

    def has_session(self, session_id: str) -> bool:
        if not session_id:
            return False
        return self._sessions.exists(session_id)

    def wants_auth_file(self) -> bool:
        return False

    # ── Cancel ──
    def cancel(self, tag: str) -> bool:
        if not tag:
            return False
        with self._active_lock:
            call = self._active_calls.get(tag)
        if call is None:
            return False
        log.info("Cancelling local HTTP call: tag=%s", tag)
        call.cancel()
        return True

    # ── HTTP I/O ──
    def _open_request(self, url: str, headers: dict, body: bytes,
                      call: _HTTPCall):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        return urllib.request.urlopen(req, timeout=call.socket_timeout)

    def _do_request(self, messages: list[dict], tag: Optional[str],
                    stream: bool, system: str):
        """Execute one HTTP request. Returns (text, input_tok, output_tok,
        stop_reason, is_error, err_text, cancelled, usage_estimated)."""
        model = self.model or self.DEFAULT_MODEL
        url, headers, body = self._adapter.build_request(
            base_url=self._base_url, model=model, system=system,
            messages=messages, max_tokens=self._max_tokens,
            api_key=self._api_key, stream=stream,
        )
        call = _HTTPCall(socket_timeout=2.0, wall_clock_timeout=float(self.timeout))
        if tag:
            with self._active_lock:
                self._active_calls[tag] = call

        state: dict[str, Any] = {"text": ""}
        cancelled = False
        is_error = False
        err_text = ""
        response = None
        try:
            try:
                response = self._open_request(url, headers, body, call)
            except urllib.error.HTTPError as e:
                is_error = True
                try:
                    err_body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    err_body = ""
                err_text = f"HTTP {e.code}: {err_body[:500]}"
                return "", 0, 0, None, is_error, err_text, False, False
            except (urllib.error.URLError, OSError) as e:
                is_error = True
                err_text = f"endpoint 不可达（{self._base_url}）: {e}"
                return "", 0, 0, None, is_error, err_text, False, False

            call.bind(response)

            # H2: cancel may have fired between open_request and bind; check
            # immediately so we don't start iterating a stream that was closed
            # under us (the resulting OSError would otherwise be indistinguishable
            # from a transport error).
            if call.is_cancelled():
                return "", 0, 0, None, False, "", True, False

            if stream:
                try:
                    for event_type, data in _sse_iter(response):
                        if call.is_cancelled():
                            cancelled = True
                            break
                        if call.wall_clock_exceeded():
                            err_text = (
                                f"Local LLM 总时长超限（{int(self.timeout)}s）"
                            )
                            is_error = True
                            break
                        self._adapter.parse_stream_event(event_type, data, state)
                        if state.get("done"):
                            break
                except (socket.timeout, TimeoutError):
                    is_error = True
                    err_text = f"Local LLM 响应超时（socket timeout, {int(self.timeout)}s wall clock）"
                except OSError as e:
                    # close()-from-cancel surfaces as OSError on the read side.
                    # Distinguish user-initiated cancel from real transport errors.
                    if call.is_cancelled():
                        cancelled = True
                    else:
                        is_error = True
                        err_text = f"Local LLM 传输错误: {e}"
                # H2: re-check cancel after loop exits cleanly — cancel may have
                # fired between the last iteration's is_cancelled() check and
                # the natural end of the stream, in which case we should report
                # cancelled rather than fall through to empty-text handling.
                if not cancelled and not is_error and call.is_cancelled():
                    cancelled = True
            else:
                # H3: response.read() can raise socket.timeout / OSError in
                # blocking mode; only JSONDecodeError was previously handled.
                try:
                    raw = response.read()
                except (socket.timeout, TimeoutError) as e:
                    is_error = True
                    err_text = f"Local LLM 响应超时: {e}"
                    return "", 0, 0, None, is_error, err_text, False, False
                except OSError as e:
                    if call.is_cancelled():
                        return "", 0, 0, None, False, "", True, False
                    is_error = True
                    err_text = f"Local LLM 响应读取失败: {e}"
                    return "", 0, 0, None, is_error, err_text, False, False
                try:
                    parsed = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError as e:
                    is_error = True
                    err_text = f"响应解析失败: {e}"
                    return "", 0, 0, None, is_error, err_text, False, False
                blocking = self._adapter.parse_blocking_response(parsed)
                state["text"] = blocking["text"]
                state["input_tokens"] = blocking["input_tokens"]
                state["output_tokens"] = blocking["output_tokens"]
                state["stop_reason"] = blocking.get("stop_reason")
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            if tag:
                with self._active_lock:
                    self._active_calls.pop(tag, None)

        text = state.get("text", "") or ""
        input_tok = int(state.get("input_tokens", 0) or 0)
        output_tok = int(state.get("output_tokens", 0) or 0)
        usage_estimated = False
        if stream and input_tok == 0 and output_tok == 0 and text:
            output_tok = max(1, len(text) // 4)
            usage_estimated = True
        return (
            text, input_tok, output_tok,
            state.get("stop_reason"),
            is_error, err_text, cancelled, usage_estimated,
        )

    # ── run() full override ──
    def run(self, prompt: str, session_id: Optional[str] = None,
            resume: bool = False, tag: Optional[str] = None,
            on_output: Optional[Callable[[str], None]] = None,
            on_tool_status=None, on_todo_update=None, on_agent_update=None,
            env_extra: Optional[dict] = None,
            fork_session: bool = False) -> dict:
        if not session_id:
            raise ValueError("LocalHTTPRunner.run requires session_id")

        # Session book-keeping
        if not resume:
            self._sessions.reset(session_id)
        history = self._sessions.get(session_id) if resume else []
        user_msg = {"role": "user", "content": prompt}
        messages = history + [user_msg]

        system = self._build_system_prompt()
        stream = bool(on_output)

        log.info(
            "Local LLM: resume=%s sid=%s stream=%s prompt=%d chars sys=%d chars",
            resume, session_id[:8] if session_id else "-",
            stream, len(prompt), len(system),
        )

        (text, input_tok, output_tok, stop_reason,
         is_error, err_text, cancelled, usage_estimated) = self._do_request(
            messages, tag, stream, system,
        )

        # OpenAI 400 fallback: retry without stream_options
        if (is_error and isinstance(self._adapter, OpenAIAdapter)
                and self._adapter.include_usage
                and err_text.startswith("HTTP 400")):
            log.warning(
                "OpenAI endpoint rejected stream_options (400); retrying without it"
            )
            self._adapter.include_usage = False
            (text, input_tok, output_tok, stop_reason,
             is_error, err_text, cancelled, usage_estimated) = self._do_request(
                messages, tag, stream, system,
            )

        # Stream out one final update so the UI sees full text
        if stream and on_output and text:
            try:
                on_output(text)
            except Exception:
                log.debug("on_output callback failed", exc_info=True)

        if cancelled:
            return {
                "result": "任务已取消。",
                "session_id": session_id,
                "is_error": False,
                "cancelled": True,
                "default_context_window": self._context_window,
            }

        if is_error:
            return {
                "result": f"{self.get_display_name()} 错误: {err_text}",
                "session_id": session_id,
                "is_error": True,
                "default_context_window": self._context_window,
            }

        if not text:
            return {
                "result": f"{self.get_display_name()} 本次未返回任何内容，请稍后重试。",
                "session_id": session_id,
                "is_error": True,
                "default_context_window": self._context_window,
            }

        # Success: persist conversation turn
        self._sessions.append(session_id, "user", prompt)
        self._sessions.append(session_id, "assistant", text)

        usage = {
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        peak = input_tok

        return {
            "result": text,
            "session_id": session_id,
            "is_error": False,
            "usage": usage,
            "last_call_usage": usage,
            "peak_context_tokens": peak,
            "compact_detected": False,
            "default_context_window": self._context_window,
            "usage_estimated": usage_estimated,
            "stop_reason": stop_reason,
        }
