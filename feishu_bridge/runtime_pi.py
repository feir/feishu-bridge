"""Pi coding-agent runner integration."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from feishu_bridge.runtime import BaseRunner, StreamState


class PiRunner(BaseRunner):
    """badlogic/pi-mono coding-agent runner.

    Pi emits JSONL events in ``--mode json``. The bridge keeps its own
    session id and maps it to a deterministic Pi session file under the
    configured workspace.
    """

    DEFAULT_MODEL = "Qwen3.6-35B-A3B-mxfp4"
    DEFAULT_CONTEXT_WINDOW = 32_768
    ALWAYS_STREAMING = True
    READONLY_TOOLS = "read,grep,find,ls"

    def build_args(self, prompt: str, session_id: Optional[str],
                   resume: bool, streaming: bool, *,
                   fork_session: bool = False) -> list:
        args = [self.command, "--mode", "json"]

        if self._extra_cli_args:
            args.extend(self._extra_cli_args)

        if self.model and not self._has_arg(args, "--model"):
            args.extend(["--model", self.model])

        if not self._has_any_arg(args, {"--tools", "--no-tools"}):
            args.extend(["--tools", self.READONLY_TOOLS])

        system_prompt = self._build_system_prompt()
        if system_prompt:
            args.extend(["--append-system-prompt", system_prompt])

        has_session_override = self._has_any_arg(args, {"--session", "--no-session"})
        if session_id and not has_session_override:
            args.extend(["--session", str(self._session_path(session_id))])
        elif not session_id and not has_session_override:
            args.append("--no-session")

        args.extend(["-p", prompt])
        return args

    def parse_streaming_line(self, event: dict, state: StreamState) -> None:
        etype = event.get("type")

        if etype == "message_update":
            self._handle_message_update(event, state)
            return

        if etype == "tool_execution_start":
            tool_name = event.get("toolName")
            if tool_name:
                state.pending_tool_status.append(str(tool_name))
            return

        if etype == "tool_execution_end":
            tool_name = event.get("toolName")
            if tool_name:
                state.pending_tool_status.append(str(tool_name))
            return

        if etype == "turn_end":
            state.final_result = event
            message = event.get("message") or {}
            self._update_usage(message.get("usage"), state)
            stop_reason = message.get("stopReason")
            if stop_reason and stop_reason != "toolUse":
                text = self._message_text(message)
                if text:
                    should_emit = text != state.accumulated_text
                    state.accumulated_text = text
                    if should_emit:
                        state.pending_output.append(state.accumulated_text)
                state.is_error = stop_reason in {"error", "aborted"}
                state.done = True
            return

        if etype == "error":
            raw = (
                event.get("message")
                or event.get("errorMessage")
                or event.get("error")
                or "Unknown Pi protocol error"
            )
            if isinstance(raw, dict):
                raw = raw.get("message") or raw.get("error") or str(raw)
            state.accumulated_text = self._format_error(str(raw))
            state.pending_output.append(state.accumulated_text)
            state.is_error = True
            state.done = True
            return

        if etype == "message_end":
            message = event.get("message") or {}
            self._update_usage(message.get("usage"), state)
            return

    def _build_streaming_result(self, state: StreamState,
                                session_id: Optional[str]) -> Optional[dict]:
        if not state.done:
            return None

        message = (state.final_result or {}).get("message") or {}
        stop_reason = message.get("stopReason")
        error_message = message.get("errorMessage")
        text = state.accumulated_text or self._message_text(message)

        is_error = state.is_error or stop_reason in {"error", "aborted"}
        if is_error and not text:
            text = error_message or "Pi request failed."
        if is_error:
            text = self._format_error(text)
        if not text:
            return None

        usage = state.last_call_usage or {}
        model_name = self.model or self.DEFAULT_MODEL

        return {
            "result": text,
            "session_id": state.session_id or session_id,
            "is_error": is_error,
            "usage": usage,
            "last_call_usage": usage,
            "modelUsage": {
                model_name: {
                    "contextWindow": self.get_default_context_window(),
                    "inputTokens": usage.get("input_tokens", 0),
                    "outputTokens": usage.get("output_tokens", 0),
                    "cacheReadInputTokens": usage.get("cache_read_input_tokens", 0),
                    "cacheCreationInputTokens": usage.get(
                        "cache_creation_input_tokens", 0
                    ),
                },
            },
            "peak_context_tokens": state.peak_context_tokens,
            "compact_detected": False,
        }

    def parse_blocking_output(self, stdout: str, session_id: Optional[str]) -> dict:
        raise NotImplementedError("PiRunner always uses streaming mode")

    def get_model_aliases(self) -> dict[str, str]:
        return self._merge_model_aliases({
            "pi": self.DEFAULT_MODEL,
            "qwen": self.DEFAULT_MODEL,
            "gemma": "gemma-4-26b-a4b-it-mxfp4",
        })

    def get_default_context_window(self) -> int:
        return self.DEFAULT_CONTEXT_WINDOW

    def get_display_name(self) -> str:
        return "Pi"

    def supports_compact(self) -> bool:
        return False

    def wants_auth_file(self) -> bool:
        return False

    @staticmethod
    def _has_arg(args: list, flag: str) -> bool:
        return any(arg == flag or str(arg).startswith(flag + "=") for arg in args)

    @classmethod
    def _has_any_arg(cls, args: list, flags: set[str]) -> bool:
        return any(cls._has_arg(args, flag) for flag in flags)

    def _session_path(self, session_id: str) -> Path:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
        session_dir = (
            Path(self.workspace)
            / "state"
            / "feishu-bridge"
            / "pi-sessions"
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir / f"{safe_id}.jsonl"

    def _handle_message_update(self, event: dict, state: StreamState) -> None:
        update = event.get("assistantMessageEvent") or {}
        utype = update.get("type")

        if utype == "text_delta":
            delta = update.get("delta") or ""
            if delta:
                state.accumulated_text += str(delta)
                state.pending_output.append(state.accumulated_text)
            return

        if utype == "text_end":
            content = update.get("content") or ""
            if content and not state.accumulated_text:
                state.accumulated_text = str(content)
                state.pending_output.append(state.accumulated_text)
            partial = update.get("partial") or {}
            self._update_usage(partial.get("usage"), state)
            return

        if utype in {"toolcall_start", "toolcall_end"}:
            tool_name = self._tool_name_from_update(update)
            if tool_name:
                state.pending_tool_status.append(tool_name)

    @staticmethod
    def _tool_name_from_update(update: dict) -> Optional[str]:
        tool_call = update.get("toolCall") or {}
        if isinstance(tool_call, dict) and tool_call.get("name"):
            return str(tool_call["name"])
        partial = update.get("partial") or {}
        content = partial.get("content") or []
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "toolCall" and item.get("name"):
                    return str(item["name"])
        return None

    @classmethod
    def _message_text(cls, message: dict) -> str:
        content = message.get("content") or []
        if not isinstance(content, list):
            return ""
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if text:
                    parts.append(str(text))
        return "".join(parts)

    @classmethod
    def _format_error(cls, message: str) -> str:
        raw = (message or "").strip() or "Unknown Pi error"
        redacted = re.sub(
            r"(?i)(api[_-]?key|authorization|bearer)\s*[:=]\s*\S+",
            r"\1=<redacted>",
            raw,
        )
        lower = redacted.lower()
        if redacted.startswith((
            "Pi provider ",
            "Pi 模型",
            "Pi 工具",
            "Pi 协议",
            "Pi 请求",
        )):
            return redacted
        if "invalid api key" in lower or "401" in lower or "unauthorized" in lower:
            return f"Pi provider 鉴权失败：{redacted}"
        if "model not found" in lower or "404" in lower:
            return f"Pi 模型不可用或不存在：{redacted}"
        if (
            "econnrefused" in lower
            or "connection refused" in lower
            or "failed to fetch" in lower
            or "network error" in lower
        ):
            return f"Pi provider 不可用：{redacted}"
        if (
            "tool denied" in lower
            or "tool not allowed" in lower
            or "not permitted" in lower
            or "permission denied" in lower
        ):
            return f"Pi 工具调用被拒绝：{redacted}"
        if "protocol" in lower or "invalid json" in lower or "jsonrpc" in lower:
            return f"Pi 协议错误：{redacted}"
        return f"Pi 请求失败：{redacted}"

    def _update_usage(self, usage: Optional[dict], state: StreamState) -> None:
        normalized = self._normalize_usage(usage)
        if not normalized:
            return
        state.last_call_usage = normalized
        ctx_tokens = (
            normalized.get("input_tokens", 0)
            + normalized.get("cache_read_input_tokens", 0)
            + normalized.get("cache_creation_input_tokens", 0)
        )
        if ctx_tokens > state.peak_context_tokens:
            state.peak_context_tokens = ctx_tokens

    @staticmethod
    def _normalize_usage(usage: Optional[dict]) -> dict:
        if not isinstance(usage, dict):
            return {}
        return {
            "input_tokens": int(usage.get("input", 0) or 0),
            "cache_read_input_tokens": int(usage.get("cacheRead", 0) or 0),
            "cache_creation_input_tokens": int(usage.get("cacheWrite", 0) or 0),
            "output_tokens": int(usage.get("output", 0) or 0),
        }
