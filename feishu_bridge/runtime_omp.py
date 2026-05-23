"""OMP RPC runner — persistent-process JSON-RPC 2.0 integration."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from feishu_bridge.runtime import BaseRunner, StreamState, _extract_hint_data, BG_AGENT_SILENT_TIMEOUT

log = logging.getLogger(__name__)

READY_TIMEOUT = 30
IDLE_REAP_INTERVAL = 300  # 5 min scan
IDLE_REAP_THRESHOLD = 1800  # 30 min
ABORT_DRAIN_TIMEOUT = 15
SILENT_TIMEOUT = 480


@dataclass
class _RpcProcess:
    proc: subprocess.Popen
    stdin_lock: threading.Lock
    stdout_queue: queue.Queue
    reader_thread: threading.Thread
    last_activity: float
    session_id: str
    env_snapshot: dict = field(default_factory=dict)


class OmpRpcRunner(BaseRunner):
    """OMP coding-agent runner using persistent RPC mode.

    Each session_id maps to one long-lived omp process communicating via
    JSON-RPC 2.0 over stdin/stdout. Processes are reused across turns and
    automatically respawned from session files after crash or bridge restart.
    """

    ALWAYS_STREAMING = True

    _TOOL_NAME_MAP = {
        "bash": "Bash",
        "read": "Read",
        "write": "Write",
        "edit": "Edit",
        "grep": "Grep",
        "search": "Grep",
        "eval": "Eval",
        "find": "Find",
        "glob": "Glob",
        "lsp": "Lsp",
        "python": "Python",
        "notebook_edit": "NotebookEdit",
        "inspect_image": "InspectImage",
        "browser": "Browser",
        "task": "Task",
        "todo_write": "TodoWrite",
        "web_search": "WebSearch",
        "web_fetch": "WebFetch",
        "ask": "Ask",
        "agent": "Agent",
    }

    @classmethod
    def _normalize_tool_name(cls, raw_name: str) -> str:
        return cls._TOOL_NAME_MAP.get(raw_name.lower(), raw_name.title())

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._processes: dict[str, _RpcProcess] = {}
        self._proc_lock = threading.Lock()
        self._reaper_started = False
        self._shutting_down = False

    # ── Abstract method stubs (not used — run() is fully overridden) ──

    def build_args(self, prompt, session_id, resume, streaming, *,
                   fork_session=False):
        raise NotImplementedError("OmpRpcRunner uses RPC mode, not CLI args")

    def parse_streaming_line(self, event, state):
        raise NotImplementedError("OmpRpcRunner uses RPC mode")

    def parse_blocking_output(self, stdout, session_id):
        raise NotImplementedError("OmpRpcRunner uses RPC mode")

    # ── Optional overrides ──

    def get_display_name(self) -> str:
        return "OMP"

    def supports_compact(self) -> bool:
        return True

    def supports_auto_compact(self) -> bool:
        return True

    def wants_auth_file(self) -> bool:
        return False

    # ── Main entry point ──

    def run(self, prompt: str, session_id: Optional[str] = None,
            resume: bool = False, tag: Optional[str] = None,
            on_output=None, on_tool_status=None, on_todo_update=None,
            on_agent_update=None, env_extra: Optional[dict] = None,
            fork_session: bool = False) -> dict:

        self._ensure_reaper()

        stripped = (prompt or "").strip()
        if stripped.lower().startswith("/compact"):
            return self._do_compact(session_id, stripped, tag)

        try:
            rpc = self._get_or_spawn(session_id, resume, env_extra)
        except _SpawnError as e:
            return {
                "result": f"OMP 启动失败：{e}",
                "session_id": session_id,
                "is_error": True,
            }

        if tag:
            with self._lock:
                self._active[tag] = rpc.proc

        try:
            self._send_command(rpc, {"type": "prompt", "message": prompt})
            result = self._stream_events(
                rpc, session_id, tag,
                on_output=on_output,
                on_tool_status=on_tool_status,
                on_todo_update=on_todo_update,
                on_agent_update=on_agent_update,
            )
        except _ProcessDead as e:
            self._evict(session_id)
            result = {
                "result": f"OMP 进程意外退出：{e}",
                "session_id": session_id,
                "is_error": True,
            }
        finally:
            was_cancelled = self._cleanup_tag(tag)

        if tag and was_cancelled:
            self._abort_and_drain(rpc)
            return {
                "result": "任务已取消。",
                "session_id": session_id,
                "is_error": False,
                "cancelled": True,
            }

        return result

    # ── Cancel ──

    def cancel(self, tag: str) -> bool:
        with self._lock:
            proc = self._active.get(tag)
            if proc:
                self._cancelled.add(tag)
        if not proc:
            return False

        rpc = self._find_rpc_by_proc(proc)
        if rpc:
            log.info("Cancelling OMP: tag=%s pid=%d (sending abort)", tag, proc.pid)
            try:
                self._send_command(rpc, {"type": "abort"})
            except _ProcessDead:
                pass
        return True

    # ── Compact ──

    def _do_compact(self, session_id: Optional[str], prompt: str,
                    tag: Optional[str]) -> dict:
        try:
            rpc = self._get_or_spawn(session_id, resume=True)
        except _SpawnError as e:
            return {
                "result": f"OMP compact 失败：{e}",
                "session_id": session_id,
                "is_error": True,
            }

        custom = prompt[len("/compact"):].strip()
        cmd: dict = {"type": "compact"}
        if custom:
            cmd["customInstructions"] = custom

        if tag:
            with self._lock:
                self._active[tag] = rpc.proc

        compact_error = None
        try:
            self._send_command(rpc, cmd)
            self._wait_for_response(rpc, "compact", timeout=120)
        except (_ProcessDead, TimeoutError) as e:
            compact_error = e
        finally:
            was_cancelled = self._cleanup_tag(tag)

        if tag and was_cancelled:
            self._abort_and_drain(rpc)
            return {
                "result": "任务已取消。",
                "session_id": session_id,
                "is_error": False,
                "cancelled": True,
            }

        if compact_error is not None:
            return {
                "result": f"OMP compact 失败：{compact_error}",
                "session_id": session_id,
                "is_error": True,
            }

        return {
            "result": "上下文已压缩。",
            "session_id": session_id,
            "is_error": False,
            "compact_detected": True,
        }

    # ── Process pool ──

    def _get_or_spawn(self, session_id: Optional[str], resume: bool = True,
                      env_extra: Optional[dict] = None) -> _RpcProcess:
        if not session_id:
            raise _SpawnError("session_id is required for OMP RPC mode")

        with self._proc_lock:
            rpc = self._processes.get(session_id)
            if rpc and rpc.proc.poll() is None:
                if not resume:
                    self._send_command(rpc, {"type": "new_session"})
                    self._wait_for_response(rpc, "new_session", timeout=READY_TIMEOUT)
                return rpc
            if rpc:
                log.warning("OMP process dead for sid=%s, respawning", session_id[:8])
                self._processes.pop(session_id, None)

        rpc = self._spawn(session_id, env_extra)

        with self._proc_lock:
            old = self._processes.get(session_id)
            if old and old.proc.poll() is None:
                self._terminate_rpc(rpc)
                return old
            self._processes[session_id] = rpc

        return rpc

    def _spawn(self, session_id: str, env_extra: Optional[dict] = None) -> _RpcProcess:
        args = [self.command, "--mode", "rpc"]

        if self._extra_cli_args:
            args.extend(self._extra_cli_args)

        if self.model and not any(a == "--model" for a in args):
            args.extend(["--model", self.model])

        session_dir = self._session_dir(session_id)
        args.extend(["--session-dir", str(session_dir)])

        system_prompt = self._build_system_prompt()
        if system_prompt:
            args.extend(["--append-system-prompt", system_prompt])

        env = os.environ.copy()
        extra_env = self.get_extra_env()
        if env_extra:
            extra_env.update(env_extra)
        if extra_env:
            env.update(extra_env)

        log.info("Spawning OMP RPC: sid=%s cmd=%s", session_id[:8], " ".join(args[:6]))

        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.workspace,
            env=env,
            start_new_session=True,
        )

        stdout_q: queue.Queue = queue.Queue(maxsize=4096)
        reader = threading.Thread(
            target=self._stdout_reader,
            args=(proc, stdout_q),
            daemon=True,
        )
        reader.start()

        stderr_thread = threading.Thread(
            target=self._stderr_drain,
            args=(proc,),
            daemon=True,
        )
        stderr_thread.start()

        rpc = _RpcProcess(
            proc=proc,
            stdin_lock=threading.Lock(),
            stdout_queue=stdout_q,
            reader_thread=reader,
            last_activity=time.monotonic(),
            session_id=session_id,
            env_snapshot=dict(env_extra or {}),
        )

        try:
            self._wait_for_ready(rpc)
        except TimeoutError:
            self._terminate_rpc(rpc)
            raise _SpawnError(f"OMP 进程未在 {READY_TIMEOUT}s 内就绪")

        return rpc

    def _wait_for_ready(self, rpc: _RpcProcess):
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            try:
                line = rpc.stdout_queue.get(timeout=1.0)
            except queue.Empty:
                if rpc.proc.poll() is not None:
                    raise _SpawnError(f"OMP 进程启动后立即退出 (code {rpc.proc.returncode})")
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "ready":
                log.info("OMP ready: sid=%s pid=%d", rpc.session_id[:8], rpc.proc.pid)
                return
        raise TimeoutError("ready timeout")

    def _wait_for_response(self, rpc: _RpcProcess, command: str,
                           timeout: float = 30) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = rpc.stdout_queue.get(timeout=1.0)
            except queue.Empty:
                if rpc.proc.poll() is not None:
                    raise _ProcessDead("process exited during response wait")
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "response" and msg.get("command") == command:
                return msg
        raise TimeoutError(f"response timeout for command={command}")

    # ── Event streaming ──

    def _stream_events(self, rpc: _RpcProcess, session_id: Optional[str],
                       tag: Optional[str], *,
                       on_output=None, on_tool_status=None,
                       on_todo_update=None, on_agent_update=None) -> dict:
        state = StreamState(session_id=session_id)
        prompt_acked = False
        idle_deadline = time.monotonic() + self.timeout
        silent_deadline = time.monotonic() + SILENT_TIMEOUT

        while True:
            now = time.monotonic()

            if tag:
                with self._lock:
                    if tag in self._cancelled:
                        break

            if now > idle_deadline:
                log.warning("OMP idle timeout: sid=%s", (session_id or "-")[:8])
                self._abort_and_drain(rpc)
                return {
                    "result": "OMP 空闲超时（连续无输出超过 %ds）" % self.timeout,
                    "session_id": session_id,
                    "is_error": True,
                }

            if now > silent_deadline:
                log.warning("OMP silent timeout: sid=%s", (session_id or "-")[:8])
                self._abort_and_drain(rpc)
                warning = "\n\n⚠️ 长时间无文本输出（>%ds），自动中断恢复中…" % SILENT_TIMEOUT
                return {
                    "result": (state.accumulated_text + warning) if state.accumulated_text else warning.strip(),
                    "session_id": session_id,
                    "is_error": False,
                    "silent_timeout": True,
                    "peak_context_tokens": state.peak_context_tokens,
                    "compact_detected": state.compact_detected,
                }

            try:
                line = rpc.stdout_queue.get(timeout=1.0)
            except queue.Empty:
                if rpc.proc.poll() is not None:
                    raise _ProcessDead("process exited during streaming")
                continue

            idle_deadline = time.monotonic() + self.timeout
            rpc.last_activity = time.monotonic()

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "response" and msg.get("command") == "prompt":
                prompt_acked = True
                continue

            if not prompt_acked:
                continue

            if msg_type == "error":
                error_msg = msg.get("message") or msg.get("error") or "Unknown RPC error"
                state.accumulated_text = self._format_error(str(error_msg))
                state.is_error = True
                if on_output:
                    on_output(state.accumulated_text)
                break

            if msg_type in ("response", "extension_ui_request"):
                continue

            done, reset_silent = self._handle_event(msg, state, on_output, on_tool_status)
            if on_output and state.pending_output:
                for text in state.pending_output:
                    on_output(text)
                state.pending_output.clear()
                silent_deadline = time.monotonic() + SILENT_TIMEOUT
            if on_tool_status and state.pending_tool_status:
                on_tool_status(list(state.pending_tool_status))
                state.pending_tool_status.clear()
                silent_deadline = time.monotonic() + SILENT_TIMEOUT
            if reset_silent:
                silent_deadline = time.monotonic() + SILENT_TIMEOUT
            if state.bg_agent_running:
                silent_deadline = max(silent_deadline, time.monotonic() + BG_AGENT_SILENT_TIMEOUT)
            if on_todo_update and state.pending_todo_update is not None:
                on_todo_update(state.pending_todo_update)
                state.pending_todo_update = None
            if on_agent_update and state.pending_agent_launches is not None:
                on_agent_update(state.pending_agent_launches)
                state.pending_agent_launches = None
            if done:
                break

        usage = state.last_call_usage or {}
        model_name = self.model or "(cli-default)"

        return {
            "result": state.accumulated_text or "OMP 未返回任何内容。",
            "session_id": session_id,
            "is_error": state.is_error,
            "usage": usage,
            "last_call_usage": usage,
            "modelUsage": {
                model_name: {
                    "contextWindow": 0,
                    "inputTokens": usage.get("input_tokens", 0),
                    "outputTokens": usage.get("output_tokens", 0),
                    "cacheReadInputTokens": usage.get("cache_read_input_tokens", 0),
                    "cacheCreationInputTokens": usage.get("cache_creation_input_tokens", 0),
                },
            },
            "peak_context_tokens": state.peak_context_tokens,
            "compact_detected": state.compact_detected,
        }

    def _handle_event(self, event: dict, state: StreamState,
                      on_output, on_tool_status):
        """Handle a single RPC event. Returns (done, reset_silent)."""
        etype = event.get("type")

        if etype == "message_update":
            reset_silent = self._handle_message_update(event, state)
            return False, reset_silent

        if etype == "tool_execution_start":
            return False, False

        if etype == "tool_execution_end":
            return False, False

        if etype == "turn_end":
            message = event.get("message") or {}
            self._update_usage(message.get("usage"), state)
            stop_reason = message.get("stopReason")
            if stop_reason == "error":
                error_msg = message.get("errorMessage") or "Unknown error"
                state.accumulated_text = self._format_error(str(error_msg))
                state.is_error = True
                state.pending_output.append(state.accumulated_text)
            elif stop_reason and stop_reason != "toolUse":
                text = self._message_text(message)
                if text and text != state.accumulated_text:
                    state.accumulated_text = text
                    state.pending_output.append(state.accumulated_text)
            return False, False

        if etype == "auto_compaction_start":
            state.compact_detected = True
            return False, True

        if etype == "auto_compaction_end":
            return False, True

        if etype == "agent_end":
            return True, False

        if etype == "error":
            raw = (
                event.get("message")
                or event.get("errorMessage")
                or event.get("error")
                or "Unknown OMP error"
            )
            if isinstance(raw, dict):
                raw = raw.get("message") or raw.get("error") or str(raw)
            state.accumulated_text = self._format_error(str(raw))
            state.is_error = True
            state.pending_output.append(state.accumulated_text)
            return True, False

        return False, False

    def _handle_message_update(self, event: dict, state: StreamState) -> bool:
        update = event.get("assistantMessageEvent") or {}
        utype = update.get("type")

        if utype == "text_delta":
            delta = update.get("delta") or ""
            if delta:
                state.accumulated_text += str(delta)
                state.pending_output.append(state.accumulated_text)
            return False

        if utype == "text_end":
            content = update.get("content") or ""
            if content and not state.accumulated_text:
                state.accumulated_text = str(content)
                state.pending_output.append(state.accumulated_text)
            partial = update.get("partial") or {}
            self._update_usage(partial.get("usage"), state)
            return False

        if utype == "thinking_delta":
            return True

        if utype in {"toolcall_start", "toolcall_end"}:
            tool_call = self._resolve_tool_call(update)
            if not tool_call:
                return False
            raw_name = tool_call.get("name", "")
            if not raw_name:
                return False
            name = self._normalize_tool_name(raw_name)
            arguments = tool_call.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            # Only append to tool history on start, not end (avoids double-counting)
            if utype == "toolcall_start":
                state.pending_tool_status.append({
                    "name": name,
                    "hint_data": _extract_hint_data(name, arguments),
                })
            if name == "TodoWrite":
                todos = arguments.get("todos")
                if isinstance(todos, list):
                    state.pending_todo_update = todos
            elif name == "Agent":
                if arguments.get("run_in_background"):
                    state.bg_agent_running = True
                else:
                    launch = {
                        "description": arguments.get("description", ""),
                        "name": arguments.get("name"),
                        "subagent_type": arguments.get("subagent_type", ""),
                    }
                    if state.pending_agent_launches is None:
                        state.pending_agent_launches = []
                    state.pending_agent_launches.append(launch)
            elif name == "Task":
                launch = {
                    "description": arguments.get("description", ""),
                    "name": arguments.get("name"),
                    "subagent_type": arguments.get("subagent_type", ""),
                }
                if state.pending_agent_launches is None:
                    state.pending_agent_launches = []
                state.pending_agent_launches.append(launch)
            return False



    @staticmethod
    def _resolve_tool_call(update: dict) -> dict | None:
        """Resolve tool call from direct toolCall field or partial fallback."""
        tc = update.get("toolCall")
        if isinstance(tc, dict) and tc.get("name"):
            return tc
        # Fallback: scan partial.content for type=="toolCall" blocks
        partial = update.get("partial") or {}
        content = partial.get("content") or []
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "toolCall" and item.get("name"):
                    return item
        return None
    @staticmethod
    def _message_text(message: dict) -> str:
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

    # ── Abort + drain ──

    def _abort_and_drain(self, rpc: _RpcProcess):
        try:
            self._send_command(rpc, {"type": "abort"})
        except _ProcessDead:
            return

        deadline = time.monotonic() + ABORT_DRAIN_TIMEOUT
        while time.monotonic() < deadline:
            try:
                line = rpc.stdout_queue.get(timeout=1.0)
            except queue.Empty:
                if rpc.proc.poll() is not None:
                    return
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = msg.get("event") or msg
            if event.get("type") == "agent_end":
                return

        log.warning("OMP abort drain timeout, force-killing pid=%d", rpc.proc.pid)
        self._terminate_rpc(rpc)
        self._evict(rpc.session_id)

    # ── I/O helpers ──

    def _send_command(self, rpc: _RpcProcess, cmd: dict):
        if rpc.proc.poll() is not None:
            raise _ProcessDead("process already exited")
        line = json.dumps(cmd, ensure_ascii=False) + "\n"
        with rpc.stdin_lock:
            try:
                rpc.proc.stdin.write(line)
                rpc.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise _ProcessDead(f"stdin write failed: {e}")

    @staticmethod
    def _stdout_reader(proc: subprocess.Popen, q: queue.Queue):
        try:
            for line in proc.stdout:
                line = line.strip()
                if line:
                    try:
                        q.put(line, timeout=10)
                    except queue.Full:
                        pass
        except (ValueError, OSError):
            pass

    @staticmethod
    def _stderr_drain(proc: subprocess.Popen):
        try:
            for line in proc.stderr:
                line = line.strip()
                if line:
                    log.debug("OMP stderr [pid=%d]: %s", proc.pid, line[:200])
        except (ValueError, OSError):
            pass

    # ── Usage tracking ──

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

        if (not state.compact_detected
                and state.peak_context_tokens >= 50_000
                and ctx_tokens < state.peak_context_tokens * 0.5):
            state.compact_detected = True

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

    # ── Error formatting ──

    @classmethod
    def _format_error(cls, message: str) -> str:
        raw = (message or "").strip() or "Unknown OMP error"
        redacted = re.sub(
            r"(?i)(api[_-]?key|authorization|bearer)\s*[:=]\s*\S+",
            r"\1=<redacted>",
            raw,
        )
        lower = redacted.lower()
        if "invalid api key" in lower or "401" in lower or "unauthorized" in lower:
            return f"OMP provider 鉴权失败：{redacted}"
        if "model not found" in lower or "404" in lower:
            return f"OMP 模型不可用或不存在：{redacted}"
        if (
            "econnrefused" in lower
            or "connection refused" in lower
            or "failed to fetch" in lower
        ):
            return f"OMP provider 不可用：{redacted}"
        return f"OMP 请求失败：{redacted}"

    # ── Session path ──

    def _session_dir(self, session_id: str) -> Path:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
        d = (
            Path(self.workspace)
            / "state"
            / "feishu-bridge"
            / "omp-sessions"
            / safe_id
        )
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── Process lifecycle ──

    def _find_rpc_by_proc(self, proc: subprocess.Popen) -> Optional[_RpcProcess]:
        with self._proc_lock:
            for rpc in self._processes.values():
                if rpc.proc is proc:
                    return rpc
        return None

    def _evict(self, session_id: str):
        with self._proc_lock:
            rpc = self._processes.pop(session_id, None)
        if rpc:
            self._terminate_rpc(rpc)

    def _terminate_rpc(self, rpc: _RpcProcess, graceful_timeout: float = 5):
        if rpc.proc.poll() is not None:
            return
        BaseRunner._kill_proc_tree(rpc.proc, graceful_timeout)

    def _ensure_reaper(self):
        if self._reaper_started:
            return
        self._reaper_started = True
        t = threading.Thread(target=self._reap_loop, daemon=True)
        t.start()

    def _reap_loop(self):
        while not self._shutting_down:
            time.sleep(IDLE_REAP_INTERVAL)
            now = time.monotonic()
            to_evict = []
            with self._proc_lock:
                for sid, rpc in list(self._processes.items()):
                    if rpc.proc.poll() is not None:
                        to_evict.append(sid)
                    elif now - rpc.last_activity > IDLE_REAP_THRESHOLD:
                        log.info("Reaping idle OMP process: sid=%s idle=%.0fs",
                                 sid[:8], now - rpc.last_activity)
                        to_evict.append(sid)
            for sid in to_evict:
                self._evict(sid)

    def shutdown(self):
        self._shutting_down = True
        with self._proc_lock:
            sids = list(self._processes.keys())
        for sid in sids:
            self._evict(sid)
        log.info("OMP runner shutdown: terminated %d processes", len(sids))


class _SpawnError(Exception):
    pass


class _ProcessDead(Exception):
    pass
