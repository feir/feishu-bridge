"""Runtime primitives for Feishu Bridge."""

import contextlib
import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from collections import OrderedDict, deque
from importlib.resources import as_file, files
from pathlib import Path
from typing import Optional

log = logging.getLogger("feishu-bridge")

DEFAULT_TIMEOUT = 300  # 5 minutes
DEDUP_TTL = 43200  # 12 hours
DEDUP_MAX = 5000
QUEUE_MAX = 50
MAX_PROMPT_CHARS = 50_000

# Static resources — materialized once at startup via ExitStack
_DATA = files("feishu_bridge.data")
_resource_stack = contextlib.ExitStack()
_BRIDGE_SETTINGS_PATH: Optional[str] = None
_CLI_PROMPT_PATH: Optional[str] = None


def materialize_data_files():
    """Call once at startup. Extracts data files and holds them for process lifetime."""
    global _BRIDGE_SETTINGS_PATH, _CLI_PROMPT_PATH
    _BRIDGE_SETTINGS_PATH = str(
        _resource_stack.enter_context(as_file(_DATA.joinpath("bridge-settings.json")))
    )
    _CLI_PROMPT_PATH = str(
        _resource_stack.enter_context(as_file(_DATA.joinpath("cli_prompt.md")))
    )


def get_bridge_settings_path() -> Optional[str]:
    """Return materialized bridge-settings.json path."""
    return _BRIDGE_SETTINGS_PATH


def get_cli_prompt_path() -> Optional[str]:
    """Return materialized cli_prompt.md path."""
    return _CLI_PROMPT_PATH

EMPTY_RESULT_MESSAGE = "Claude 本次未返回任何内容，请稍后重试。"
SILENT_OK_MESSAGE = "✓ 操作已完成（无文本输出）"


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

    def __init__(self, path: Path):
        self._lock = threading.RLock()
        self._path = path
        self._data: dict[str, str] = {}
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
                log.info("Loaded %d sessions from %s", len(self._data), self._path)
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


class ClaudeRunner:
    """Spawn claude -p one-shot and parse JSON result."""

    def __init__(self, command: str, model: str, workspace: str, timeout: int,
                 max_budget_usd: Optional[float] = None,
                 extra_system_prompts: list[str] = None):
        self.command = command
        self.model = model
        self.workspace = workspace
        self.timeout = timeout
        self.max_budget_usd = max_budget_usd
        self._extra_system_prompts = extra_system_prompts or []
        self._active: dict[str, subprocess.Popen] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()


    _SAFETY_PROMPT = (
        "CRITICAL: You are running as a subprocess of feishu-bridge. "
        "NEVER execute systemctl restart/stop/reload on feishu-bridge - "
        "doing so kills your own parent process, causing an infinite restart loop."
    )

    def _build_system_prompt(self) -> str:
        """Merge safety guard + extra system prompts into one string."""
        parts = [self._SAFETY_PROMPT]
        parts.extend(self._extra_system_prompts)
        return "\n\n".join(parts)

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
        # The Timer is a non-daemon thread, so it keeps the process alive
        # until it fires. If the child exits before the timer, poll()
        # returns non-None and we skip the SIGKILL — the Timer then
        # exits cleanly with no side effects.
        def _deferred_sigkill():
            if proc.poll() is None:  # Still alive
                log.warning("Process %d did not exit after SIGTERM (%ds), sending SIGKILL",
                            proc.pid, graceful_timeout)
                ClaudeRunner._force_kill(proc)
        threading.Timer(graceful_timeout, _deferred_sigkill).start()

    def cancel(self, tag: str) -> bool:
        with self._lock:
            proc = self._active.get(tag)
            if proc:
                self._cancelled.add(tag)
        if proc:
            log.info("Cancelling claude process: tag=%s pid=%d", tag, proc.pid)
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
            on_output=None, env_extra: dict = None) -> dict:
        args = [
            self.command, "-p",
            "--dangerously-skip-permissions",
            "--settings", get_bridge_settings_path(),
            "--model", self.model,
            "--append-system-prompt",
            self._build_system_prompt(),
        ]

        if self.max_budget_usd is not None:
            args.extend(["--max-budget-usd", str(self.max_budget_usd)])

        if on_output:
            args.extend(["--output-format", "stream-json",
                         "--verbose", "--include-partial-messages"])
        else:
            args.extend(["--output-format", "json"])

        if resume and session_id:
            args.extend(["--resume", session_id])
        elif session_id:
            args.extend(["--session-id", session_id])

        if len(prompt) > MAX_PROMPT_CHARS:
            log.warning("Prompt truncated: %d -> %d chars", len(prompt), MAX_PROMPT_CHARS)
            prompt = prompt[:MAX_PROMPT_CHARS] + "\n\n...(message truncated)"

        args.append("--")
        args.append(prompt)

        log.info("claude: resume=%s sid=%s stream=%s prompt=%d chars",
                 resume, session_id[:8] if session_id else "-",
                 bool(on_output), len(prompt))

        env = None
        if env_extra:
            env = os.environ.copy()
            env.update(env_extra)

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

        if on_output:
            return self._run_streaming(proc, session_id, tag, on_output)
        return self._run_blocking(proc, session_id, tag)

    def _run_blocking(self, proc, session_id, tag) -> dict:
        try:
            stdout, stderr = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            # Graceful kill: SIGTERM + deferred SIGKILL. proc.communicate()
            # blocks until exit — will unblock after SIGTERM or deferred SIGKILL.
            self._kill_proc_tree(proc)
            proc.communicate()
            return {
                "result": f"Claude 超时（{self.timeout}s）",
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
                "result": f"Claude 退出码 {proc.returncode}: {stderr[:500]}",
                "session_id": session_id,
                "is_error": True,
            }

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return {
                "result": f"Claude 输出解析失败: {stdout[:500]}",
                "session_id": session_id,
                "is_error": True,
            }

        result_text = data.get("result", "")
        if not data.get("is_error", False) and not result_text:
            log.info(
                "Claude returned empty blocking result (silent OK): sid=%s stdout_len=%d stderr_len=%d",
                (data.get("session_id") or session_id or "-")[:8],
                len(stdout),
                len(stderr),
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

    def _run_streaming(self, proc, session_id, tag, on_output) -> dict:
        accumulated = ""
        final_result = None
        last_call_usage = None  # per-call usage from last assistant event
        timed_out = False
        stderr_lines = []

        def _drain_stderr():
            for line in proc.stderr:
                stderr_lines.append(line)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        def _timeout_kill():
            nonlocal timed_out
            timed_out = True
            # Graceful kill spawns its own deferred SIGKILL Timer (15s).
            # If the process exits from SIGTERM, stdout EOF unblocks the
            # main loop; the deferred timer no-ops via poll() check.
            ClaudeRunner._kill_proc_tree(proc)

        # Idle timeout: resets on every stdout line from the CLI.
        # This keeps long-running but active sessions alive (e.g. multi-hour
        # tool-use chains) while still killing truly stuck processes.
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

                etype = event.get("type", "")
                if etype == "result":
                    final_result = event
                    break
                if etype == "assistant":
                    # Capture per-call usage from each assistant message.
                    # The CLI emits one assistant event per API sub-call;
                    # the last one reflects the actual context window fill.
                    msg_usage = event.get("message", {}).get("usage")
                    if msg_usage:
                        last_call_usage = msg_usage
                elif etype == "stream_event":
                    inner = event.get("event", {})
                    if (inner.get("type") == "content_block_delta"
                            and inner.get("delta", {}).get("type") == "text_delta"):
                        accumulated += inner["delta"].get("text", "")
                        on_output(accumulated)

            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self._force_kill(proc)
            proc.wait()
            return {
                "result": f"Claude 超时（{self.timeout}s）",
                "session_id": session_id,
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
                "session_id": session_id,
                "is_error": False,
                "cancelled": True,
            }

        if timed_out:
            return {
                "result": f"Claude 超时（{self.timeout}s）",
                "session_id": session_id,
                "is_error": True,
            }

        if final_result:
            result_text = final_result.get("result") or accumulated
            if not final_result.get("is_error", False) and not result_text:
                log.info(
                    "Claude returned empty streaming result (silent OK): sid=%s accumulated=%d stderr_len=%d",
                    (final_result.get("session_id") or session_id or "-")[:8],
                    len(accumulated),
                    len(stderr_lines),
                )
                return {
                    "result": SILENT_OK_MESSAGE,
                    "session_id": final_result.get("session_id", session_id),
                    "is_error": False,
                    "usage": final_result.get("usage"),
                    "last_call_usage": last_call_usage,
                    "modelUsage": final_result.get("modelUsage"),
                    "total_cost_usd": final_result.get("total_cost_usd"),
                }
            if accumulated and not final_result.get("result"):
                log.info(
                    "Claude streaming fallback used accumulated text: sid=%s chars=%d",
                    (final_result.get("session_id") or session_id or "-")[:8],
                    len(accumulated),
                )
            return {
                "result": result_text,
                "session_id": final_result.get("session_id", session_id),
                "is_error": final_result.get("is_error", False),
                "usage": final_result.get("usage"),
                "last_call_usage": last_call_usage,
                "modelUsage": final_result.get("modelUsage"),
                "total_cost_usd": final_result.get("total_cost_usd"),
            }

        stderr = "".join(stderr_lines)
        if proc.returncode != 0:
            return {
                "result": f"Claude 退出码 {proc.returncode}: {stderr[:500]}",
                "session_id": session_id,
                "is_error": True,
            }

        if not accumulated:
            log.warning(
                "Claude streaming completed without text or result event: sid=%s stderr_len=%d",
                (session_id or "-")[:8],
                len(stderr),
            )
            return {
                "result": EMPTY_RESULT_MESSAGE,
                "session_id": session_id,
                "is_error": True,
            }

        return {
            "result": accumulated,
            "session_id": session_id,
            "is_error": False,
        }
