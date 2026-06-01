"""Pi coding-agent runner integration."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from feishu_bridge.runtime import BaseRunner, StreamState, _extract_hint_data, log


class PiRunner(BaseRunner):
    """badlogic/pi-mono coding-agent runner.

    Pi emits JSONL events in ``--mode json``. The bridge keeps its own
    session id and maps it to a deterministic Pi session file under the
    configured workspace.
    """

    ALWAYS_STREAMING = True

    # Pi emits lowercase native tool names (read/bash/edit/write/ls/grep/find).
    # Normalize to the bridge's canonical PascalCase vocabulary so ui.py's
    # _TOOL_STATUS_MAP / _format_tool_hint (keyed on canonical names) and the
    # shared _extract_hint_data both apply. Mirrors omp's _normalize_tool_name.
    _TOOL_NAME_MAP = {
        "bash": "Bash",
        "read": "Read",
        "write": "Write",
        "edit": "Edit",
        "ls": "Ls",
        "list": "Ls",
        "grep": "Grep",
        "search": "Grep",
        "find": "Find",
        "glob": "Glob",
        "subagent": "Subagent",
    }

    @classmethod
    def _normalize_pi_tool(cls, raw_name: str) -> str:
        return cls._TOOL_NAME_MAP.get((raw_name or "").lower(), (raw_name or "").title())

    def display_default_model(self) -> Optional[str]:
        """Pi pins no ``--model`` under the default provider; it reads its own
        ``defaultModel`` from ``~/.pi/agent/settings.json``. Surface that for the
        card footer / status display (mirrors OmpRunner.display_default_model).
        Returns ``None`` when unreadable. Display only — never feeds build_args."""
        if self.model:
            return self.model
        return self._read_pi_default_model()

    @staticmethod
    def _read_pi_default_model() -> Optional[str]:
        """Read defaultModel from ~/.pi/agent/settings.json."""
        config_path = Path.home() / ".pi" / "agent" / "settings.json"
        try:
            with open(config_path) as f:
                config = json.load(f)
        except (OSError, ValueError) as e:
            log.debug("Cannot read pi settings for default model: %s", e)
            return None
        if isinstance(config, dict):
            model = config.get("defaultModel")
            if isinstance(model, str) and model.strip():
                return model.strip()
        return None

    def build_args(self, prompt: str, session_id: Optional[str],
                   resume: bool, streaming: bool, *,
                   fork_session: bool = False,
                   fresh_context: Optional[str] = None) -> list:
        args = [self.command, "--mode", "json"]

        if self._extra_cli_args:
            args.extend(self._extra_cli_args)

        if self.model and not self._has_arg(args, "--model"):
            args.extend(["--model", self.model])

        # No tool-policy injection: the bridge is a pure conduit for pi. Pi
        # uses its native toolset (read/bash/edit/write) governed by pi's own
        # config (~/.pi/agent/settings.json, AGENTS.md). Operators can still
        # scope tools per provider via config args_by_type (--tools/--no-tools/
        # --exclude-tools), which arrive through _extra_cli_args above. Unlike
        # Claude, pi has no tool_deferred approval flow, so access is gated by
        # the bot's allowed_users, not a per-call approval card.
        system_prompt = self._build_system_prompt(extra=fresh_context)
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

        if etype in ("tool_execution_start", "tool_execution_end"):
            # No-op for tool status. These coarse lifecycle events carry only
            # `toolName` (no tool-call id, no arguments). The authoritative,
            # rich source is `message_update.toolcall_*` (carries id + name +
            # arguments), handled in _handle_message_update. Emitting here too
            # would duplicate each call and provide no file/command target.
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
        model_name = self.display_default_model() or "(cli-default)"

        return {
            "result": text,
            "session_id": state.session_id or session_id,
            "is_error": is_error,
            "usage": usage,
            "last_call_usage": usage,
            "modelUsage": {
                model_name: {
                    "contextWindow": 0,
                    "inputTokens": (usage.get("input_tokens", 0) or 0),
                    "outputTokens": (usage.get("output_tokens", 0) or 0),
                    "cacheReadInputTokens": (usage.get("cache_read_input_tokens", 0) or 0),
                    "cacheCreationInputTokens": (usage.get(
                        "cache_creation_input_tokens", 0
                    ) or 0),
                },
            },
            "peak_context_tokens": state.peak_context_tokens,
            "compact_detected": False,
        }

    def parse_blocking_output(self, stdout: str, session_id: Optional[str]) -> dict:
        raise NotImplementedError("PiRunner always uses streaming mode")

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
            self._emit_tool_status(update, state,
                                   is_start=(utype == "toolcall_start"))

    def _emit_tool_status(self, update: dict, state: StreamState,
                          is_start: bool) -> None:
        """Surface a pi tool call as one ``{name, hint_data}`` entry.

        Authoritative source for pi tool status (``message_update.toolcall_*``).
        Each call is emitted exactly once — when its arguments first become
        available — keyed by tool-call id (``state._tool_seen_ids``). Never
        raises: extraction failures degrade to skipping this status update.
        """
        try:
            tc = self._tool_call_from_update(update)
            if not tc:
                return
            name = tc.get("name")
            if not name:
                return
            call_id = tc.get("id")
            if call_id and call_id in state._tool_seen_ids:
                return
            if not call_id and not is_start:
                # id-less call: act on start only to avoid start+end double.
                # Known limitation: an id-less split call whose args arrive only
                # on `end` surfaces a bare label (no hint). Real pi always emits
                # a tool-call id (verified), so this affects only malformed
                # streams — an acceptable degradation, never a crash.
                return
            canonical = self._normalize_pi_tool(name)
            args = tc.get("arguments")
            if not isinstance(args, dict):
                args = {}
            if not args:
                # No usable arguments. With an id on a *start* event, defer —
                # a later toolcall_end may carry them. Otherwise the call is
                # resolving without extractable args: degrade to a bare-name
                # entry (still useful: "执行命令"/"读取文件") rather than
                # dropping the status (spec contract: never-raises → bare label).
                if call_id and is_start:
                    return
                state.pending_tool_status.append(
                    {"name": canonical, "hint_data": ""})
                if call_id:
                    state._tool_seen_ids.add(call_id)
                return
            # Subagent: extract agent/task → pending_agent_launches
            if canonical == "Subagent":
                agent_name = args.get("agent", "")
                task_text = args.get("task", "")
                if agent_name and task_text:
                    launch = {
                        "description": task_text,
                        "name": None,
                        "subagent_type": agent_name,
                    }
                    if state.pending_agent_launches is None:
                        state.pending_agent_launches = []
                    state.pending_agent_launches.append(launch)
                    if call_id:
                        state._tool_seen_ids.add(call_id)
                    return
                # Extraction failed: degrade to tool_status path
            hint = _extract_hint_data(canonical, args)
            state.pending_tool_status.append(
                {"name": canonical, "hint_data": hint})
            if call_id:
                state._tool_seen_ids.add(call_id)
        except Exception as e:  # never-raises hot path
            log.debug("pi tool-status extract failed: %s", e)

    @staticmethod
    def _tool_call_from_update(update: dict) -> Optional[dict]:
        """Return the toolCall object (id/name/arguments) from an update."""
        tool_call = update.get("toolCall")
        if isinstance(tool_call, dict) and tool_call.get("name"):
            return tool_call
        partial = update.get("partial") or {}
        content = partial.get("content") or []
        if isinstance(content, list):
            for item in content:
                if (isinstance(item, dict)
                        and item.get("type") == "toolCall"
                        and item.get("name")):
                    return item
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
