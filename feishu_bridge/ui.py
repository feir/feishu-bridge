"""Feishu UI delivery helpers for Feishu bridge."""

import json
import logging
import re
import threading
import time
import uuid
from typing import Optional

from lark_oapi.api.cardkit.v1 import (
    CreateCardRequest,
    CreateCardRequestBody,
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    SettingsCardRequest,
    SettingsCardRequestBody,
    UpdateCardRequest,
    UpdateCardRequestBody,
)
from lark_oapi.api.cardkit.v1.model import Card
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

from feishu_bridge.runtime import ClaudeRunner

log = logging.getLogger("feishu-bridge")

MESSAGE_TERMINAL_CODES = {230011, 231003}

_unavailable_messages: dict[str, dict] = {}
_UNAVAILABLE_TTL = 30 * 60
_UNAVAILABLE_MAX_SIZE = 512

MAX_DIV_CHARS = 10_000
MAX_CARD_PAYLOAD_BYTES = 28 * 1024
COMPACT_MAX_CHARS = 4_000

CARDKIT_ELEMENT_ID = "streaming_output"
CARDKIT_THROTTLE_MS = 100
PATCH_THROTTLE_MS = 1500
GAP_THRESHOLD_MS = 2000
BATCH_AFTER_GAP_MS = 300

# Bot display name (fetched from Feishu API at startup)
_bot_display_name = "Claude Code"


def set_bot_display_name(name: str):
    global _bot_display_name
    _bot_display_name = name


def build_restart_card(message: str = "") -> dict:
    """Blue card shown when bridge is about to restart."""
    text = message or "正在重启，约 10 秒后恢复..."
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": f"⏳ {_bot_display_name}", "tag": "plain_text"},
            "template": "blue",
        },
        "elements": [{
            "tag": "div",
            "text": {"content": text, "tag": "lark_md"},
        }],
    }


def build_restart_complete_card() -> dict:
    """Green card patched after bridge restarts successfully."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": f"✅ {_bot_display_name}", "tag": "plain_text"},
            "template": "green",
        },
        "elements": [{
            "tag": "div",
            "text": {"content": "重启完成", "tag": "lark_md"},
        }],
    }


def _prune_unavailable_cache():
    now = time.monotonic()
    expired = [mid for mid, state in list(_unavailable_messages.items())
               if now - state["ts"] > _UNAVAILABLE_TTL]
    for mid in expired:
        _unavailable_messages.pop(mid, None)


def mark_message_unavailable(message_id: str, code: int):
    if len(_unavailable_messages) >= _UNAVAILABLE_MAX_SIZE:
        _prune_unavailable_cache()
    _unavailable_messages[message_id] = {"code": code, "ts": time.monotonic()}
    log.info("Message marked unavailable: %s code=%d", message_id, code)


def is_message_unavailable(message_id: str) -> bool:
    state = _unavailable_messages.get(message_id)
    if not state:
        return False
    if time.monotonic() - state["ts"] > _UNAVAILABLE_TTL:
        _unavailable_messages.pop(message_id, None)
        return False
    return True


def _check_terminal_code(resp, message_id: str) -> bool:
    if resp and not resp.success() and resp.code in MESSAGE_TERMINAL_CODES:
        mark_message_unavailable(message_id, resp.code)
        return True
    return False


def add_queued_reaction(lark_client, message_id: str) -> str | None:
    """Add ⏳ emoji reaction to user's message."""
    if is_message_unavailable(message_id):
        return None
    try:
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest, CreateMessageReactionRequestBody,
        )
        from lark_oapi.api.im.v1.model.emoji import Emoji

        body = CreateMessageReactionRequestBody.builder() \
            .reaction_type(Emoji.builder().emoji_type("OneSecond").build()) \
            .build()
        req = CreateMessageReactionRequest.builder() \
            .message_id(message_id) \
            .request_body(body) \
            .build()
        resp = lark_client.im.v1.message_reaction.create(req)
        if resp.success() and resp.data:
            return resp.data.reaction_id
        _check_terminal_code(resp, message_id)
        log.debug("Queued reaction add failed: code=%s msg=%s", resp.code, resp.msg)
    except Exception:
        log.debug("Queued reaction add error", exc_info=True)
    return None


def remove_queued_reaction(lark_client, message_id: str,
                           reaction_id: str | None):
    if not reaction_id:
        return
    if is_message_unavailable(message_id):
        return
    try:
        from lark_oapi.api.im.v1 import DeleteMessageReactionRequest

        req = DeleteMessageReactionRequest.builder() \
            .message_id(message_id) \
            .reaction_id(reaction_id) \
            .build()
        resp = lark_client.im.v1.message_reaction.delete(req)
        if not resp.success():
            log.debug("Queued reaction remove failed: code=%s msg=%s",
                      resp.code, resp.msg)
    except Exception:
        log.debug("Queued reaction remove error", exc_info=True)


class FlushController:
    """Non-blocking throttle for streaming card updates."""

    def __init__(self, flush_fn, use_cardkit: bool):
        self._lock = threading.Lock()
        self._flush_fn = flush_fn
        self._throttle_ms = CARDKIT_THROTTLE_MS if use_cardkit else PATCH_THROTTLE_MS
        self._flush_in_progress = False
        self._needs_reflush = False
        self._last_flush_time = 0.0
        self._pending_text = ""
        self._timer: Optional[threading.Timer] = None
        self._card_ready = False
        self._closed = False

    def set_card_ready(self):
        with self._lock:
            self._card_ready = True
            if self._pending_text and not self._closed:
                self._execute_flush()

    def request_flush(self, text: str):
        with self._lock:
            if self._closed:
                return
            self._pending_text = text
            if not self._card_ready:
                return
            if self._flush_in_progress:
                self._needs_reflush = True
                return
            elapsed_ms = (time.time() - self._last_flush_time) * 1000
            if elapsed_ms >= self._throttle_ms:
                if elapsed_ms >= GAP_THRESHOLD_MS and self._last_flush_time > 0:
                    self._schedule_locked(BATCH_AFTER_GAP_MS / 1000)
                else:
                    self._execute_flush()
            else:
                remaining = (self._throttle_ms - elapsed_ms) / 1000
                self._schedule_locked(remaining)

    def drain(self):
        text_to_flush = None
        with self._lock:
            self._closed = True
            self._needs_reflush = False
            if self._timer:
                self._timer.cancel()
                self._timer = None
            if (self._pending_text and self._card_ready
                    and not self._flush_in_progress):
                text_to_flush = self._pending_text

        deadline = time.time() + 3.0
        while True:
            with self._lock:
                if not self._flush_in_progress:
                    break
            if time.time() >= deadline:
                log.warning("FlushController drain: timed out waiting for in-flight flush")
                break
            time.sleep(0.02)

        if not text_to_flush:
            with self._lock:
                if (self._pending_text and self._card_ready
                        and not self._flush_in_progress):
                    text_to_flush = self._pending_text

        if text_to_flush:
            try:
                self._flush_fn(text_to_flush)
            except Exception:
                log.exception("FlushController drain error")

    def _schedule_locked(self, delay_sec: float):
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(delay_sec, self._on_timer)
        self._timer.daemon = True
        self._timer.start()

    def _execute_flush(self):
        self._flush_in_progress = True
        self._needs_reflush = False
        text = self._pending_text
        if self._timer:
            self._timer.cancel()
            self._timer = None
        threading.Thread(target=self._do_flush, args=(text,), daemon=True).start()

    def _on_timer(self):
        with self._lock:
            if self._closed or not self._card_ready or self._flush_in_progress:
                if self._flush_in_progress and not self._closed:
                    self._needs_reflush = True
                return
            self._execute_flush()

    def _do_flush(self, text: str):
        try:
            self._flush_fn(text)
        except Exception:
            log.exception("FlushController flush error")
        finally:
            with self._lock:
                self._last_flush_time = time.time()
                self._flush_in_progress = False
                if (not self._closed and self._needs_reflush
                        and self._pending_text and self._card_ready):
                    self._execute_flush()


def build_card(content: str, is_error: bool = False,
               compact: bool = False) -> dict:
    if compact and len(content) > COMPACT_MAX_CHARS:
        content = content[:COMPACT_MAX_CHARS] + "\n\n…（已截断）"

    chunks = []
    remaining = content
    while remaining:
        if len(remaining) <= MAX_DIV_CHARS:
            chunks.append(remaining)
            break
        split_at = MAX_DIV_CHARS
        for sep in ["\n", " "]:
            idx = remaining.rfind(sep, 0, split_at)
            if idx > split_at // 2:
                split_at = idx + 1
                break
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]

    if not chunks:
        chunks = ["(空回复)"]

    elements = [
        {"tag": "div", "text": {"content": chunk, "tag": "lark_md"}}
        for chunk in chunks
    ]

    title_text = "❌ 错误" if is_error else f"✅ {_bot_display_name}"
    template = "red" if is_error else "green"
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": title_text, "tag": "plain_text"},
            "template": template,
        },
        "elements": elements,
    }

    payload = json.dumps(card, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_CARD_PAYLOAD_BYTES and not compact:
        return build_card(content, is_error, compact=True)
    return card


def build_processing_card() -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"content": "🤔 处理中...", "tag": "plain_text"},
                   "template": "blue"},
        "elements": [{
            "tag": "div",
            "text": {"content": "正在处理你的请求，请稍候...", "tag": "lark_md"},
        }],
    }


def build_streaming_card(content: str) -> dict:
    if len(content) > COMPACT_MAX_CHARS:
        content = "…（前文已省略）\n\n" + content[-COMPACT_MAX_CHARS:]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"content": "⏳ 生成中...", "tag": "plain_text"},
                   "template": "blue"},
        "elements": [
            {"tag": "div", "text": {"content": content, "tag": "lark_md"}}
        ],
    }


def build_cardkit_streaming_card() -> dict:
    return {
        "schema": "2.0",
        "config": {
            "streaming_mode": True,
            "enable_forward": False,
            "summary": {"content": "思考中..."},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "element_id": CARDKIT_ELEMENT_ID,
                    "content": "",
                },
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": "⏳ 生成中..."},
                    ],
                },
            ],
        },
    }


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}m{secs:.0f}s"


def build_cardkit_final_card(content: str, is_error: bool = False,
                             elapsed_s: float = 0) -> dict:
    chunks: list[str] = []
    remaining = content
    while remaining:
        if len(remaining) <= MAX_DIV_CHARS:
            chunks.append(remaining)
            break
        split_at = MAX_DIV_CHARS
        for sep in ["\n", " "]:
            idx = remaining.rfind(sep, 0, split_at)
            if idx > split_at // 2:
                split_at = idx + 1
                break
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]

    if not chunks:
        chunks = ["(空回复)"]

    elements = [
        {
            "tag": "markdown",
            "element_id": f"{CARDKIT_ELEMENT_ID}_{i}" if i > 0 else CARDKIT_ELEMENT_ID,
            "content": chunk,
        }
        for i, chunk in enumerate(chunks)
    ]

    # Footer with status and elapsed time
    status = "❌ 出错" if is_error else "✅ 完成"
    footer_parts = [status]
    if elapsed_s > 0:
        footer_parts.append(f"耗时 {_format_elapsed(elapsed_s)}")
    elements.append({"tag": "hr"})
    elements.append({
        "tag": "note",
        "elements": [
            {"tag": "plain_text", "content": " · ".join(footer_parts)},
        ],
    })

    summary_text = re.sub(r"[*`#>\[\]()~_]", "", content)
    summary_text = re.sub(r"\s+", " ", summary_text).strip()
    config = {"streaming_mode": False, "enable_forward": True}
    if summary_text:
        config["summary"] = {"content": summary_text[:120]}

    return {
        "schema": "2.0",
        "config": config,
        "header": {
            "title": {"content": ("❌ " if is_error else "✅ ") + ("错误" if is_error else _bot_display_name)},
            "template": "red" if is_error else "green",
        },
        "body": {"elements": elements},
    }


def add_typing_indicator(lark_client, message_id: str) -> Optional[str]:
    if is_message_unavailable(message_id):
        return None
    try:
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest, CreateMessageReactionRequestBody,
        )
        from lark_oapi.api.im.v1.model.emoji import Emoji

        body = CreateMessageReactionRequestBody.builder() \
            .reaction_type(Emoji.builder().emoji_type("Typing").build()) \
            .build()
        req = CreateMessageReactionRequest.builder() \
            .message_id(message_id) \
            .request_body(body) \
            .build()
        resp = lark_client.im.v1.message_reaction.create(req)
        if resp.success() and resp.data:
            return resp.data.reaction_id
        _check_terminal_code(resp, message_id)
        log.debug("Typing indicator add failed: code=%s msg=%s", resp.code, resp.msg)
    except Exception:
        log.debug("Typing indicator add error", exc_info=True)
    return None


def remove_typing_indicator(lark_client, message_id: str,
                            reaction_id: Optional[str]):
    if not reaction_id:
        return
    if is_message_unavailable(message_id):
        return
    try:
        from lark_oapi.api.im.v1 import DeleteMessageReactionRequest

        req = DeleteMessageReactionRequest.builder() \
            .message_id(message_id) \
            .reaction_id(reaction_id) \
            .build()
        resp = lark_client.im.v1.message_reaction.delete(req)
        if not resp.success():
            log.debug("Typing indicator remove failed: code=%s msg=%s",
                      resp.code, resp.msg)
    except Exception:
        log.debug("Typing indicator remove error", exc_info=True)


class ResponseHandle:
    """Deliver results to Feishu via CardKit (primary) or IM patch (fallback)."""

    def __init__(self, lark_client, chat_id: str,
                 thread_id: Optional[str] = None,
                 source_message_id: Optional[str] = None):
        self.client = lark_client
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.source_message_id = source_message_id
        self.card_message_id: Optional[str] = None
        self._use_cardkit = False
        self._cardkit_card_id: Optional[str] = None
        self._cardkit_seq = 0
        self._seq_lock = threading.Lock()
        self._flush_ctrl: Optional[FlushController] = None
        self._typing_reaction_id: Optional[str] = None
        self._card_creation_lock = threading.Lock()
        self._card_fallback_timer: Optional[threading.Timer] = None
        self._card_fallback_timeout = 8
        self._summary_updated = False
        self._terminated = False
        self._stream_start_time: Optional[float] = None
        self._runner: Optional[ClaudeRunner] = None
        self._runner_tag: Optional[str] = None

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._cardkit_seq += 1
            return self._cardkit_seq

    def set_card_id(self, message_id: str):
        self.card_message_id = message_id

    def stream_update(self, content: str):
        if self._terminated:
            return
        if not self.card_message_id and not self._ensure_card(content):
            return
        if self._flush_ctrl:
            self._flush_ctrl.request_flush(content)
        elif self.card_message_id:
            self._try_patch(self.card_message_id, build_streaming_card(content))

    def send_processing_indicator(self) -> bool:
        if self.source_message_id:
            self._typing_reaction_id = add_typing_indicator(
                self.client, self.source_message_id)
            if is_message_unavailable(self.source_message_id):
                self._terminated = True
                log.info("Message recalled before processing: %s", self.source_message_id)
                return False
        return True

    def _ensure_card(self, initial_content: str = "") -> bool:
        with self._card_creation_lock:
            if self.card_message_id:
                return True

            if self._card_fallback_timer:
                self._card_fallback_timer.cancel()
                self._card_fallback_timer = None

            if self._typing_reaction_id and self.source_message_id:
                remove_typing_indicator(
                    self.client, self.source_message_id, self._typing_reaction_id)
                self._typing_reaction_id = None

            card_id = self._try_create_cardkit()
            if card_id:
                self._use_cardkit = True
                self._cardkit_card_id = card_id
                msg_id = self._send_cardkit_im(card_id)
                if msg_id:
                    self.card_message_id = msg_id
                    self._stream_start_time = time.time()
                    log.info("CardKit card created (deferred): card_id=%s msg_id=%s",
                             card_id, msg_id)
                    self._flush_ctrl = FlushController(self._perform_flush, use_cardkit=True)
                    self._flush_ctrl.set_card_ready()
                    if initial_content:
                        self._flush_ctrl.request_flush(initial_content)
                    return True
                if self._terminated:
                    return False
                log.warning("CardKit card created but IM send failed, falling back to IM patch")
                self._use_cardkit = False
                self._cardkit_card_id = None

            card = build_streaming_card(initial_content) if initial_content else build_processing_card()
            msg_id = self._send_card(card)
            if msg_id:
                self.card_message_id = msg_id
                self._flush_ctrl = FlushController(self._perform_flush, use_cardkit=False)
                self._flush_ctrl.set_card_ready()
                return True
            return False

    def deliver(self, content: str, is_error: bool = False):
        if self._card_fallback_timer:
            self._card_fallback_timer.cancel()
            self._card_fallback_timer = None
        if self._terminated:
            log.info("Deliver skipped: message unavailable (recalled/deleted)")
            return
        if self._typing_reaction_id and self.source_message_id:
            remove_typing_indicator(
                self.client, self.source_message_id, self._typing_reaction_id)
            self._typing_reaction_id = None
        if not self.card_message_id:
            self._ensure_card()
        if self._flush_ctrl:
            self._flush_ctrl.drain()
        if self._use_cardkit and self._cardkit_card_id:
            self._deliver_cardkit(content, is_error)
        else:
            self._deliver_im_patch(content, is_error)

    def _deliver_cardkit(self, content: str, is_error: bool):
        card_id = self._cardkit_card_id
        seq = self._next_seq()
        settings_json = json.dumps({"config": {"streaming_mode": False}})
        settings_ok = False
        try:
            body = SettingsCardRequestBody.builder() \
                .uuid(str(uuid.uuid4())) \
                .settings(settings_json) \
                .sequence(seq) \
                .build()
            req = SettingsCardRequest.builder() \
                .card_id(card_id) \
                .request_body(body) \
                .build()
            resp = self.client.cardkit.v1.card.settings(req)
            if resp.success():
                settings_ok = True
            else:
                log.error("CardKit settings failed: code=%s msg=%s", resp.code, resp.msg)
        except Exception:
            log.exception("CardKit settings error")

        if not settings_ok:
            log.warning("CardKit settings failed, falling back to IM patch")
            self.card_message_id = None
            self._deliver_im_patch(content, is_error)
            return

        elapsed_s = 0.0
        if self._stream_start_time:
            elapsed_s = time.time() - self._stream_start_time
        seq = self._next_seq()
        final_card_json = build_cardkit_final_card(content, is_error, elapsed_s=elapsed_s)
        try:
            card_obj = Card.builder() \
                .type("card_json") \
                .data(json.dumps(final_card_json, ensure_ascii=False)) \
                .build()
            body = UpdateCardRequestBody.builder() \
                .card(card_obj) \
                .uuid(str(uuid.uuid4())) \
                .sequence(seq) \
                .build()
            req = UpdateCardRequest.builder() \
                .card_id(card_id) \
                .request_body(body) \
                .build()
            resp = self.client.cardkit.v1.card.update(req)
            if not resp.success():
                log.error("CardKit card.update failed: code=%s msg=%s", resp.code, resp.msg)
                self.card_message_id = None
                self._deliver_im_patch(content, is_error)
        except Exception:
            log.exception("CardKit card.update error")
            self.card_message_id = None
            self._deliver_im_patch(content, is_error)

    def _deliver_im_patch(self, content: str, is_error: bool):
        card = build_card(content, is_error)
        if self.card_message_id:
            if self._try_patch(self.card_message_id, card):
                return
            self._try_patch(self.card_message_id, build_card("回复已发送到新消息", compact=True))
            card = build_card(content, is_error, compact=True)
        self._send_card(card)

    def _update_summary_to_typing(self):
        """Switch CardKit summary from '思考中...' to '输入中...' on first content."""
        if self._summary_updated or not self._cardkit_card_id:
            return
        self._summary_updated = True
        try:
            settings_json = json.dumps(
                {"config": {"summary": {"content": "输入中..."}}})
            body = SettingsCardRequestBody.builder() \
                .uuid(str(uuid.uuid4())) \
                .settings(settings_json) \
                .sequence(self._next_seq()) \
                .build()
            req = SettingsCardRequest.builder() \
                .card_id(self._cardkit_card_id) \
                .request_body(body) \
                .build()
            resp = self.client.cardkit.v1.card.settings(req)
            if not resp.success():
                log.debug("Summary update failed: code=%s msg=%s",
                          resp.code, resp.msg)
        except Exception:
            log.debug("Summary update error", exc_info=True)

    def _perform_flush(self, text: str):
        if self._use_cardkit and self._cardkit_card_id:
            self._update_summary_to_typing()
            seq = self._next_seq()
            try:
                body = ContentCardElementRequestBody.builder() \
                    .uuid(str(uuid.uuid4())) \
                    .content(text) \
                    .sequence(seq) \
                    .build()
                req = ContentCardElementRequest.builder() \
                    .card_id(self._cardkit_card_id) \
                    .element_id(CARDKIT_ELEMENT_ID) \
                    .request_body(body) \
                    .build()
                resp = self.client.cardkit.v1.card_element.content(req)
                if not resp.success():
                    log.debug("CardKit stream update: code=%s msg=%s", resp.code, resp.msg)
            except Exception:
                log.exception("CardKit stream update error")
        elif self.card_message_id:
            self._try_patch(self.card_message_id, build_streaming_card(text))

    def _terminate_pipeline(self, source: str = ""):
        if self._terminated:
            return
        self._terminated = True
        log.warning("Pipeline terminated by unavailable message (source=%s, message_id=%s)",
                    source, self.source_message_id)
        if self._runner and self._runner_tag:
            self._runner.cancel(self._runner_tag)

    def _pre_send_content(self, card_id: str, content: str):
        try:
            body = ContentCardElementRequestBody.builder() \
                .uuid(str(uuid.uuid4())) \
                .content(content) \
                .sequence(self._next_seq()) \
                .build()
            req = ContentCardElementRequest.builder() \
                .card_id(card_id) \
                .element_id(CARDKIT_ELEMENT_ID) \
                .request_body(body) \
                .build()
            resp = self.client.cardkit.v1.card_element.content(req)
            if not resp.success():
                log.debug("Pre-send content push: code=%s msg=%s", resp.code, resp.msg)
        except Exception:
            log.debug("Pre-send content push failed", exc_info=True)

    def _try_create_cardkit(self) -> Optional[str]:
        try:
            card_json = build_cardkit_streaming_card()
            body = CreateCardRequestBody.builder() \
                .type("card_json") \
                .data(json.dumps(card_json, ensure_ascii=False)) \
                .build()
            req = CreateCardRequest.builder().request_body(body).build()
            resp = self.client.cardkit.v1.card.create(req)
            if resp.success():
                return resp.data.card_id
            log.warning("CardKit create failed: code=%s msg=%s", resp.code, resp.msg)
            return None
        except Exception:
            log.exception("CardKit create error")
            return None

    def _send_cardkit_im(self, card_id: str) -> Optional[str]:
        content = json.dumps({"type": "card", "data": {"card_id": card_id}})
        if self.source_message_id and is_message_unavailable(self.source_message_id):
            self._terminate_pipeline("_send_cardkit_im pre-check")
            return None
        try:
            if self.source_message_id:
                from lark_oapi.api.im.v1 import (
                    ReplyMessageRequest, ReplyMessageRequestBody,
                )
                body = ReplyMessageRequestBody.builder() \
                    .msg_type("interactive") \
                    .content(content) \
                    .reply_in_thread(bool(self.thread_id)) \
                    .build()
                req = ReplyMessageRequest.builder() \
                    .message_id(self.source_message_id) \
                    .request_body(body) \
                    .build()
                resp = self.client.im.v1.message.reply(req)
            else:
                body = CreateMessageRequestBody.builder() \
                    .receive_id(self.chat_id) \
                    .msg_type("interactive") \
                    .content(content) \
                    .build()
                req = CreateMessageRequest.builder() \
                    .receive_id_type("chat_id") \
                    .request_body(body) \
                    .build()
                resp = self.client.im.v1.message.create(req)
            if resp.success():
                return resp.data.message_id
            if self.source_message_id and _check_terminal_code(resp, self.source_message_id):
                self._terminate_pipeline("_send_cardkit_im")
                return None
            log.error("CardKit IM send failed: code=%s msg=%s", resp.code, resp.msg)
            return None
        except Exception:
            log.exception("CardKit IM send error")
            return None

    def _send_card(self, card: dict) -> Optional[str]:
        if self.source_message_id and is_message_unavailable(self.source_message_id):
            self._terminate_pipeline("_send_card pre-check")
            return None
        try:
            card_json = json.dumps(card, ensure_ascii=False)
            if self.source_message_id:
                from lark_oapi.api.im.v1 import (
                    ReplyMessageRequest, ReplyMessageRequestBody,
                )
                body = ReplyMessageRequestBody.builder() \
                    .msg_type("interactive") \
                    .content(card_json) \
                    .reply_in_thread(bool(self.thread_id)) \
                    .build()
                req = ReplyMessageRequest.builder() \
                    .message_id(self.source_message_id) \
                    .request_body(body) \
                    .build()
                resp = self.client.im.v1.message.reply(req)
            else:
                body = CreateMessageRequestBody.builder() \
                    .receive_id(self.chat_id) \
                    .msg_type("interactive") \
                    .content(card_json) \
                    .build()
                req = CreateMessageRequest.builder() \
                    .receive_id_type("chat_id") \
                    .request_body(body) \
                    .build()
                resp = self.client.im.v1.message.create(req)
            if resp.success():
                return resp.data.message_id
            if self.source_message_id and _check_terminal_code(resp, self.source_message_id):
                self._terminate_pipeline("_send_card")
                return None
            log.error("Send card failed: code=%s msg=%s", resp.code, resp.msg)
            return None
        except Exception:
            log.exception("Send card error")
            return None

    def _try_patch(self, message_id: str, card: dict) -> bool:
        try:
            req = PatchMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(json.dumps(card, ensure_ascii=False))
                    .build()
                ).build()
            resp = self.client.im.v1.message.patch(req)
            if resp.success():
                return True
            log.error("Patch failed: code=%s msg=%s", resp.code, resp.msg)
            return False
        except Exception:
            log.exception("Patch error")
            return False
