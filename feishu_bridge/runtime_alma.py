"""Alma desktop app runner — WS API backend for feishu-bridge.

AlmaRunner communicates with Alma's local WebSocket API (localhost:23001)
instead of spawning a CLI subprocess. Bridge retains Feishu card rendering;
Alma handles LLM calls, tool execution, memory, and compaction.
"""

import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import ClassVar, Optional

from feishu_bridge.runtime import BaseRunner, MAX_PROMPT_CHARS, _extract_hint_data

log = logging.getLogger("feishu-bridge")

ALMA_HOST = "localhost"
ALMA_PORT = 23001
ALMA_WS_URL = f"ws://{ALMA_HOST}:{ALMA_PORT}/ws/threads"
ALMA_HTTP_BASE = f"http://{ALMA_HOST}:{ALMA_PORT}"

_MODEL_MAP = {
    "opus": "claude-subscription:claude-opus-4-20250514",
    "sonnet": "claude-subscription:claude-sonnet-4-20250514",
    "haiku": "claude-subscription:claude-haiku-4-5-20251001",
}

_TOOL_NAME_MAP = {
    "bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "grep": "Grep",
    "glob": "Glob",
    "agent": "Agent",
    "web_search": "WebSearch",
    "web_fetch": "WebFetch",
    "notebook_edit": "NotebookEdit",
}


# ---------------------------------------------------------------------------
# Alma HTTP helpers
# ---------------------------------------------------------------------------

def _alma_http(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{ALMA_HTTP_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")[:500]
        raise RuntimeError(
            f"Alma API {method} {path} returned {e.code}: {body_text}"
        ) from e
    except urllib.error.URLError as e:
        raise ConnectionError(
            f"Alma API unreachable ({url}): {e.reason}"
        ) from e


def _is_alma_running() -> bool:
    try:
        _alma_http("GET", "/api/health")
        return True
    except Exception:
        return False


def _create_alma_thread(title: str = "feishu-bridge") -> str:
    resp = _alma_http("POST", "/api/threads", {"title": title})
    thread_id = resp.get("id") or resp.get("threadId")
    if not thread_id:
        raise RuntimeError(f"Alma create-thread returned no ID: {resp}")
    return thread_id


def _thread_exists(thread_id: str) -> bool:
    try:
        _alma_http("GET", f"/api/threads/{thread_id}")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# AlmaThreadMap — persistent {session_key → alma_thread_id}
# ---------------------------------------------------------------------------

class AlmaThreadMap:
    """Thread-safe atomic-write mapping stored at state/alma-threads-<bot>.json."""

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
                log.info("Loaded %d alma-thread mappings from %s",
                         len(self._data), self._path)
            except (json.JSONDecodeError, IOError) as e:
                log.warning("Failed to load alma threads: %s", e)
                self._data = {}

    def _save(self):
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

    def get(self, session_key: str) -> Optional[str]:
        with self._lock:
            return self._data.get(session_key)

    def put(self, session_key: str, thread_id: str):
        with self._lock:
            self._data[session_key] = thread_id
            self._save()

    def delete(self, session_key: str):
        with self._lock:
            if self._data.pop(session_key, None) is not None:
                self._save()


# ---------------------------------------------------------------------------
# AlmaWSManager — persistent WS + background reader + per-thread dispatch
# ---------------------------------------------------------------------------

class AlmaWSManager:
    """Single persistent WebSocket to Alma, dispatching events by threadId."""

    _CONNECT_TIMEOUT = 5

    def __init__(self):
        self._ws = None
        self._reader_thread: Optional[threading.Thread] = None
        self._state = "DISCONNECTED"
        self._pending: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()

    def ensure_connected(self):
        with self._lock:
            if self._state == "CONNECTED" and self._ws is not None:
                return
        self._connect()

    def _connect(self):
        try:
            import websocket
        except ImportError:
            raise ConnectionError(
                "websocket-client package not installed. "
                "Run: pip install websocket-client"
            )

        try:
            ws = websocket.WebSocket(enable_multithread=True)
            ws.settimeout(self._CONNECT_TIMEOUT)
            ws.connect(ALMA_WS_URL)
        except Exception as e:
            with self._lock:
                self._state = "DISCONNECTED"
            raise ConnectionError(
                f"Cannot connect to Alma WS ({ALMA_WS_URL}): {e}"
            ) from e

        with self._lock:
            old_ws = self._ws
            self._ws = ws
            self._state = "CONNECTED"
            self._stop.clear()

        if old_ws:
            try:
                old_ws.close()
            except Exception:
                pass

        if self._reader_thread is None or not self._reader_thread.is_alive():
            self._reader_thread = threading.Thread(
                target=self._reader_loop, daemon=True, name="alma-ws-reader",
            )
            self._reader_thread.start()

        log.info("Alma WS connected: %s", ALMA_WS_URL)

    def send(self, message: dict):
        with self._lock:
            ws = self._ws
            if ws is None or self._state != "CONNECTED":
                raise ConnectionError("Alma WS not connected")
        with self._send_lock:
            try:
                ws.send(json.dumps(message))
            except Exception as e:
                self._handle_disconnect()
                raise ConnectionError(f"Alma WS send failed: {e}") from e

    def register_run(self, thread_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._pending[thread_id] = q
        return q

    def unregister_run(self, thread_id: str):
        with self._lock:
            self._pending.pop(thread_id, None)

    # ── background reader ──

    def _reader_loop(self):
        import websocket as _ws_mod

        while not self._stop.is_set():
            with self._lock:
                ws = self._ws
            if ws is None:
                time.sleep(0.1)
                continue
            try:
                ws.settimeout(1.0)
                data = ws.recv()
                if not data:
                    continue
                event = json.loads(data)
                self._dispatch(event)
            except _ws_mod.WebSocketTimeoutException:
                continue
            except _ws_mod.WebSocketConnectionClosedException:
                if not self._stop.is_set():
                    log.warning("Alma WS closed by remote")
                    self._handle_disconnect()
                break
            except Exception as e:
                if self._stop.is_set():
                    break
                log.warning("Alma WS reader error: %s", e)
                self._handle_disconnect()
                break

    def _dispatch(self, event: dict):
        thread_id = (event.get("data") or {}).get("threadId") or event.get("threadId")
        if not thread_id:
            return
        with self._lock:
            q = self._pending.get(thread_id)
        if q:
            q.put(event)

    def _handle_disconnect(self):
        with self._lock:
            self._state = "DISCONNECTED"
            old_ws = self._ws
            self._ws = None
            pending = dict(self._pending)

        if old_ws:
            try:
                old_ws.close()
            except Exception:
                pass

        sentinel = {"type": "_ws_disconnect", "error": "Alma WS disconnected"}
        for q in pending.values():
            q.put(sentinel)
        if pending:
            log.warning("Alma WS disconnected; %d in-flight runs failed",
                        len(pending))

    def close(self):
        self._stop.set()
        with self._lock:
            ws = self._ws
            self._ws = None
            self._state = "DISCONNECTED"
        if ws:
            try:
                ws.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# AlmaRunner
# ---------------------------------------------------------------------------

class AlmaRunner(BaseRunner):
    """Alma desktop app runner via local WebSocket API."""

    ALWAYS_STREAMING: ClassVar[bool] = True

    _ws_mgr: Optional[AlmaWSManager] = None
    _ws_mgr_lock = threading.Lock()

    def __init__(self, model: Optional[str], workspace: str, timeout: int,
                 bot_id: str,
                 max_budget_usd: Optional[float] = None,
                 extra_system_prompts: Optional[list[str]] = None,
                 extra_cli_args: Optional[list[str]] = None,
                 fixed_env: Optional[dict[str, str]] = None,
                 safety_prompt_mode: str = "full",
                 setting_sources: Optional[str] = None):
        super().__init__(
            command=None,
            model=model,
            workspace=workspace,
            timeout=timeout,
            max_budget_usd=max_budget_usd,
            extra_system_prompts=extra_system_prompts,
            extra_cli_args=extra_cli_args,
            fixed_env=fixed_env,
            safety_prompt_mode=safety_prompt_mode,
            setting_sources=setting_sources,
        )
        self._bot_id = bot_id
        state_dir = Path.home() / ".feishu-bridge" / "state"
        self._thread_map = AlmaThreadMap(
            state_dir / f"alma-threads-{bot_id}.json"
        )

    @classmethod
    def _get_ws_mgr(cls) -> AlmaWSManager:
        with cls._ws_mgr_lock:
            if cls._ws_mgr is None:
                cls._ws_mgr = AlmaWSManager()
            return cls._ws_mgr

    # ── ABC stubs (AlmaRunner uses WS, not subprocess) ──

    def build_args(self, prompt, session_id, resume, streaming, *,
                   fork_session=False):
        raise NotImplementedError("AlmaRunner uses WS, not subprocess")

    def parse_streaming_line(self, event, state):
        raise NotImplementedError("AlmaRunner uses WS, not subprocess")

    def parse_blocking_output(self, stdout, session_id):
        raise NotImplementedError("AlmaRunner uses WS, not subprocess")

    # ── overrides ──

    def get_display_name(self) -> str:
        return "Alma"

    def supports_compact(self) -> bool:
        return True

    def supports_auto_compact(self) -> bool:
        return False

    def has_session(self, session_id: str) -> bool:
        return self._thread_map.get(session_id) is not None

    def wants_auth_file(self) -> bool:
        return True

    # ── thread mapping ──

    def _resolve_thread(self, session_key: str, *, force_new: bool = False) -> str:
        if not force_new:
            thread_id = self._thread_map.get(session_key)
            if thread_id:
                if _thread_exists(thread_id):
                    return thread_id
                log.warning("Alma thread %s gone; creating replacement",
                            thread_id)

        thread_id = _create_alma_thread()
        self._thread_map.put(session_key, thread_id)
        log.info("Created Alma thread %s for session %s",
                 thread_id, session_key[:24])
        return thread_id

    def clear_thread(self, session_key: str):
        self._thread_map.delete(session_key)

    # ── model resolution ──

    def _resolve_model(self) -> str:
        if not self.model:
            return "claude-subscription:claude-sonnet-4-20250514"
        if ":" in self.model:
            return self.model
        key = self.model.lower().replace("-", "").replace("_", "")
        for alias, full in _MODEL_MAP.items():
            if alias in key:
                return full
        return f"claude-subscription:{self.model}"

    # ── core run() ──

    def run(self, prompt: str, session_id: Optional[str] = None,
            resume: bool = False, tag: Optional[str] = None,
            on_output=None, on_tool_status=None, on_todo_update=None,
            on_agent_update=None, env_extra: Optional[dict] = None,
            fork_session: bool = False) -> dict:

        if fork_session:
            return {
                "result": "AlmaRunner 不支持 /btw（fork session）。"
                          "请使用 `/agent claude` 切回后使用。",
                "session_id": session_id,
                "is_error": True,
            }

        if len(prompt) > MAX_PROMPT_CHARS:
            log.warning("Prompt truncated: %d -> %d chars",
                        len(prompt), MAX_PROMPT_CHARS)
            prompt = prompt[:MAX_PROMPT_CHARS] + "\n\n...(message truncated)"

        session_key = tag or session_id or ""

        # /compact → Alma HTTP API
        if prompt.strip().startswith("/compact"):
            return self._handle_compact(session_key)

        if not _is_alma_running():
            return {
                "result": "Alma 未运行。请启动 Alma 桌面应用，"
                          "或使用 `/agent claude` 切回 Claude Code。",
                "session_id": tag or session_id,
                "is_error": True,
            }

        system_prompt = self._build_system_prompt()
        log.info("Alma: key=%s prompt=%d sys=%d chars",
                 session_key[:24], len(prompt), len(system_prompt))

        force_new = not resume
        try:
            thread_id = self._resolve_thread(session_key, force_new=force_new)
        except (ConnectionError, RuntimeError) as e:
            return {
                "result": f"Alma thread 创建失败: {e}",
                "session_id": tag or session_id,
                "is_error": True,
            }

        ws_mgr = self._get_ws_mgr()
        try:
            ws_mgr.ensure_connected()
        except ConnectionError as e:
            return {
                "result": f"Alma 连接失败: {e}",
                "session_id": tag or session_id,
                "is_error": True,
            }

        event_queue = ws_mgr.register_run(thread_id)
        try:
            ws_mgr.send({
                "type": "generate_response",
                "data": {
                    "threadId": thread_id,
                    "model": self._resolve_model(),
                    "userMessage": {
                        "role": "user",
                        "parts": [{"type": "text", "text": prompt}],
                    },
                    "ephemeralContext": system_prompt,
                    "source": "feishu",
                },
            })

            return self._consume_events(
                event_queue, thread_id, tag or session_id,
                on_output=on_output,
                on_tool_status=on_tool_status,
            )
        except ConnectionError as e:
            return {
                "result": f"Alma 通信中断: {e}",
                "session_id": tag or session_id,
                "is_error": True,
            }
        finally:
            ws_mgr.unregister_run(thread_id)

    def _consume_events(self, event_queue: queue.Queue, thread_id: str,
                        session_id: Optional[str], *,
                        on_output=None, on_tool_status=None) -> dict:
        accumulated = ""
        tool_calls: dict[str, int] = {}
        tool_status: list[dict] = []
        context_usage = None
        deadline = time.monotonic() + self.timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "result": f"Alma 响应超时（{self.timeout}s）。",
                    "session_id": session_id,
                    "is_error": True,
                }
            try:
                event = event_queue.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue

            etype = event.get("type", "")

            if etype == "_ws_disconnect":
                return {
                    "result": "Alma WebSocket 断开。请检查 Alma 是否仍在运行。",
                    "session_id": session_id,
                    "is_error": True,
                }

            if etype == "message_delta":
                data_obj = event.get("data") or {}
                deltas = data_obj.get("deltas") or []
                if not deltas:
                    delta = data_obj.get("delta") or {}
                    if delta:
                        deltas = [delta]

                for delta in deltas:
                    dt = delta.get("type", "")

                    if dt == "text_append":
                        accumulated += delta.get("text", "")
                        if on_output:
                            on_output(accumulated)

                    elif dt == "part_add":
                        part = delta.get("part") or {}
                        if part.get("type") == "tool-invocation":
                            tcid = part.get("toolCallId", "")
                            raw_name = part.get("toolName", "")
                            name = _TOOL_NAME_MAP.get(
                                raw_name.lower(), raw_name.title()
                            )
                            hint = _extract_hint_data(name, part.get("args") or {})
                            idx = len(tool_status)
                            tool_calls[tcid] = idx
                            tool_status.append({
                                "name": name,
                                "hint_data": hint,
                                "status": "running",
                            })
                            if on_tool_status:
                                on_tool_status(list(tool_status))

                    elif dt == "tool_output_set":
                        tcid = delta.get("toolCallId", "")
                        idx = tool_calls.get(tcid)
                        if idx is not None and idx < len(tool_status):
                            tool_status[idx]["status"] = "done"
                            if on_tool_status:
                                on_tool_status(list(tool_status))

            elif etype == "context_usage_update":
                d = event.get("data") or {}
                used = d.get("contextTokens", 0)
                total = d.get("contextWindow", 0)
                pct = (used / total * 100) if total > 0 else 0
                context_usage = {"used": used, "total": total, "percent": pct}
                log.info("Alma context: %d/%d (%.0f%%)", used, total, pct)

            elif etype == "generation_completed":
                log.info("Alma generation_completed: accumulated=%d chars, tools=%d",
                         len(accumulated), len(tool_status))
                result_text = accumulated
                if not result_text.strip() and tool_status:
                    # Generate a meaningful message for tool-only responses
                    tool_names = [t["name"] for t in tool_status]
                    if len(tool_names) == 1:
                        result_text = f"✓ 已执行 {tool_names[0]}"
                    else:
                        result_text = f"✓ 已执行 {len(tool_names)} 个工具: {', '.join(tool_names)}"
                
                return {
                    "result": result_text,
                    "session_id": session_id,
                    "is_error": False,
                    "total_cost_usd": None,
                    "context_usage": context_usage,
                }

            elif etype == "generation_error":
                err = (event.get("data") or {}).get("error", "Unknown error")
                return {
                    "result": f"Alma 生成错误: {err}",
                    "session_id": session_id,
                    "is_error": True,
                }

    def _handle_compact(self, session_key: str) -> dict:
        thread_id = self._thread_map.get(session_key)
        if not thread_id:
            return {
                "result": "当前没有活跃的 Alma 会话。",
                "session_id": session_key,
                "is_error": True,
            }
        try:
            _alma_http("POST", f"/api/threads/{thread_id}/compact")
            return {
                "result": "Alma 上下文已压缩。",
                "session_id": session_key,
                "is_error": False,
            }
        except (ConnectionError, RuntimeError) as e:
            return {
                "result": f"Alma compact 失败: {e}",
                "session_id": session_key,
                "is_error": True,
            }

    # ── preflight ──

    @staticmethod
    def preflight_check() -> tuple[bool, str]:
        if not _is_alma_running():
            return False, "Alma 未运行。请先启动 Alma 桌面应用。"
        try:
            resp = _alma_http("GET", "/api/settings")
            feishu_cfg = resp.get("feishu") or {}
            if feishu_cfg.get("enabled", False):
                return False, (
                    "Alma 内置 Feishu bridge 仍启用（feishu.enabled=true），"
                    "两个 bot 会同时响应。"
                    "请在 Alma 设置中禁用后重试。"
                )
        except (ConnectionError, RuntimeError):
            log.warning("Cannot verify Alma feishu settings; proceeding")
        return True, ""
