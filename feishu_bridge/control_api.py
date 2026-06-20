"""Unix socket JSON-RPC Control API for bridge runtime.

Design decisions:
1. Reuses the FeishuBot instance — direct access to in-memory state.
2. Calls existing switch_provider / set_model / switch_agent — no duplication.
3. socketserver.ThreadingUnixStreamServer in a daemon thread.
4. Each component uses its own lock for state snapshots.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket as _socket
import socketserver
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from feishu_bridge.main import FeishuBot

log = logging.getLogger("feishu-bridge")

API_VERSION = 1

_CAPABILITIES = (
    "logs", "quota", "sessions", "provider", "model", "agent",
    "stop", "tasks", "call_service",
)


# ── helpers ────────────────────────────────────────────────────────────

def generate_token(token_path: Path) -> str:
    """Generate a random control token and persist it (0600)."""
    token = secrets.token_urlsafe(32)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(token)
    except BaseException:
        try:
            os.unlink(str(token_path))
        except OSError:
            pass
        raise
    return token


def load_or_create_token(token_path: Path) -> str:
    """Load existing token, or generate a new one."""
    if token_path.exists():
        token = token_path.read_text().strip()
        if token:
            return token
    return generate_token(token_path)


# ── method dispatch ────────────────────────────────────────────────────

def _build_dispatcher(bot: FeishuBot, log_buffer) -> dict[str, Any]:
    """Build method name → handler mapping.

    Each handler is ``(params: dict) -> dict``.  Errors are raised as
    ``_RPCError``.
    """

    from feishu_bridge import __version__
    from feishu_bridge.main import (
        _RUNNER_CLASSES,
        _normalize_provider_profiles,
        resolve_effective_agent_command,
        resolve_provider_name,
    )

    # -- read methods -------------------------------------------------------

    def _health(_params: dict) -> dict:
        return {"ok": True}

    def _status(_params: dict) -> dict:
        # agent info
        model_display, is_override = bot.get_model_status()
        resolved_cmd, _configured = resolve_effective_agent_command(
            bot.agent_config, bot.agent_config["type"],
        )

        # sessions — snapshot under SessionMap._lock
        sm = bot.session_map
        with sm._lock:
            session_keys = [k for k in sm._data if k != sm._AGENT_TYPE_KEY]
            active_count = len(session_keys)

        # queue — snapshot under ChatTaskQueue._lock
        cq = bot._chat_queue
        with cq._lock:
            pending_total = sum(len(v) for v in cq._pending.values())
            active_sessions = len(cq._active)

        # quota
        snap = bot._quota_poller.snapshot
        quota_dict: dict[str, Any] = {"available": snap.available, "stale": snap.stale}
        if snap.available:
            windows = {}
            for wk, wv in snap.windows.items():
                windows[wk] = {
                    "utilization": wv.utilization,
                    "resets_at": wv.resets_at,
                }
            quota_dict["windows"] = windows
            quota_dict["extra_usage_enabled"] = snap.extra_usage_enabled

        # uptime
        start_ts = getattr(bot, "_start_time", None)
        uptime = time.time() - start_ts if start_ts else 0

        # providers / agents lists
        provider_names = sorted(_normalize_provider_profiles(bot.agent_config).keys())
        agent_types = sorted(_RUNNER_CLASSES.keys())

        return {
            "api_version": API_VERSION,
            "capabilities": list(_CAPABILITIES),
            "version": __version__,
            "uptime_seconds": round(uptime, 1),
            "agent": {
                "type": bot.agent_config.get("type"),
                "provider": resolve_provider_name(bot.agent_config),
                "model": model_display,
                "model_override": model_display if is_override else None,
                "command": resolved_cmd,
            },
            "bot": {
                "name": bot.bot_id,
                "workspace": str(bot.workspace),
            },
            "sessions": {
                "active_count": active_count,
                "keys": session_keys,
            },
            "queue": {
                "pending_total": pending_total,
                "active_sessions": active_sessions,
            },
            "quota": quota_dict,
            "providers": provider_names,
            "agents": agent_types,
        }

    def _config(_params: dict) -> dict:
        """Return sanitised config (secrets masked)."""
        import copy

        bot_cfg = copy.deepcopy(bot.bot_config)
        for key in ("app_secret",):
            if key in bot_cfg:
                bot_cfg[key] = "***"

        agent_cfg = copy.deepcopy(bot.agent_config)
        # mask secrets inside provider profiles
        for _pname, profile in agent_cfg.get("providers", {}).items():
            for k in list(profile):
                if "secret" in k.lower() or "key" in k.lower() or "token" in k.lower():
                    profile[k] = "***"

        return {"bot": bot_cfg, "agent": agent_cfg}

    def _sessions(_params: dict) -> dict:
        sm = bot.session_map
        with sm._lock:
            keys = [k for k in sm._data if k != sm._AGENT_TYPE_KEY]
            sessions = [
                {"session_key": k, "session_id": sm._data[k]}
                for k in keys
            ]
        return {"sessions": sessions}

    def _quota(_params: dict) -> dict:
        snap = bot._quota_poller.snapshot
        result: dict[str, Any] = {
            "available": snap.available,
            "stale": snap.stale,
            "timestamp": snap.timestamp,
        }
        if snap.available:
            windows = {}
            for wk, wv in snap.windows.items():
                windows[wk] = {
                    "utilization": wv.utilization,
                    "resets_at": wv.resets_at,
                }
            result["windows"] = windows
            result["extra_usage_enabled"] = snap.extra_usage_enabled
        if snap.error:
            result["error"] = snap.error
        return result

    def _logs(params: dict) -> dict:
        n = params.get("n", 200)
        level = params.get("level", "INFO")
        if log_buffer is None:
            return {"entries": []}
        entries = log_buffer.recent(n=n, level=level)
        return {"entries": entries}

    def _tasks(_params: dict) -> dict:
        sup = getattr(bot, "_bg_supervisor", None)
        if sup is None:
            return {"tasks": []}
        try:
            from feishu_bridge.bg_supervisor import BgTaskRepo
            repo = BgTaskRepo(sup._db_path)
            rows = repo.list_tasks(limit=50)
            tasks = [
                {
                    "id": str(r["id"]),
                    "state": r["state"],
                    "description": r.get("description", ""),
                    "created": r.get("created_at", ""),
                }
                for r in rows
            ]
            return {"tasks": tasks}
        except Exception as exc:
            log.debug("tasks RPC failed: %s", exc)
            return {"tasks": [], "error": str(exc)}

    # -- write methods (hot update) -----------------------------------------

    def _set_provider(params: dict) -> dict:
        name = params.get("name")
        if not name:
            raise _RPCError(400, "missing 'name' parameter")
        ok, msg = bot.switch_provider(name)
        if not ok:
            raise _RPCError(400, msg)
        return {"ok": True, "message": msg}

    def _set_model(params: dict) -> dict:
        name = params.get("name")
        if not name:
            raise _RPCError(400, "missing 'name' parameter")
        effective, is_cleared = bot.set_model(name)
        return {"ok": True, "model": effective, "cleared": is_cleared}

    def _set_agent(params: dict) -> dict:
        agent_type = params.get("type")
        if not agent_type:
            raise _RPCError(400, "missing 'type' parameter")
        ok, msg, cmd = bot.switch_agent(agent_type)
        if not ok:
            raise _RPCError(400, msg)
        return {"ok": True, "message": msg, "command": cmd}

    # -- service methods ----------------------------------------------------

    def _call_service(params: dict) -> dict:
        chat_id = params.get("chat_id", "")
        sender_id = params.get("sender_id", "")
        service = params.get("service", "")
        action = params.get("action", "")
        args = params.get("args", {})
        if not isinstance(args, dict):
            args = {}

        # 权限校验：chat_id 必须在活跃会话中
        active_chats: set[str] = set()
        with bot.session_map._lock:
            for key in bot.session_map._data:
                if key == bot.session_map._AGENT_TYPE_KEY:
                    continue
                parts = key.split(":", 2)
                if len(parts) >= 2:
                    active_chats.add(parts[1])
        if chat_id not in active_chats:
            return {"ok": False, "error": "unauthorized",
                    "message": "会话未激活或已过期"}

        # 路由到对应 wrapper
        wrapper_map = {
            "tasks": bot.feishu_tasks,
            "sheets": bot.feishu_sheets,
            "docs": bot.feishu_docs,
            "bitable": bot.feishu_bitable,
            "calendar": bot.feishu_calendar,
            "mail": bot.feishu_mail,
        }
        wrapper = wrapper_map.get(service)
        if wrapper is None:
            return {"ok": False, "error": "unknown_service",
                    "message": f"不支持的服务: {service}，"
                    f"可用: {', '.join(sorted(wrapper_map))}"}

        return wrapper.dispatch(action, chat_id, sender_id, **args)

    # -- session-level methods ----------------------------------------------

    def _stop_session(params: dict) -> dict:
        session_key = params.get("session_key")
        if not session_key:
            raise _RPCError(400, "missing 'session_key' parameter")
        # Validate format: must be "bot_id:chat_id:thread_id"
        parts = session_key.split(":")
        if len(parts) != 3:
            raise _RPCError(400, "session_key must be 'bot_id:chat_id:thread_id'")
        cancelled = bot.runner.cancel(session_key)
        return {"ok": True, "cancelled": cancelled}
    # -- shutdown -----------------------------------------------------------

    def _shutdown(_params: dict) -> dict:
        """Initiate graceful shutdown.

        We respond first, then schedule the actual shutdown in a
        background thread so the client gets the response.
        """

        def _do_shutdown():
            time.sleep(0.3)  # let the response flush
            log.info("Control API shutdown requested — exiting")
            # Clean up our own socket
            try:
                _control_api = getattr(bot, "_control_api", None)
                if _control_api:
                    _control_api.stop()
            except Exception:
                pass
            # Stop bg supervisor
            sup = getattr(bot, "_bg_supervisor", None)
            if sup is not None:
                try:
                    sup.stop()
                except Exception:
                    pass
            # Shutdown runner (e.g. OmpRpcRunner terminates RPC subprocesses)
            runner = getattr(bot, "runner", None)
            if runner is not None and hasattr(runner, "shutdown"):
                try:
                    runner.shutdown()
                except Exception:
                    pass
            # Exit — launchd KeepAlive will restart
            os._exit(0)

        t = threading.Thread(target=_do_shutdown, daemon=True, name="shutdown")
        t.start()
        return {"ok": True, "message": "shutting down"}

    return {
        "health": _health,
        "status": _status,
        "config": _config,
        "sessions": _sessions,
        "quota": _quota,
        "logs": _logs,
        "tasks": _tasks,
        "call_service": _call_service,
        "set_provider": _set_provider,
        "set_model": _set_model,
        "set_agent": _set_agent,
        "stop_session": _stop_session,
        "shutdown": _shutdown,
    }


# ── RPC error ──────────────────────────────────────────────────────────

class _RPCError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ── socketserver implementation ────────────────────────────────────────

class _ControlHandler(socketserver.StreamRequestHandler):
    """Handle one connection: read lines, dispatch, respond."""

    def handle(self) -> None:
        for raw_line in self.rfile:
            line = raw_line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_error(None, 400, "invalid JSON")
                continue

            if not isinstance(req, dict):
                self._send_error(None, 400, "request must be a JSON object")
                continue

            req_id = req.get("id")
            token = req.get("token")
            method = req.get("method")
            params = req.get("params", {})
            if not isinstance(params, dict):
                self._send_error(req_id, 400, "'params' must be a JSON object")
                continue

            # Auth check
            if token != self.server.token:
                self._send_error(req_id, 401, "invalid token")
                continue

            handler = self.server.dispatch.get(method)
            if handler is None:
                self._send_error(req_id, 404, f"unknown method: {method}")
                continue

            try:
                result = handler(params)
            except _RPCError as e:
                self._send_error(req_id, e.code, e.message)
                continue
            except Exception as exc:
                log.warning("Control API error in %s: %s", method, exc,
                            exc_info=True)
                self._send_error(req_id, 500, str(exc))
                continue

            resp = {
                "result": result,
                "id": req_id,
                "api_version": API_VERSION,
                "capabilities": list(_CAPABILITIES),
            }
            self._send(resp)

    def _send(self, obj: dict) -> None:
        try:
            data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            self.wfile.write(data.encode("utf-8") + b"\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_error(self, req_id, code: int, message: str) -> None:
        self._send({
            "error": {"code": code, "message": message},
            "id": req_id,
        })


class _ControlServer(socketserver.ThreadingUnixStreamServer):
    """ThreadingUnixStreamServer with bot reference and token."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_class, *,
                 bot: FeishuBot, token: str, log_buffer):
        self.bot = bot
        self.token = token
        self.dispatch = _build_dispatcher(bot, log_buffer)
        super().__init__(server_address, handler_class)


# ── public API ─────────────────────────────────────────────────────────

class ControlAPI:
    """Thread-based Unix socket JSON-RPC server for bridge control.

    Lifecycle::

        api = ControlAPI(bot, sock_path, token_path, log_buffer)
        api.start()   # non-blocking (daemon thread)
        ...
        api.stop()    # cleanup
    """

    def __init__(self, bot: FeishuBot, sock_path: Path,
                 token_path: Path, log_buffer=None):
        self._bot = bot
        self._sock_path = sock_path
        self._token_path = token_path
        self._log_buffer = log_buffer
        self._server: _ControlServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Bind socket and start serving in a daemon thread."""
        self._sock_path.parent.mkdir(parents=True, exist_ok=True)

        # Probe: is another instance listening?
        if self._sock_path.exists():
            probe = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            try:
                probe.connect(str(self._sock_path))
                probe.close()
                raise RuntimeError(
                    f"Another bridge instance is listening on {self._sock_path}"
                )
            except ConnectionRefusedError:
                self._sock_path.unlink()  # stale socket
            except FileNotFoundError:
                pass  # race: already removed
            finally:
                probe.close()

        token = load_or_create_token(self._token_path)

        self._server = _ControlServer(
            str(self._sock_path), _ControlHandler,
            bot=self._bot, token=token, log_buffer=self._log_buffer,
        )
        os.chmod(str(self._sock_path), 0o600)

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True, name="control-api",
        )
        self._thread.start()
        log.info("Control API listening on %s", self._sock_path)

    def stop(self) -> None:
        """Shutdown server and clean up socket file."""
        if self._server:
            self._server.shutdown()
        if self._sock_path.exists():
            self._sock_path.unlink(missing_ok=True)
