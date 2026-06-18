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
        "grep": "Grep",
        "find": "Find",
        "subagent": "Subagent",
        "web_search": "WebSearch",
        "web_fetch": "WebFetch",
        "get_subagent_result": "GetSubagentResult",
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

        if etype == "tool_execution_start":
            state.tool_active_count += 1
            state.pending_silent_reset = True
            # pi's JSON output (≥v3) carries toolCallId + args in
            # tool_execution_start.  Forward them as a pending status
            # entry so the UI can render rich input-argument blocks
            # alongside the tool label (like pi-feishu does).
            tool_name = event.get("toolName")
            call_id = event.get("toolCallId")
            exec_args = event.get("args")
            if tool_name and call_id and isinstance(exec_args, dict):
                state.pending_tool_status.append({
                    "name": self._normalize_pi_tool(tool_name),
                    "hint_data": "",
                    "id": call_id,
                    "_exec_args": exec_args,
                })
            return

        if etype == "tool_execution_end":
            state.tool_active_count = max(0, state.tool_active_count - 1)
            state.pending_silent_reset = True
            # Forward execution result to UI for rich output display
            # (pi-feishu parity: args in the expandable card).
            tool_name = event.get("toolName")
            call_id = event.get("toolCallId")
            result = event.get("result")
            is_error = event.get("isError", False)
            if tool_name and call_id:
                state.pending_tool_status.append({
                    "name": self._normalize_pi_tool(tool_name),
                    "hint_data": "",
                    "id": call_id,
                    "_exec_result": result,
                    "_is_error": is_error,
                })
            return

        if etype == "turn_end":
            state.final_result = event
            # Turn boundary: clear any tool-active count left dangling by a missing
            # tool_execution_end (best-effort lifecycle), so the silent budget does
            # not stay extended into a text-only tail.
            state.tool_active_count = 0
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
            state.tool_active_count = 0  # clear lifecycle count on error path
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
        Start and end events are deduped independently (``_tool_seen_starts`` /
        ``_tool_seen_ends``) so a start never suppresses an end (Bug #4 fix).
        End events push the call-id into ``pending_tool_end_ids`` for the
        drain-loop ``on_tool_end`` callback (F-1). Never raises.
        """
        try:
            tc = self._tool_call_from_update(update)
            if not tc:
                return
            name = tc.get("name")
            if not name:
                return
            call_id = tc.get("id")
            # Dedup: start and end tracked separately.
            seen_set = state._tool_seen_starts if is_start else state._tool_seen_ends
            if call_id and call_id in seen_set:
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

            # Subagent always routes through pending_agent_launches OR
            # warning; never reaches pending_tool_status.  End events are
            # skipped — Subagent already emitted to pending_agent_launches
            # on start (F-1).
            if canonical == "Subagent":
                if not is_start:
                    return
                # Multi-task parallel dispatch: tasks=[{agent, task}, ...]
                tasks_list = args.get("tasks")
                if isinstance(tasks_list, list) and tasks_list:
                    emitted = False
                    for entry in tasks_list:
                        if not isinstance(entry, dict):
                            continue
                        agent_name = entry.get("agent", "")
                        task_text = entry.get("task", "")
                        if agent_name and task_text:
                            if state.pending_agent_launches is None:
                                state.pending_agent_launches = []
                            state.pending_agent_launches.append({
                                "description": task_text,
                                "name": None,
                                "subagent_type": agent_name,
                            })
                            emitted = True
                        elif agent_name:
                            if state.pending_agent_launches is None:
                                state.pending_agent_launches = []
                            state.pending_agent_launches.append({
                                "description": agent_name,
                                "name": None,
                                "subagent_type": agent_name,
                            })
                            emitted = True
                    if emitted:
                        if call_id:
                            state._tool_seen_starts.add(call_id)
                        return
                    # fall through to single-shot try then warning

                # Single agent+task path
                agent_name = args.get("agent", "")
                task_text = args.get("task", "")
                if agent_name and task_text:
                    if state.pending_agent_launches is None:
                        state.pending_agent_launches = []
                    state.pending_agent_launches.append({
                        "description": task_text,
                        "name": None,
                        "subagent_type": agent_name,
                    })
                    if call_id:
                        state._tool_seen_starts.add(call_id)
                    return

                # All extraction failed (including empty args).
                # If args are empty AND this is the start event with a
                # call_id, defer — the end event may carry usable args.
                if not args and call_id and is_start:
                    return
                log.warning("Subagent toolcall with unrecognized args shape: %s", args)
                if call_id:
                    state._tool_seen_starts.add(call_id)
                return

            # Non-Subagent: original general path
            start_already_emitted = bool(
                call_id and call_id in state._tool_seen_starts)
            if not args:
                # No usable arguments. With an id on a *start* event, defer —
                # a later toolcall_end may carry them. Otherwise:
                # - start without call_id → bare label (never-raises contract)
                # - end with call_id → only pending_tool_end_ids (no bare
                #   status entry; unmatched warning at drain time)
                if call_id and is_start:
                    return
                if not is_start and call_id:
                    # End without args: only end-id (unmatched warning path).
                    state.pending_tool_end_ids.append(call_id)
                    state._tool_seen_ends.add(call_id)
                    return
                # Start without call_id or bare start (id but no args,
                # not deferred above because !is_start path already returned).
                entry = {"name": canonical, "hint_data": ""}
                if call_id:
                    entry["id"] = call_id
                state.pending_tool_status.append(entry)
                if call_id:
                    state._tool_seen_starts.add(call_id)
                return

            hint = _extract_hint_data(canonical, args)
            entry = {"name": canonical, "hint_data": hint}
            if call_id:
                entry["id"] = call_id

            if is_start:
                # Normal start with args → one status entry.
                state.pending_tool_status.append(entry)
                if call_id:
                    state._tool_seen_starts.add(call_id)
            else:
                # End with args.
                if call_id:
                    state.pending_tool_end_ids.append(call_id)
                    state._tool_seen_ends.add(call_id)
                    if not start_already_emitted:
                        # Deferred start: end carries the args → emit status
                        # as compensation (UI gets entry + immediate done).
                        state.pending_tool_status.append(entry)
                        state._tool_seen_starts.add(call_id)
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
            # Prefer the toolCall at the update's own contentIndex.
            # Without this, multi-tool turns return the *first* toolCall for
            # every start event, causing subsequent starts to be deduped (id
            # already in _tool_seen_starts) and their entries silently lost.
            content_index = update.get("contentIndex")
            if isinstance(content_index, int) and 0 <= content_index < len(content):
                item = content[content_index]
                if (isinstance(item, dict)
                        and item.get("type") == "toolCall"
                        and item.get("name")):
                    return item
            # Legacy fallback for events that lack contentIndex.
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
