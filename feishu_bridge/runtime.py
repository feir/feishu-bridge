"""Runtime primitives for Feishu Bridge."""

import contextlib
import json
import logging
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from importlib.resources import as_file, files
from pathlib import Path
from typing import ClassVar, Optional

log = logging.getLogger("feishu-bridge")

DEFAULT_TIMEOUT = 300  # 5 minutes
DEDUP_TTL = 43200  # 12 hours
DEDUP_MAX = 5000
QUEUE_MAX = 50
MAX_PROMPT_CHARS = 50_000

# In-memory per-session flag: once a session uses Feishu features,
# inject the full CLI prompt for the rest of that session.
# Key: (bot_id, chat_id, thread_id) → True.  Cleared on /new.
feishu_cli_activated: dict[tuple, bool] = {}

# Regex for detecting feishu-related Chinese keywords in message text
_FEISHU_KEYWORD_RE = re.compile(
    r"飞书|文档|表格|日历|邮件|任务|多维表格|wiki|bitable"
)

# Static resources — materialized once at startup via ExitStack
_DATA = files("feishu_bridge.data")
_resource_stack = contextlib.ExitStack()
_BRIDGE_SETTINGS_PATH: Optional[str] = None
_CLI_PROMPT_PATH: Optional[str] = None
_CLI_PROMPT_SUMMARY_PATH: Optional[str] = None


def materialize_data_files():
    """Call once at startup. Extracts data files and holds them for process lifetime."""
    global _BRIDGE_SETTINGS_PATH, _CLI_PROMPT_PATH, _CLI_PROMPT_SUMMARY_PATH
    _BRIDGE_SETTINGS_PATH = str(
        _resource_stack.enter_context(as_file(_DATA.joinpath("bridge-settings.json")))
    )
    _CLI_PROMPT_PATH = str(
        _resource_stack.enter_context(as_file(_DATA.joinpath("cli_prompt.md")))
    )
    _CLI_PROMPT_SUMMARY_PATH = str(
        _resource_stack.enter_context(as_file(_DATA.joinpath("cli_prompt_summary.md")))
    )


def get_bridge_settings_path() -> Optional[str]:
    """Return materialized bridge-settings.json path."""
    return _BRIDGE_SETTINGS_PATH


def get_cli_prompt_path() -> Optional[str]:
    """Return materialized cli_prompt.md path."""
    return _CLI_PROMPT_PATH


def get_cli_prompt_summary_path() -> Optional[str]:
    """Return materialized cli_prompt_summary.md path."""
    return _CLI_PROMPT_SUMMARY_PATH

EMPTY_RESULT_MESSAGE = "Claude 本次未返回任何内容，请稍后重试。"
SILENT_OK_MESSAGE = "✓ 操作已完成（无文本输出）"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """Runner 统一返回结构。"""
    result: str = ""
    session_id: Optional[str] = None
    is_error: bool = False
    cancelled: bool = False
    usage: Optional[dict] = None
    last_call_usage: Optional[dict] = None
    model_usage: Optional[dict] = None
    total_cost_usd: Optional[float] = None
    peak_context_tokens: int = 0
    compact_detected: bool = False
    default_context_window: int = 200_000
    rate_limit_info: Optional[dict] = None

    def to_dict(self) -> dict:
        """向后兼容：转为 dict，保持 camelCase key。"""
        from dataclasses import asdict
        d = {k: v for k, v in asdict(self).items() if v is not None}
        if "model_usage" in d:
            d["modelUsage"] = d.pop("model_usage")
        return d


@dataclass
class StreamState:
    """流式解析过程中的可变状态。"""
    accumulated_text: str = ""
    session_id: Optional[str] = None
    final_result: Optional[dict] = None
    last_call_usage: Optional[dict] = None
    peak_context_tokens: int = 0
    compact_detected: bool = False
    rate_limit_info: Optional[dict] = None
    is_error: bool = False
    done: bool = False
    pending_output: list[str] = field(default_factory=list)
    pending_tool_status: list[str] = field(default_factory=list)
    pending_todo_update: list[dict] | None = None
    pending_agent_launches: list[dict] | None = None


# ---------------------------------------------------------------------------
# Dedup / Session / Queue (unchanged)
# ---------------------------------------------------------------------------

class MessageDedup:
    """LRU message dedup with TTL."""

    def __init__(self, ttl: int = DEDUP_TTL, max_entries: int = DEDUP_MAX):
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._ttl = ttl
        self._max = max_entries
        self._lock = threading.Lock()

    def is_duplicate(self, message_id: str) -> bool:
        now = time.time()
        with self._lock:
            expired = []
            for mid, ts in self._seen.items():
                if now - ts > self._ttl:
                    expired.append(mid)
                else:
                    break
            for mid in expired:
                del self._seen[mid]

            while len(self._seen) >= self._max:
                self._seen.popitem(last=False)

            if message_id in self._seen:
                return True
            self._seen[message_id] = now
            return False


class SessionMap:
    """Thread-safe session mapping with atomic JSON persistence."""

    _AGENT_TYPE_KEY = "_agent_type"

    def __init__(self, path: Path, agent_type: str | None = None):
        self._lock = threading.RLock()
        self._path = path
        self._data: dict[str, str] = {}
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._load()
        if agent_type:
            self._reconcile_agent_type(agent_type)

    def _reconcile_agent_type(self, agent_type: str):
        """Clear stale sessions when agent type changes."""
        stored = self._data.get(self._AGENT_TYPE_KEY)
        if stored == agent_type:
            return  # match — nothing to do

        session_count = sum(1 for k in self._data if k != self._AGENT_TYPE_KEY)
        if stored is None and agent_type == "claude" and session_count > 0:
            # Legacy file without metadata + still using claude → preserve sessions
            log.info("Adding agent_type=claude to existing sessions file")
        elif stored is not None and stored != agent_type and session_count > 0:
            log.warning(
                "Agent type changed %s → %s; clearing %d stale sessions",
                stored, agent_type, session_count,
            )
            self._data = {}
        elif stored is None and agent_type != "claude" and session_count > 0:
            log.warning(
                "Agent type set to %s but existing sessions have no type marker; "
                "clearing %d sessions", agent_type, session_count,
            )
            self._data = {}

        self._data[self._AGENT_TYPE_KEY] = agent_type
        self._save()

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
                count = sum(1 for k in self._data if k != self._AGENT_TYPE_KEY)
                log.info("Loaded %d sessions from %s", count, self._path)
            except (json.JSONDecodeError, IOError) as e:
                log.warning("Failed to load sessions: %s", e)
                self._data = {}

    def _save(self):
        """Best-effort atomic: write tmp (0600) -> fsync -> os.replace."""
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            try:
                os.unlink(str(tmp))
            except OSError:
                pass
            raise
        os.replace(str(tmp), str(self._path))

    @staticmethod
    def _key_str(key: tuple) -> str:
        return ":".join(str(k or "") for k in key)

    def get(self, key: tuple) -> Optional[str]:
        with self._lock:
            return self._data.get(self._key_str(key))

    def put(self, key: tuple, session_id: str):
        with self._lock:
            ks = self._key_str(key)
            old = self._data.get(ks)
            self._data[ks] = session_id
            try:
                self._save()
            except Exception:
                if old is None:
                    self._data.pop(ks, None)
                else:
                    self._data[ks] = old
                raise

    def delete(self, key: tuple):
        with self._lock:
            ks = self._key_str(key)
            old = self._data.pop(ks, None)
            try:
                self._save()
            except Exception:
                if old is not None:
                    self._data[ks] = old
                raise


class SessionQueueFull(Exception):
    """Raised when a session's pending queue exceeds MAX_PENDING."""


class ChatTaskQueue:
    """Per-session FIFO task queue. Only one task per session in flight."""

    MAX_PENDING_PER_SESSION = 10

    def __init__(self, work_queue: queue.Queue):
        self._work_queue = work_queue
        self._active: set[str] = set()
        self._pending: dict[str, deque] = {}
        self._lock = threading.Lock()

    def enqueue(self, key: str, item: dict) -> str:
        with self._lock:
            if key in self._active:
                pending = self._pending.get(key)
                if pending and len(pending) >= self.MAX_PENDING_PER_SESSION:
                    raise SessionQueueFull(
                        f"Session {key} has {self.MAX_PENDING_PER_SESSION} pending"
                    )
                self._pending.setdefault(key, deque()).append(item)
                return "queued"

            self._active.add(key)
            self._work_queue.put_nowait(item)
            return "immediate"

    def on_complete(self, key: str) -> None:
        with self._lock:
            pending = self._pending.get(key)
            if pending:
                next_item = pending.popleft()
                if not pending:
                    del self._pending[key]
                try:
                    self._work_queue.put_nowait(next_item)
                except queue.Full:
                    self._pending.setdefault(key, deque()).appendleft(next_item)
                    log.warning("work_queue full in on_complete, retry in 2s (key=%s)", key)
                    threading.Timer(2.0, self.on_complete, args=(key,)).start()
            else:
                self._active.discard(key)
                self._pending.pop(key, None)

    def drain(self, key: str) -> list:
        with self._lock:
            return list(self._pending.pop(key, deque()))

    def pending_count(self, key: str) -> int:
        with self._lock:
            return len(self._pending.get(key, []))


# ---------------------------------------------------------------------------
# BaseRunner ABC
# ---------------------------------------------------------------------------

class BaseRunner(ABC):
    """Abstract base for AI Agent CLI runners."""

    DEFAULT_MODEL: ClassVar[str]
    ALWAYS_STREAMING: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Enforce DEFAULT_MODEL on concrete subclasses
        if not getattr(cls, '__abstractmethods__', None) and not hasattr(cls, 'DEFAULT_MODEL'):
            raise TypeError(f"{cls.__name__} must define DEFAULT_MODEL")

    _SAFETY_PROMPT = (
        "CRITICAL: You are running as a subprocess of feishu-bridge. "
        "NEVER execute systemctl restart/stop/reload on feishu-bridge - "
        "doing so kills your own parent process, causing an infinite restart loop.\n\n"
        "Do not output 'Status:' lines at the end of responses — "
        "status is tracked externally by the bridge."
    )

    def __init__(self, command: str, model: str, workspace: str, timeout: int,
                 max_budget_usd: Optional[float] = None,
                 extra_system_prompts: Optional[list[str]] = None,
                 extra_system_prompts_summary: Optional[list[str]] = None):
        self.command = command
        self.model = model
        self.workspace = workspace
        self.timeout = timeout
        self.max_budget_usd = max_budget_usd
        self._extra_system_prompts_full = extra_system_prompts or []
        self._extra_system_prompts_summary = extra_system_prompts_summary or []
        self._active: dict[str, subprocess.Popen] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    # ── Abstract methods (subclass must implement) ──

    @abstractmethod
    def build_args(self, prompt: str, session_id: Optional[str],
                   resume: bool, streaming: bool, *,
                   fork_session: bool = False,
                   full_cli_prompt: bool = True) -> list:
        """构建 CLI 命令行参数列表。"""

    @abstractmethod
    def parse_streaming_line(self, event: dict, state: StreamState) -> None:
        """解析单行流式 JSONL 事件，更新 StreamState。"""

    @abstractmethod
    def parse_blocking_output(self, stdout: str, session_id: Optional[str]) -> dict:
        """解析阻塞模式的完整 stdout，返回 result dict。"""

    @abstractmethod
    def get_model_aliases(self) -> dict[str, str]:
        """返回 {alias: full_model_name} 映射。"""

    @abstractmethod
    def get_default_context_window(self) -> int:
        """默认 context window 大小。"""

    # ── Optional overrides ──

    def get_session_not_found_signatures(self) -> list[str]:
        """返回表示 session 不存在的错误签名列表。默认空。"""
        return []

    def get_extra_env(self) -> dict:
        """额外环境变量。默认注入用户 PATH。"""
        env = {}
        # Ensure user-local bin dirs are in PATH for subprocesses.
        # systemd services don't source ~/.profile, so ~/.local/bin etc.
        # are missing from PATH, causing tools like gh to be not found.
        user_bins = [
            os.path.expanduser("~/.local/bin"),
            os.path.expanduser("~/bin"),
        ]
        existing = os.environ.get("PATH", "")
        additions = [p for p in user_bins if os.path.isdir(p) and p not in existing]
        if additions:
            env["PATH"] = ":".join(additions) + ":" + existing
        return env

    def get_display_name(self) -> str:
        """用户可见的 Agent 名称。"""
        return "AI Agent"

    def supports_compact(self) -> bool:
        """是否支持 /compact 命令。"""
        return True

    def _build_system_prompt(self, full: bool = True) -> str:
        """Merge safety guard + extra system prompts into one string.

        Args:
            full: If True, use full CLI prompt. If False, use summary version.
        """
        parts = [self._SAFETY_PROMPT]
        if full:
            parts.extend(self._extra_system_prompts_full)
        else:
            parts.extend(self._extra_system_prompts_summary)
        return "\n\n".join(parts)

    def _build_streaming_result(self, state: StreamState,
                                session_id: Optional[str]) -> Optional[dict]:
        """Build content-level result from streaming state.

        Return dict if a definitive result is available, None to fall through
        to generic BaseRunner fallbacks (timeout, exit code, empty text).
        Subclasses override this for protocol-specific result handling.
        """
        return None

    # ── Shared subprocess management ──

    @staticmethod
    def _force_kill(proc: subprocess.Popen):
        """Send SIGKILL to process tree (last resort)."""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    @staticmethod
    def _kill_proc_tree(proc: subprocess.Popen, graceful_timeout: float = 15):
        """Non-blocking graceful kill: SIGTERM now, SIGKILL after grace period.

        Does NOT call proc.wait() — the caller's main thread handles reaping.
        This avoids concurrent proc.wait() races when called from a Timer thread.
        """
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError):
            return
        # Phase 1: send SIGTERM (non-blocking)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        # Phase 2: schedule SIGKILL after grace period (non-blocking).
        def _deferred_sigkill():
            if proc.poll() is None:  # Still alive
                log.warning("Process %d did not exit after SIGTERM (%ds), sending SIGKILL",
                            proc.pid, graceful_timeout)
                BaseRunner._force_kill(proc)
        threading.Timer(graceful_timeout, _deferred_sigkill).start()

    def cancel(self, tag: str) -> bool:
        with self._lock:
            proc = self._active.get(tag)
            if proc:
                self._cancelled.add(tag)
        if proc:
            log.info("Cancelling %s process: tag=%s pid=%d",
                     self.get_display_name(), tag, proc.pid)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            return True
        return False

    def _cleanup_tag(self, tag: Optional[str]) -> bool:
        if not tag:
            return False
        with self._lock:
            self._active.pop(tag, None)
            was_cancelled = tag in self._cancelled
            self._cancelled.discard(tag)
        return was_cancelled

    def run(self, prompt: str, session_id: Optional[str] = None,
            resume: bool = False, tag: Optional[str] = None,
            on_output=None, on_tool_status=None, on_todo_update=None,
            on_agent_update=None, env_extra: Optional[dict] = None,
            fork_session: bool = False,
            full_cli_prompt: bool = True) -> dict:

        if len(prompt) > MAX_PROMPT_CHARS:
            log.warning("Prompt truncated: %d -> %d chars", len(prompt), MAX_PROMPT_CHARS)
            prompt = prompt[:MAX_PROMPT_CHARS] + "\n\n...(message truncated)"

        streaming = bool(on_output) or self.ALWAYS_STREAMING
        args = self.build_args(prompt, session_id, resume, streaming,
                               fork_session=fork_session,
                               full_cli_prompt=full_cli_prompt)

        _sp = self._build_system_prompt(full=full_cli_prompt)
        log.info("%s: resume=%s sid=%s stream=%s full_cli=%s prompt=%d chars sys_prompt=%d chars (~%d tokens)",
                 self.get_display_name(), resume,
                 session_id[:8] if session_id else "-",
                 streaming, full_cli_prompt, len(prompt), len(_sp), len(_sp) // 4)

        env = None
        extra_env = self.get_extra_env()
        if env_extra:
            extra_env.update(env_extra)
        if extra_env:
            env = os.environ.copy()
            env.update(extra_env)

        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.workspace,
            env=env,
            start_new_session=True,
        )

        if tag:
            with self._lock:
                self._active[tag] = proc

        if streaming:
            result = self._run_streaming(proc, session_id, tag, on_output,
                                         on_tool_status=on_tool_status,
                                         on_todo_update=on_todo_update,
                                         on_agent_update=on_agent_update)
        else:
            result = self._run_blocking(proc, session_id, tag)

        result["default_context_window"] = self.get_default_context_window()
        return result

    def _run_blocking(self, proc, session_id, tag) -> dict:
        t0 = time.monotonic()
        try:
            stdout, stderr = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t0
            log.error(
                "%s blocking timeout: sid=%s elapsed=%.0fs limit=%ds",
                self.get_display_name(),
                (session_id or "-")[:8], elapsed, self.timeout,
            )
            # Graceful kill: SIGTERM + deferred SIGKILL. proc.communicate()
            # blocks until exit — will unblock after SIGTERM or deferred SIGKILL.
            self._kill_proc_tree(proc)
            proc.communicate()
            return {
                "result": f"{self.get_display_name()} 超时（已运行 {int(elapsed)}s，限制 {self.timeout}s）",
                "session_id": session_id,
                "is_error": True,
            }
        finally:
            was_cancelled = self._cleanup_tag(tag)

        if tag and was_cancelled:
            return {
                "result": "任务已取消。",
                "session_id": session_id,
                "is_error": False,
                "cancelled": True,
            }

        if proc.returncode != 0 and not stdout.strip():
            return {
                "result": f"{self.get_display_name()} 退出码 {proc.returncode}: {stderr[:500]}",
                "session_id": session_id,
                "is_error": True,
            }

        return self.parse_blocking_output(stdout, session_id)

    def _run_streaming(self, proc, session_id, tag, on_output,
                        on_tool_status=None, on_todo_update=None,
                        on_agent_update=None) -> dict:
        state = StreamState(session_id=session_id)
        timed_out = False
        result_received = threading.Event()
        stderr_lines = []
        t0 = time.monotonic()

        def _drain_stderr():
            for line in proc.stderr:
                stderr_lines.append(line)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        def _timeout_kill():
            nonlocal timed_out
            if result_received.is_set():
                return  # Result already received; don't flag as timeout.
            timed_out = True
            # Graceful kill spawns its own deferred SIGKILL Timer (15s).
            BaseRunner._kill_proc_tree(proc)

        # Idle timeout: resets on every stdout line from the CLI.
        timer = threading.Timer(self.timeout, _timeout_kill)
        timer.start()

        def _reset_idle_timer():
            nonlocal timer
            timer.cancel()
            timer = threading.Timer(self.timeout, _timeout_kill)
            timer.start()

        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                _reset_idle_timer()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                self.parse_streaming_line(event, state)

                # Drain pending_output → on_output callback
                if on_output and state.pending_output:
                    for text in state.pending_output:
                        on_output(text)
                    state.pending_output.clear()

                # Drain pending_tool_status → on_tool_status callback
                # Only send the last tool name (the one visually shown).
                if on_tool_status and state.pending_tool_status:
                    on_tool_status(state.pending_tool_status[-1])
                    state.pending_tool_status.clear()

                # Drain pending_todo_update → on_todo_update callback
                if on_todo_update and state.pending_todo_update is not None:
                    on_todo_update(state.pending_todo_update)
                    state.pending_todo_update = None

                # Drain pending_agent_launches → on_agent_update callback
                if on_agent_update and state.pending_agent_launches is not None:
                    on_agent_update(state.pending_agent_launches)
                    state.pending_agent_launches = None

                if state.done:
                    result_received.set()
                    break

            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t0
            # Distinguish: idle timer kill (timed_out=True) vs proc.wait(30) hang.
            if timed_out:
                # The idle timer fired, killed the process, stdout EOF'd,
                # and now proc.wait(30) also timed out — unusual but possible.
                log.error(
                    "%s idle timeout + proc.wait hang: sid=%s elapsed=%.0fs idle_limit=%ds",
                    self.get_display_name(),
                    (state.session_id or session_id or "-")[:8], elapsed, self.timeout,
                )
                self._force_kill(proc)
                proc.wait()
            elif state.done:
                # Agent sent a result event (task completed!) but the process
                # didn't exit within 30s. Treat as success, not timeout.
                log.warning(
                    "%s process hung after result event: sid=%s elapsed=%.0fs, force-killing",
                    self.get_display_name(),
                    (state.session_id or session_id or "-")[:8], elapsed,
                )
                self._force_kill(proc)
                proc.wait()
                # Fall through to the content result handler below.
            else:
                # stdout closed without a result event and process won't exit.
                # Likely a crash or abnormal termination.
                log.error(
                    "%s process hung (no result): sid=%s elapsed=%.0fs accumulated=%d chars, force-killing",
                    self.get_display_name(),
                    (state.session_id or session_id or "-")[:8], elapsed, len(state.accumulated_text),
                )
                self._force_kill(proc)
                proc.wait()
                return {
                    "result": (state.accumulated_text + "\n\n⚠️ %s 进程未正常退出（已运行 %ds）" % (self.get_display_name(), int(elapsed)))
                             if state.accumulated_text else
                             "%s 进程未正常退出（已运行 %ds，无输出）" % (self.get_display_name(), int(elapsed)),
                    "session_id": state.session_id or session_id,
                    "is_error": True,
                }
        except Exception:
            self._force_kill(proc)
            proc.wait()
            raise
        finally:
            timer.cancel()
            stderr_thread.join(timeout=5)
            was_cancelled = self._cleanup_tag(tag)

        if tag and was_cancelled:
            return {
                "result": "任务已取消。",
                "session_id": state.session_id or session_id,
                "is_error": False,
                "cancelled": True,
            }

        # Content-level result from subclass (checked BEFORE timed_out
        # to handle race where idle timer fires during proc.wait after
        # result was already received).
        content_result = self._build_streaming_result(state, session_id)
        if content_result is not None:
            return content_result

        if timed_out:
            elapsed = time.monotonic() - t0
            log.error(
                "%s idle timeout: sid=%s elapsed=%.0fs idle_limit=%ds",
                self.get_display_name(),
                (state.session_id or session_id or "-")[:8], elapsed, self.timeout,
            )
            return {
                "result": f"{self.get_display_name()} 空闲超时（连续无输出超过 {self.timeout}s，已运行 {int(elapsed)}s）",
                "session_id": state.session_id or session_id,
                "is_error": True,
            }

        stderr = "".join(stderr_lines)
        if proc.returncode != 0:
            return {
                "result": f"{self.get_display_name()} 退出码 {proc.returncode}: {stderr[:500]}",
                "session_id": state.session_id or session_id,
                "is_error": True,
            }

        if not state.accumulated_text:
            log.warning(
                "%s streaming completed without text or result event: sid=%s stderr_len=%d",
                self.get_display_name(),
                (state.session_id or session_id or "-")[:8],
                len(stderr),
            )
            return {
                "result": f"{self.get_display_name()} 本次未返回任何内容，请稍后重试。",
                "session_id": state.session_id or session_id,
                "is_error": True,
            }

        return {
            "result": state.accumulated_text,
            "session_id": state.session_id or session_id,
            "is_error": state.is_error,
            "peak_context_tokens": state.peak_context_tokens,
            "compact_detected": state.compact_detected,
            "rate_limit_info": state.rate_limit_info,
        }


# ---------------------------------------------------------------------------
# ClaudeRunner
# ---------------------------------------------------------------------------

class ClaudeRunner(BaseRunner):
    """Claude Code CLI runner."""

    DEFAULT_MODEL = "claude-opus-4-6"

    SESSION_NOT_FOUND_SIGNATURES = [
        "session not found",
        "Session not found",
        "no such session",
        "session does not exist",
        "sessionId that does not exist",
        "Could not find session",
        "ENOENT",
        "no such file or directory",
    ]

    def build_args(self, prompt, session_id, resume, streaming, *,
                   fork_session=False, full_cli_prompt=True):
        args = [
            self.command, "-p",
            "--dangerously-skip-permissions",
            "--settings", get_bridge_settings_path(),
            "--model", self.model,
            "--append-system-prompt",
            self._build_system_prompt(full=full_cli_prompt),
        ]

        if self.max_budget_usd is not None:
            args.extend(["--max-budget-usd", str(self.max_budget_usd)])

        if streaming:
            args.extend(["--output-format", "stream-json",
                         "--verbose", "--include-partial-messages"])
        else:
            args.extend(["--output-format", "json"])

        if resume and session_id:
            args.extend(["--resume", session_id])
            if fork_session:
                args.extend(["--fork-session", "--disallowed-tools", "*"])
        elif session_id:
            args.extend(["--session-id", session_id])

        args.append("--")
        args.append(prompt)
        return args

    def parse_streaming_line(self, event, state):
        etype = event.get("type", "")
        if etype == "result":
            state.final_result = event
            state.done = True
        elif etype == "assistant":
            msg = event.get("message", {})
            msg_usage = msg.get("usage")
            if msg_usage:
                state.last_call_usage = msg_usage
                # Track peak context tokens (pre-compact high-water mark).
                ctx_tokens = (msg_usage.get("input_tokens", 0)
                              + msg_usage.get("cache_read_input_tokens", 0)
                              + msg_usage.get("cache_creation_input_tokens", 0))
                if ctx_tokens > state.peak_context_tokens:
                    state.peak_context_tokens = ctx_tokens
            # Extract tool-use names for progress feedback.
            for block in msg.get("content", []):
                if block.get("type") == "tool_use":
                    tool_name = block.get("name", "")
                    if tool_name:
                        state.pending_tool_status.append(tool_name)
                    if tool_name == "TodoWrite":
                        todos = block.get("input", {}).get("todos")
                        if isinstance(todos, list):
                            state.pending_todo_update = todos
                    elif tool_name == "Agent":
                        ai = block.get("input", {})
                        if not ai.get("run_in_background"):
                            launch = {
                                "description": ai.get("description", ""),
                                "name": ai.get("name"),
                                "subagent_type": ai.get("subagent_type", ""),
                            }
                            if state.pending_agent_launches is None:
                                state.pending_agent_launches = []
                            state.pending_agent_launches.append(launch)
        elif etype == "rate_limit_event":
            rli = event.get("rate_limit_info")
            if rli:
                state.rate_limit_info = rli
        elif etype == "stream_event":
            inner = event.get("event", {})
            if (inner.get("type") == "content_block_delta"
                    and inner.get("delta", {}).get("type") == "text_delta"):
                state.accumulated_text += inner["delta"].get("text", "")
                state.pending_output.append(state.accumulated_text)
            elif inner.get("type") == "message_delta":
                edits = (inner.get("delta", {})
                         .get("context_management", {})
                         .get("applied_edits"))
                if edits:
                    state.compact_detected = True
                    log.info("Auto-compact detected: %d edit(s) applied", len(edits))

    def parse_blocking_output(self, stdout, session_id):
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return {
                "result": f"{self.get_display_name()} 输出解析失败: {stdout[:500]}",
                "session_id": session_id,
                "is_error": True,
            }

        result_text = data.get("result", "")
        if not data.get("is_error", False) and not result_text:
            log.info(
                "Claude returned empty blocking result (silent OK): sid=%s stdout_len=%d",
                (data.get("session_id") or session_id or "-")[:8],
                len(stdout),
            )
            return {
                "result": SILENT_OK_MESSAGE,
                "session_id": data.get("session_id", session_id),
                "is_error": False,
                "usage": data.get("usage"),
                "modelUsage": data.get("modelUsage"),
                "total_cost_usd": data.get("total_cost_usd"),
            }

        return {
            "result": result_text,
            "session_id": data.get("session_id", session_id),
            "is_error": data.get("is_error", False),
            "usage": data.get("usage"),
            "modelUsage": data.get("modelUsage"),
            "total_cost_usd": data.get("total_cost_usd"),
        }

    def _build_streaming_result(self, state, session_id):
        if not state.final_result:
            return None

        fr = state.final_result
        result_text = fr.get("result") or state.accumulated_text
        sid = fr.get("session_id", session_id)

        if not fr.get("is_error", False) and not result_text:
            log.info(
                "Claude returned empty streaming result (silent OK): sid=%s accumulated=%d",
                (sid or "-")[:8],
                len(state.accumulated_text),
            )
            return {
                "result": SILENT_OK_MESSAGE,
                "session_id": sid,
                "is_error": False,
                "usage": fr.get("usage"),
                "last_call_usage": state.last_call_usage,
                "modelUsage": fr.get("modelUsage"),
                "total_cost_usd": fr.get("total_cost_usd"),
                "peak_context_tokens": state.peak_context_tokens,
                "compact_detected": state.compact_detected,
                "rate_limit_info": state.rate_limit_info,
            }

        if state.accumulated_text and not fr.get("result"):
            log.info(
                "Claude streaming fallback used accumulated text: sid=%s chars=%d",
                (sid or "-")[:8],
                len(state.accumulated_text),
            )

        return {
            "result": result_text,
            "session_id": sid,
            "is_error": fr.get("is_error", False),
            "usage": fr.get("usage"),
            "last_call_usage": state.last_call_usage,
            "modelUsage": fr.get("modelUsage"),
            "total_cost_usd": fr.get("total_cost_usd"),
            "peak_context_tokens": state.peak_context_tokens,
            "compact_detected": state.compact_detected,
            "rate_limit_info": state.rate_limit_info,
        }

    def get_model_aliases(self):
        return {
            "opus": "claude-opus-4-6",
            "sonnet": "claude-sonnet-4-6",
            "haiku": "claude-haiku-4-5",
        }

    def get_default_context_window(self):
        m = (self.model or "").lower()
        if "opus" in m:
            return 1_000_000
        return 200_000

    def get_session_not_found_signatures(self):
        return self.SESSION_NOT_FOUND_SIGNATURES

    def get_display_name(self):
        return "Claude"


# ---------------------------------------------------------------------------
# CodexRunner
# ---------------------------------------------------------------------------

class CodexRunner(BaseRunner):
    """OpenAI Codex CLI runner."""

    DEFAULT_MODEL = "gpt-5.2-codex"
    ALWAYS_STREAMING = True  # session_id comes from first event (thread.started)

    def __init__(self, command: str, model: str, workspace: str, timeout: int,
                 max_budget_usd: Optional[float] = None,
                 extra_system_prompts: Optional[list[str]] = None,
                 extra_system_prompts_summary: Optional[list[str]] = None):
        if max_budget_usd is not None:
            log.warning("Codex does not support budget tracking, max_budget_usd ignored")
        super().__init__(
            command=command, model=model, workspace=workspace,
            timeout=timeout, max_budget_usd=None,
            extra_system_prompts=extra_system_prompts,
            extra_system_prompts_summary=extra_system_prompts_summary,
        )
        # Thread-local storage for per-invocation temp file path.
        # run() writes the path; build_args() reads it (same thread).
        self._tls = threading.local()

    def build_args(self, prompt, session_id, resume, streaming, *,
                   fork_session=False, full_cli_prompt=True):
        args = [
            self.command, "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "-C", self.workspace,
            "-m", self.model,
        ]

        # Inject system prompt via -c model_instructions_file (set by run())
        instructions_path = getattr(self._tls, "instructions_path", None)
        if instructions_path:
            args.extend(["-c", f"model_instructions_file={instructions_path}"])

        if resume and session_id:
            args.extend(["resume", session_id, "--", prompt])
        else:
            # Codex assigns its own thread_id; ignore caller-provided session_id
            args.extend(["--", prompt])

        return args

    def run(self, prompt: str, session_id: Optional[str] = None,
            resume: bool = False, tag: Optional[str] = None,
            on_output=None, on_tool_status=None, on_todo_update=None,
            on_agent_update=None, env_extra: Optional[dict] = None,
            full_cli_prompt: bool = True) -> dict:
        """Override run() to manage per-invocation system prompt temp file."""
        self._tls.instructions_path = None
        try:
            system_prompt = self._build_system_prompt(full=full_cli_prompt)
            if system_prompt:
                fd, path = tempfile.mkstemp(
                    prefix="codex-instructions-", suffix=".md", text=True,
                )
                # Set early so finally can always clean up, even if fdopen/write fails
                self._tls.instructions_path = path
                with os.fdopen(fd, "w") as f:
                    f.write(system_prompt)

            return super().run(
                prompt, session_id=session_id, resume=resume,
                tag=tag, on_output=on_output, on_tool_status=on_tool_status,
                on_todo_update=on_todo_update, on_agent_update=on_agent_update,
                env_extra=env_extra, full_cli_prompt=full_cli_prompt,
            )
        finally:
            path = getattr(self._tls, "instructions_path", None)
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                self._tls.instructions_path = None

    def parse_streaming_line(self, event, state):
        etype = event.get("type", "")

        if etype == "thread.started":
            # Session ID comes from the first event; ignore if missing/null
            # to avoid persisting a caller-generated placeholder as a real
            # Codex thread id (which would break later resume attempts).
            tid = event.get("thread_id")
            if tid:
                state.session_id = tid

        elif etype == "item.completed":
            item = event.get("item") or {}
            item_type = item.get("type", "")
            if item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    state.accumulated_text += text
                    state.pending_output.append(state.accumulated_text)
            elif item_type == "error":
                err_msg = item.get("text", "") or item.get("message", "")
                log.error("Codex item error: %s", err_msg)
                # Propagate error — may be the only error signal before stream ends
                state.accumulated_text += (
                    f"\n\n⚠️ Codex error: {err_msg}" if state.accumulated_text
                    else f"Codex error: {err_msg}"
                )
                state.is_error = True
            # command_execution items are intermediate tool-use events — ignore

        elif etype == "turn.completed":
            usage = event.get("usage", {})
            if usage:
                # Normalize Codex usage keys to match Claude convention
                state.last_call_usage = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "cache_read_input_tokens": usage.get("cached_input_tokens", 0),
                    "cache_creation_input_tokens": 0,
                    "output_tokens": usage.get("output_tokens", 0),
                }
                ctx_tokens = (
                    state.last_call_usage["input_tokens"]
                    + state.last_call_usage["cache_read_input_tokens"]
                )
                if ctx_tokens > state.peak_context_tokens:
                    state.peak_context_tokens = ctx_tokens
            state.done = True

        elif etype == "turn.failed":
            err = event.get("error", {})
            err_msg = err.get("message", "") if isinstance(err, dict) else str(err)
            log.error("Codex turn failed: %s", err_msg)
            state.accumulated_text += f"\n\n⚠️ Codex error: {err_msg}" if state.accumulated_text else f"Codex error: {err_msg}"
            state.is_error = True
            state.done = True

        elif etype == "error":
            err_msg = event.get("message") or "Unknown error"
            log.error("Codex top-level error: %s", err_msg)
            state.accumulated_text += f"\n\n⚠️ Codex error: {err_msg}" if state.accumulated_text else f"Codex error: {err_msg}"
            state.is_error = True
            state.done = True

        # turn.started — ignored (no useful data)

    def _build_streaming_result(self, state, session_id):
        if not state.done:
            return None

        sid = state.session_id or session_id
        result_text = state.accumulated_text

        if not result_text:
            return None  # Fall through to BaseRunner empty-text handler

        return {
            "result": result_text,
            "session_id": sid,
            "is_error": state.is_error,
            "usage": state.last_call_usage or {},
            "last_call_usage": state.last_call_usage or {},
            "peak_context_tokens": state.peak_context_tokens,
            "compact_detected": False,
        }

    def parse_blocking_output(self, stdout, session_id):
        # CodexRunner always streams (ALWAYS_STREAMING=True).
        # This method should never be called.
        raise NotImplementedError("CodexRunner always uses streaming mode")

    def get_model_aliases(self):
        return {
            "codex": "gpt-5.2-codex",
            "codex-mini": "gpt-5.1-codex-mini",
        }

    def get_default_context_window(self):
        return 200_000

    def get_display_name(self):
        return "Codex"

    def supports_compact(self):
        return False
