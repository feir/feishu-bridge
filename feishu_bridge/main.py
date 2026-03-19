#!/usr/bin/env python3
"""Feishu Bridge — Feishu <-> Claude Code CLI bridge.

Single-layer architecture: Feishu WebSocket -> this process -> claude -p.
CLAUDE.md, rules, skills, hooks all preserved (same claude binary, same cwd).

Usage:
    feishu-bridge --bot claude-code [--config path/to/config.json]
"""

import argparse
import json
import logging
import os
import queue
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import lark_oapi as lark

# Regex for detecting Feishu document/wiki/sheet/bitable URLs in messages.
# Captures (type, token) from URLs like https://xxx.feishu.cn/wiki/TOKEN
_FEISHU_URL_RE = re.compile(
    r'https?://[a-zA-Z0-9_-]+\.feishu\.cn/'
    r'(?P<type>wiki|docx?|sheets|base)'
    r'/(?P<token>[A-Za-z0-9_-]+)',
)

from feishu_bridge.parsers import download_image, fetch_quoted_message, parse_post_content
from feishu_bridge.commands import BridgeCommandHandler
from feishu_bridge.runtime import (
    ChatTaskQueue,
    ClaudeRunner,
    DEDUP_MAX,
    DEDUP_TTL,
    DEFAULT_TIMEOUT,
    MessageDedup,
    QUEUE_MAX,
    SessionMap,
    SessionQueueFull,
)
from feishu_bridge.ui import (
    ResponseHandle,
    remove_queued_reaction,
    remove_typing_indicator,
)
from feishu_bridge.worker import (
    format_task_detail_bridge as _bridge_worker_format_task_detail,
    process_message as _bridge_worker_process_message,
)
from feishu_bridge.api.client import FeishuAPIError
from feishu_bridge.api.tasks import FeishuTasks
from feishu_bridge.api.docs import FeishuDocs
from feishu_bridge.api.sheets import FeishuSheets
from feishu_bridge.api.bitable import FeishuBitable
from feishu_bridge.api.wiki import FeishuWiki
from feishu_bridge.api.comments import FeishuComments
from feishu_bridge.api.calendar import FeishuCalendar
from feishu_bridge.api.search import FeishuSearch

# ============================================================
# Constants
# ============================================================

SESSION_NOT_FOUND_SIGNATURES = [
    "session not found",
    "no such session",
    "could not find session",
    "session does not exist",
]

WORKER_COUNT = 4

log = logging.getLogger("feishu-bridge")


# ============================================================
# Config
# ============================================================





def load_config(config_path: str, bot_name: str) -> dict:
    """Load config JSON, substitute ${VAR} env vars, validate, return config."""
    raw = Path(config_path).read_text()

    # Substitute ${VAR} patterns
    def _sub(m):
        val = os.environ.get(m.group(1))
        return val if val is not None else m.group(0)

    resolved = re.sub(r'\$\{(\w+)\}', _sub, raw)

    # Fail-fast on unresolved placeholders
    remaining = re.findall(r'\$\{(\w+)\}', resolved)
    if remaining:
        log.error("Unresolved env vars in config: %s", remaining)
        sys.exit(1)

    config = json.loads(resolved)

    # Find the bot entry
    bot = None
    for b in config.get("bots", []):
        if b["name"] == bot_name:
            bot = b
            break

    if not bot:
        available = [b["name"] for b in config.get("bots", [])]
        log.error("Bot '%s' not found. Available: %s", bot_name, available)
        sys.exit(1)

    # Validate required bot fields
    for field in ("app_id", "app_secret", "workspace"):
        if not bot.get(field):
            log.error("Bot '%s' missing required field: %s", bot_name, field)
            sys.exit(1)

    # Parse allowed_users from CSV string -> list
    if isinstance(bot.get("allowed_users"), str):
        bot["allowed_users"] = [
            s.strip() for s in bot["allowed_users"].split(",") if s.strip()
        ]

    # Parse allowed_chats from CSV string -> list (if needed)
    if isinstance(bot.get("allowed_chats"), str):
        bot["allowed_chats"] = [
            s.strip() for s in bot["allowed_chats"].split(",") if s.strip()
        ]

    # Validate allowed_users
    users = bot.get("allowed_users", [])
    if not isinstance(users, list) or len(users) == 0:
        log.error("allowed_users must be a non-empty list, got: %s", users)
        sys.exit(1)
    if not all(isinstance(x, str) and x for x in users):
        log.error("allowed_users entries must be non-empty strings")
        sys.exit(1)

    # Validate claude command
    claude_cfg = config.get("claude", {})
    claude_cmd = claude_cfg.get("command", "claude")
    resolved_cmd = shutil.which(claude_cmd)
    if not resolved_cmd:
        log.error(
            "Claude command '%s' not found in PATH. "
            "Set absolute path in config or update PATH.", claude_cmd
        )
        sys.exit(1)
    claude_cfg["_resolved_command"] = resolved_cmd
    log.info("Claude CLI: %s", resolved_cmd)

    # Log config (mask secrets)
    masked = {**bot, "app_secret": "***"}
    log.info("Bot config: %s", json.dumps(masked, ensure_ascii=False))

    return {"bot": bot, "claude": claude_cfg, "dedup": config.get("dedup", {}),
            "todo_auto_drive": config.get("todo_auto_drive", True)}

# ============================================================
# Message Processing (Worker)
# ============================================================

def _format_task_detail_bridge(task: dict) -> str:
    return _bridge_worker_format_task_detail(task)


def process_message(item: dict, bot_config: dict, lark_client,
                    session_map: SessionMap, runner: ClaudeRunner,
                    feishu_tasks=None, feishu_docs=None, feishu_sheets=None):
    """Compatibility wrapper for the worker pipeline implementation."""
    return _bridge_worker_process_message(
        item=item,
        bot_config=bot_config,
        lark_client=lark_client,
        session_map=session_map,
        runner=runner,
        feishu_tasks=feishu_tasks,
        feishu_docs=feishu_docs,
        feishu_sheets=feishu_sheets,
        feishu_api_error_cls=FeishuAPIError,
        response_handle_cls=ResponseHandle,
        download_image_fn=download_image,
        fetch_quoted_message_fn=fetch_quoted_message,
        remove_typing_indicator_fn=remove_typing_indicator,
        session_not_found_signatures=SESSION_NOT_FOUND_SIGNATURES,
    )


# ============================================================
# FeishuBot
# ============================================================

class FeishuBot:
    """Feishu WebSocket bot with non-blocking message handling."""

    def __init__(self, config: dict):
        self.bot_config = config["bot"]
        self.claude_config = config["claude"]
        self.dedup_config = config.get("dedup", {})

        self.bot_id = self.bot_config["name"]
        self.app_id = self.bot_config["app_id"]
        self.app_secret = self.bot_config["app_secret"]
        self.workspace = self.bot_config["workspace"]
        self.allowed_users = self.bot_config.get("allowed_users", ["*"])
        self.allowed_chats = self.bot_config.get("allowed_chats", ["*"])
        self._todo_auto_drive = config.get("todo_auto_drive", True)
        self._session_cost: dict[str, dict] = {}  # sid -> last {usage, model_usage, total_cost_usd}

        # Components
        self.dedup = MessageDedup(
            ttl=self.dedup_config.get("ttl_seconds", DEDUP_TTL),
            max_entries=self.dedup_config.get("max_entries", DEDUP_MAX),
        )
        self.session_map = SessionMap(
            Path(self.workspace) / "state" / "feishu-bridge" / f"sessions-{self.bot_id}.json"
        )
        # Load CLI prompt for Feishu operations
        _cli_prompt_path = Path(__file__).resolve().parent / "feishu_cli_prompt.md"
        _extra_prompts = []
        if _cli_prompt_path.exists():
            _cli_text = _cli_prompt_path.read_text()
            _cli_abs = str(Path(__file__).resolve().parent / "feishu_cli.py")
            _cli_text = _cli_text.replace("python3 feishu_cli.py", f"python3 {_cli_abs}")
            _extra_prompts.append(_cli_text)

        self.runner = ClaudeRunner(
            command=self.claude_config["_resolved_command"],
            model=self.bot_config.get("model", "claude-opus-4-6"),
            workspace=self.workspace,
            timeout=self.claude_config.get("timeout_seconds", DEFAULT_TIMEOUT),
            max_budget_usd=self.claude_config.get("max_budget_usd"),
            extra_system_prompts=_extra_prompts,
        )
        self.command_handler = BridgeCommandHandler(self)

        # Work queue: ChatTaskQueue manages per-session FIFO,
        # _work_queue is the shared worker pool queue
        self._work_queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._chat_queue = ChatTaskQueue(self._work_queue)
        self._io_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="io")

        # Lark REST client (for sending messages)
        self.lark_client = lark.Client.builder() \
            .app_id(self.app_id) \
            .app_secret(self.app_secret) \
            .domain(lark.FEISHU_DOMAIN) \
            .log_level(lark.LogLevel.WARNING) \
            .build()

        # Feishu API services (with auto-auth via Device Flow OAuth)
        if _FEISHU_SERVICES_OK:
            _args = (self.app_id, self.app_secret, self.lark_client)
            self.feishu_tasks = FeishuTasks(*_args)
            self.feishu_docs = FeishuDocs(*_args)
            self.feishu_sheets = FeishuSheets(*_args)
            self.feishu_bitable = FeishuBitable(*_args)
            self.feishu_wiki = FeishuWiki(*_args)
            self.feishu_comments = FeishuComments(*_args)
            self.feishu_calendar = FeishuCalendar(*_args)
            self.feishu_search = FeishuSearch(*_args)
        else:
            self.feishu_tasks = None
            self.feishu_docs = None
            self.feishu_sheets = None
            self.feishu_bitable = None
            self.feishu_wiki = None
            self.feishu_comments = None
            self.feishu_calendar = None
            self.feishu_search = None
            log.warning("Feishu API services unavailable (missing dependencies)")

    def start(self):
        """Start worker threads and WebSocket connection (blocking)."""
        # Record startup time — used to skip replayed events after restart
        self._startup_ms = str(int(time.time() * 1000))
        log.info("Bridge startup timestamp: %s", self._startup_ms)

        # Start workers
        for i in range(WORKER_COUNT):
            t = threading.Thread(
                target=self._worker_loop, daemon=True, name=f"worker-{i}"
            )
            t.start()
        log.info("Started %d worker threads", WORKER_COUNT)

        # Build event handler (empty encrypt_key/token for WS mode)
        # Register reaction events with no-op handler to suppress SDK ERROR logs
        def _noop_reaction(data):
            pass
        handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._on_message) \
            .register_p2_im_message_reaction_created_v1(_noop_reaction) \
            .register_p2_im_message_reaction_deleted_v1(_noop_reaction) \
            .build()

        # Start WebSocket (blocking, auto-reconnect)
        ws_client = lark.ws.Client(
            app_id=self.app_id,
            app_secret=self.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
            domain=lark.FEISHU_DOMAIN,
            auto_reconnect=True,
        )

        log.info("Connecting to Feishu WebSocket (bot=%s)...", self.bot_id)
        ws_client.start()  # Blocks forever

    def _on_message(self, data):
        """Event handler — MUST return immediately, zero network I/O."""
        try:
            event = data.event
            msg = event.message
            sender = event.sender

            message_id = msg.message_id
            chat_id = msg.chat_id
            msg_type = msg.message_type
            thread_id = getattr(msg, 'thread_id', None) or None
            parent_id = getattr(msg, 'parent_id', None) or None
            sender_id = (sender.sender_id.open_id
                         if sender and sender.sender_id else None)

            # Dedup (in-memory only)
            if self.dedup.is_duplicate(message_id):
                return

            # Skip messages created before this bridge instance started
            # (prevents restart storm from replayed WebSocket events)
            msg_create_time = getattr(msg, "create_time", None) or "0"
            if msg_create_time < self._startup_ms:
                log.info("Skipping stale message %s (create_time=%s < startup=%s)",
                         message_id, msg_create_time, self._startup_ms)
                return

            # Permission: user (reject None sender early — system/bot messages)
            if not sender_id:
                log.debug("Rejected message with no sender_id (system/bot)")
                return
            if ("*" not in self.allowed_users
                    and sender_id not in self.allowed_users):
                log.debug("Unauthorized user: %s", sender_id)
                return

            # Permission: chat
            if ("*" not in self.allowed_chats
                    and chat_id not in self.allowed_chats):
                log.debug("Unauthorized chat: %s", chat_id)
                return

            # Lightweight parse (no network I/O)
            text = ""
            image_key = None

            try:
                content = json.loads(msg.content)
            except (json.JSONDecodeError, TypeError):
                content = {}

            _todo_task_id = None  # set by todo branch when auto_drive=True

            if msg_type == "text":
                text = content.get("text", "")
                # Strip @mentions
                mentions = getattr(msg, 'mentions', None) or []
                for mention in mentions:
                    mk = getattr(mention, 'key', '')
                    if mk:
                        text = text.replace(mk, '').strip()
                if not text:
                    return

            elif msg_type == "image":
                image_key = content.get("image_key")
                if not image_key:
                    return

            elif msg_type == "post":
                # Rich-text message — use shared parser
                text = parse_post_content(content)
                # Strip @mentions from text (defensive: _parse_post_content
                # skips 'at' tags so keys won't appear, but kept for safety)
                mentions = getattr(msg, 'mentions', None) or []
                for mention in mentions:
                    mk = getattr(mention, 'key', '')
                    if mk:
                        text = text.replace(mk, '').strip()
                if not text:
                    return

            elif msg_type == "todo":
                # Feishu task share — extract task info as context for Claude
                task_id = content.get("task_id", "")
                summary_obj = content.get("summary", {})
                # Extract plain text from rich-text content blocks
                todo_title = ""
                for para in summary_obj.get("content", []):
                    for seg in para:
                        if seg.get("tag") == "text":
                            todo_title += seg.get("text", "")
                todo_title = todo_title.strip() or "未命名任务"
                due = content.get("due_time", "")
                due_str = f"，截止时间: {due}" if due and due != "0000" else ""
                # Fallback text (used when auto_drive=False or API unavailable)
                # NOTE: Must NOT start with "/" to avoid matching bridge commands
                text = (f"[用户分享了一个飞书任务]\n"
                        f"任务: {todo_title}{due_str}\n"
                        f"task_id: {task_id}\n"
                        f"请查看这个任务并提供帮助。")
                # Config guard: auto-drive stores task_id for worker-thread API calls
                if self._todo_auto_drive and task_id:
                    _todo_task_id = task_id

            else:
                log.info("Unsupported msg_type: %s content=%s",
                         msg_type, msg.content[:200] if msg.content else "")
                return

            # Auto-detect Feishu doc/wiki/sheet/bitable URLs in message text
            _feishu_urls = _FEISHU_URL_RE.findall(text) if text else []
            # _feishu_urls is list of (type, token) tuples

            # Bridge commands (intercept before enqueueing to Claude)
            stripped = text.strip()
            cmd_lower = stripped.lower().split(None, 1)
            cmd = cmd_lower[0] if cmd_lower else ""
            # Preserve original case for arguments
            original_parts = stripped.split(None, 1)
            cmd_arg = original_parts[1] if len(original_parts) > 1 else ""

            bridge_cmd = None
            if cmd == "/restart":
                # Restart bridge — handled inline (not via queue) because
                # sys.exit() kills workers before they can process the item.
                log.info("Restart requested by user %s in chat %s",
                         sender_id, chat_id)
                try:
                    from feishu_bridge.ui import build_restart_card
                    handle = ResponseHandle(
                        self.lark_client, chat_id, thread_id, message_id,
                    )
                    msg_id = handle._send_card(build_restart_card())
                    if msg_id:
                        # Persist message_id so the new process can patch it
                        state_dir = Path(self.workspace) / "state" / "feishu-bridge"
                        state_dir.mkdir(parents=True, exist_ok=True)
                        restart_file = state_dir / f"restart-{self.bot_id}.json"
                        restart_file.write_text(json.dumps({
                            "message_id": msg_id,
                        }))
                except Exception:
                    log.exception("Failed to send restart confirmation")
                # Give message time to deliver, flush logs, then exit.
                # Non-zero exit triggers restart by both systemd (Restart=on-failure)
                # and launchd (KeepAlive.SuccessfulExit=false).
                def _deferred_exit():
                    logging.shutdown()
                    os._exit(1)
                threading.Timer(0.3, _deferred_exit).start()
                return

            elif cmd in ("/new", "/reset", "/clear"):
                bridge_cmd = "new"
                # Also cancel any running task for this chat
                key = (self.bot_id, chat_id, thread_id)
                tag = SessionMap._key_str(key)
                self.runner.cancel(tag)
                # Session delete deferred to worker under chat lock
            elif cmd in ("/stop", "/cancel"):
                # Kill active process immediately (non-blocking, safe here)
                key = (self.bot_id, chat_id, thread_id)
                tag = SessionMap._key_str(key)
                cancelled = self.runner.cancel(tag)
                # /stop all: also drain pending queue and cleanup ⏳ reactions
                stop_all = cmd_arg.strip().lower() == "all"
                drained_count = 0
                if stop_all:
                    drained = self._chat_queue.drain(tag)
                    drained_count = len(drained)
                    if drained:
                        def _cleanup_drained(items, client):
                            for d in items:
                                rid = d.get("_queued_reaction_id")
                                mid = d.get("message_id")
                                if rid and mid:
                                    remove_queued_reaction(client, mid, rid)
                        self._io_executor.submit(
                            _cleanup_drained, drained, self.lark_client)
                bridge_cmd = "stop"
                cmd_arg = f"{1 if cancelled else 0}|{drained_count}"
            elif cmd == "/help":
                bridge_cmd = "help"
            elif cmd == "/compact":
                bridge_cmd = "compact"
            elif cmd == "/model":
                bridge_cmd = "model"
            elif cmd == "/cost":
                bridge_cmd = "cost"
            elif cmd == "/feishu-tasks":
                bridge_cmd = "feishu-tasks"
            elif cmd == "/feishu-doc":
                bridge_cmd = "feishu-doc"
            elif cmd == "/feishu-sheet":
                bridge_cmd = "feishu-sheet"
            elif cmd == "/feishu-bitable":
                bridge_cmd = "feishu-bitable"

            if bridge_cmd:
                bc_item = {
                    "_bridge_command": bridge_cmd,
                    "_cmd_arg": cmd_arg,
                    "bot_id": self.bot_id,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "message_id": message_id,
                    "sender_id": sender_id,
                    "_queued_reaction_id": None,
                }
                # Heavy commands (/compact, /new, /reset, /clear, /cost)
                # serialize with normal messages via ChatTaskQueue.
                # Light commands (/help, /stop, /cancel, /status, /model,
                # /feishu-*) go directly to work queue.
                _heavy_cmds = {"new", "compact", "cost"}
                if bridge_cmd in _heavy_cmds:
                    bc_key = SessionMap._key_str(
                        (self.bot_id, chat_id, thread_id))
                    bc_item["_queue_key"] = bc_key
                    try:
                        status = self._chat_queue.enqueue(bc_key, bc_item)
                    except SessionQueueFull:
                        self._io_executor.submit(
                            self.command_handler.reply_queue_full,
                            chat_id, thread_id, message_id)
                        return
                    if status == 'queued':
                        self._io_executor.submit(
                            self.command_handler.add_queued_reaction_to_item,
                            bc_item, message_id)
                else:
                    try:
                        self._work_queue.put_nowait(bc_item)
                    except queue.Full:
                        log.warning("Queue full, dropping bridge cmd: %s",
                                    bridge_cmd)
                return

            # Enqueue via ChatTaskQueue (zero I/O, non-blocking)
            item = {
                "bot_id": self.bot_id,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "parent_id": parent_id,
                "message_id": message_id,
                "sender_id": sender_id,
                "text": text,
                "image_key": image_key,
                "_queued_reaction_id": None,
                "_todo_task_id": _todo_task_id,
                "_feishu_urls": _feishu_urls,
                "_cost_store": self._session_cost,
            }
            msg_key = SessionMap._key_str(
                (self.bot_id, chat_id, thread_id))
            item["_queue_key"] = msg_key

            try:
                status = self._chat_queue.enqueue(msg_key, item)
            except SessionQueueFull:
                self._io_executor.submit(
                    self.command_handler.reply_queue_full,
                    chat_id, thread_id, message_id)
                return

            if status == 'queued':
                # Add ⏳ reaction in background (zero I/O on event thread)
                self._io_executor.submit(
                    self.command_handler.add_queued_reaction_to_item,
                    item, message_id)

        except Exception:
            log.exception("on_message error")

    def _handle_bridge_command(self, item: dict):
        self._command_handler().handle_bridge_command(item)

    def _command_handler(self) -> BridgeCommandHandler:
        handler = getattr(self, "command_handler", None)
        if handler is None:
            handler = BridgeCommandHandler(self)
            self.command_handler = handler
        return handler

    def _dispatch_task_command(self, arg: str, chat_id: str, sender_id: str) -> str:
        return self._command_handler().dispatch_task_command(arg, chat_id, sender_id)

    def _task_list(self, rest: str, chat_id: str, sender_id: str) -> str:
        return self._command_handler()._task_list(rest, chat_id, sender_id)

    def _task_get(self, rest: str, chat_id: str, sender_id: str) -> str:
        return self._command_handler()._task_get(rest, chat_id, sender_id)

    def _task_subtasks(self, rest: str, chat_id: str, sender_id: str) -> str:
        return self._command_handler()._task_subtasks(rest, chat_id, sender_id)

    def _task_add_subtask(self, rest: str, chat_id: str, sender_id: str) -> str:
        return self._command_handler()._task_add_subtask(rest, chat_id, sender_id)

    def _task_complete(self, rest: str, chat_id: str, sender_id: str) -> str:
        return self._command_handler()._task_complete(rest, chat_id, sender_id)

    def _task_help(self) -> str:
        return self._command_handler()._task_help()

    def _handle_feishu_service(self, item: dict, handle, service: str):
        return self._command_handler()._handle_feishu_service(item, handle, service)

    def _dispatch_feishu_service(self, service: str, arg: str,
                                 chat_id: str, sender_id: str) -> str:
        return self._command_handler().dispatch_feishu_service(
            service, arg, chat_id, sender_id)

    @staticmethod
    def _feishu_service_help(service: str) -> str:
        return BridgeCommandHandler.feishu_service_help(service)

    def _add_queued_reaction_to_item(self, item: dict, message_id: str):
        self._command_handler().add_queued_reaction_to_item(item, message_id)

    def _reply_queue_full(self, chat_id: str, thread_id, message_id: str):
        self._command_handler().reply_queue_full(chat_id, thread_id, message_id)

    def _worker_loop(self):
        """Consumer loop — pulls from work queue and processes.

        ChatTaskQueue guarantees at most one item per session in flight.
        on_complete() in finally block submits next pending or marks idle.
        """
        while True:
            handle = None
            key = None
            try:
                item = self._work_queue.get()
                key = item.get("_queue_key")

                # Remove ⏳ reaction in background (non-blocking)
                queued_reaction = item.pop("_queued_reaction_id", None)
                if queued_reaction and item.get("message_id"):
                    self._io_executor.submit(
                        remove_queued_reaction,
                        self.lark_client, item["message_id"], queued_reaction)

                if item.get("_bridge_command"):
                    self._handle_bridge_command(item)
                else:
                    handle = process_message(
                        item, self.bot_config, self.lark_client,
                        self.session_map, self.runner,
                        feishu_tasks=getattr(self, 'feishu_tasks', None),
                        feishu_docs=getattr(self, 'feishu_docs', None),
                        feishu_sheets=getattr(self, 'feishu_sheets', None),
                    )
            except Exception:
                log.exception("Worker loop error")
            finally:
                # Cancel fallback timer (no-op if already fired or cancelled)
                if handle and getattr(handle, '_card_fallback_timer', None):
                    handle._card_fallback_timer.cancel()
                    handle._card_fallback_timer = None
                # Ensure typing indicator is always removed
                if handle and getattr(handle, '_typing_reaction_id', None) \
                        and handle.source_message_id:
                    remove_typing_indicator(
                        handle.client, handle.source_message_id,
                        handle._typing_reaction_id)
                    handle._typing_reaction_id = None
                # Submit next pending item or mark session idle
                if key:
                    self._chat_queue.on_complete(key)
                self._work_queue.task_done()


# ============================================================
# Startup Validation
# ============================================================

def validate_feishu_token(client) -> bool:
    """Validate Feishu app credentials with a lightweight API call."""
    try:
        from lark_oapi.api.im.v1 import ListChatRequest
        req = ListChatRequest.builder().page_size(1).build()
        resp = client.im.v1.chat.list(req)
        if resp.success():
            log.info("Feishu credentials validated")
            return True
        log.error("Feishu validation failed: code=%s msg=%s", resp.code, resp.msg)
        if "permission" in (resp.msg or "").lower():
            log.error(
                "Check required scopes: im:message, im:message:send_as_bot, "
                "im:message:patch, im:resource"
            )
        return False
    except Exception:
        log.exception("Feishu validation error")
        return False




def fetch_bot_display_name(client) -> str:
    """Fetch bot display name from Feishu API (GET /open-apis/bot/v3/info/).

    Returns the app_name registered in Feishu Open Platform,
    or 'Claude Code' as fallback on any failure.
    """
    fallback = 'Claude Code'
    try:
        req = lark.BaseRequest()
        req.http_method = lark.HttpMethod.GET
        req.uri = '/open-apis/bot/v3/info/'
        req.token_types = {lark.AccessTokenType.TENANT}
        resp = client.request(req)
        if resp.code == 0:
            bot_info = json.loads(resp.raw.content).get('bot', {})
            name = bot_info.get('app_name', '').strip()
            if name:
                log.info('Bot display name from API: %s', name)
                return name
        log.warning('fetch_bot_display_name: code=%s, using fallback', resp.code)
    except Exception:
        log.warning('Failed to fetch bot display name, using fallback', exc_info=True)
    return fallback

def _notify_restart_complete(bot):
    """Patch the pre-restart card to show completion, if one exists."""
    state_dir = Path(bot.workspace) / "state" / "feishu-bridge"
    restart_file = state_dir / f"restart-{bot.bot_id}.json"
    if not restart_file.exists():
        return
    try:
        data = json.loads(restart_file.read_text())
        mid = data.get("message_id")
        if mid:
            from feishu_bridge.ui import build_restart_complete_card
            from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
            card = build_restart_complete_card()
            card_json = json.dumps(card, ensure_ascii=False)
            body = PatchMessageRequestBody.builder() \
                .content(card_json).build()
            req = PatchMessageRequest.builder() \
                .message_id(mid).request_body(body).build()
            resp = bot.lark_client.im.v1.message.patch(req)
            if resp.success():
                log.info("Restart-complete card patched: %s", mid)
            else:
                log.warning("Restart-complete patch failed: code=%s msg=%s",
                            resp.code, resp.msg)
    except Exception:
        log.warning("Failed to notify restart completion", exc_info=True)
    finally:
        try:
            restart_file.unlink(missing_ok=True)
        except Exception:
            pass


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Feishu <-> Claude Code CLI bridge"
    )
    parser.add_argument("--bot", required=True, help="Bot name from config")
    parser.add_argument(
        "--config",
        default=str(Path("~/.claude/scripts/feishu_bridge_config.json").expanduser()),
        help="Config file path",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    # Logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Load config
    config = load_config(args.config, args.bot)

    # Create bot and run startup tasks in parallel
    bot = FeishuBot(config)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from feishu_bridge.ui import set_bot_display_name

    def _startup_validate():
        if not validate_feishu_token(bot.lark_client):
            log.warning("Feishu token validation failed (may need im:chat:readonly scope). "
                        "Continuing anyway — message delivery will reveal actual issues.")

    def _startup_restart_card():
        _notify_restart_complete(bot)

    # Fetch display name first (fast) to avoid race with restart card
    try:
        name = fetch_bot_display_name(bot.lark_client)
        set_bot_display_name(name)
    except Exception as e:
        log.warning("Failed to fetch bot display name: %s", e)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="startup") as pool:
        futures = [pool.submit(fn) for fn in (
            _startup_validate, _startup_restart_card)]
        for f in as_completed(futures):
            exc = f.exception()
            if exc:
                log.warning("Startup task failed: %s", exc)

    # Start (blocking)
    log.info("=== Feishu Bridge starting (bot=%s) ===", args.bot)
    try:
        bot.start()
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    main()
