"""Feishu UI delivery helpers for Feishu bridge."""

import copy
import json
import logging
import os.path
import re
import subprocess
import threading
import time
import urllib.parse
import uuid
from collections import deque
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

# ---------------------------------------------------------------------------
# Markdown style optimization for Feishu CardKit v2
# ---------------------------------------------------------------------------
# Feishu markdown renders a limited subset of standard markdown.
# This adapter handles: heading downgrade, table/code-block spacing,
# invalid image cleanup, and blank-line compression.
# Ported from openclaw-lark/src/card/markdown-style.js.

_CB_MARK = "___CB_"
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")


def optimize_markdown_style(text: str) -> str:
    """Adapt standard markdown for Feishu CardKit v2 rendering."""
    try:
        r = _optimize_impl(text)
        r = _strip_invalid_image_keys(r)
        return r
    except Exception:
        return text


def _optimize_impl(text: str) -> str:
    # 1. Extract code blocks → placeholders
    code_blocks: list[str] = []

    def _save(m):
        code_blocks.append(m.group(0))
        return f"{_CB_MARK}{len(code_blocks) - 1}___"

    r = re.sub(r"```[\s\S]*?```", _save, text)

    # 2. Heading downgrade: H1→H4, H2-H6→H5 (only when H1-H3 present)
    if re.search(r"^#{1,3} ", r, re.MULTILINE):
        r = re.sub(r"^#{2,6} (.+)$", r"##### \1", r, flags=re.MULTILINE)
        r = re.sub(r"^# (.+)$", r"#### \1", r, flags=re.MULTILINE)

    # 3. Consecutive headings: insert <br> gap
    r = re.sub(
        r"^(#{4,5} .+)\n{1,2}(#{4,5} )",
        r"\1\n<br>\n\2", r, flags=re.MULTILINE,
    )

    # 4. Table spacing
    # 4a. Non-table line → table: ensure blank line
    r = re.sub(r"^([^|\n].*)\n(\|.+\|)", r"\1\n\n\2", r, flags=re.MULTILINE)
    # 4b. Blank line before table block → insert <br>
    r = re.sub(r"\n\n((?:\|.+\|[^\S\n]*\n?)+)", r"\n\n<br>\n\n\1", r)
    # 4c. After table block → append <br>
    r = re.sub(
        r"((?:^\|.+\|[^\S\n]*\n?)+)", r"\1\n<br>\n", r, flags=re.MULTILINE,
    )
    # 4d. Plain text before table: tighten spacing
    r = re.sub(
        r"^((?!#{4,5} )(?!\*\*).+)\n\n(<br>)\n\n(\|)",
        r"\1\n\2\n\3", r, flags=re.MULTILINE,
    )
    # 4d2. Bold line before table
    r = re.sub(
        r"^(\*\*.+)\n\n(<br>)\n\n(\|)",
        r"\1\n\2\n\n\3", r, flags=re.MULTILINE,
    )
    # 4e. Table → plain text: tighten spacing
    r = re.sub(
        r"(\|[^\n]*\n)\n(<br>\n)((?!#{4,5} )(?!\*\*))",
        r"\1\2\3", r, flags=re.MULTILINE,
    )

    # 5. Restore code blocks with <br> spacing
    for i, block in enumerate(code_blocks):
        r = r.replace(f"{_CB_MARK}{i}___", f"\n<br>\n{block}\n<br>\n")

    # 6. Compress 3+ newlines → 2
    r = re.sub(r"\n{3,}", "\n\n", r)
    return r


def _strip_invalid_image_keys(text: str) -> str:
    """Remove ![alt](url) where url is not a Feishu img_xxx key."""
    if "![" not in text:
        return text
    return _IMAGE_RE.sub(
        lambda m: m.group(0) if m.group(2).startswith("img_") else "", text,
    )

# ---------------------------------------------------------------------------
# P0: URL extraction + sidebar applink
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r'(?<!\()\bhttps://\S+')
_IMAGE_URL_RE = re.compile(r'!\[[^\]]*\]\(([^)\s]+)\)')

SIDEBAR_APPLINK = "https://applink.feishu.cn/client/web_url/open"
_MAX_SIDEBAR_URLS = 3
_MAX_URL_LABEL_LEN = 40


def extract_urls(content: str) -> list[str]:
    """Extract unique https:// URLs from content, excluding markdown image URLs."""
    image_urls = set(_IMAGE_URL_RE.findall(content))
    seen: set[str] = set()
    result: list[str] = []
    for m in _URL_RE.finditer(content):
        url = m.group().rstrip('.,;:!?)>]')
        if url in seen or url in image_urls:
            continue
        seen.add(url)
        result.append(url)
        if len(result) >= _MAX_SIDEBAR_URLS:
            break
    return result


def _url_label(url: str) -> str:
    """Generate a short label for a URL: hostname/path_tail, max 40 chars."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path.rstrip("/")
    tail = path.rsplit("/", 1)[-1] if "/" in path else path
    label = f"{host}/{tail}" if tail else host
    if len(label) > _MAX_URL_LABEL_LEN:
        label = label[:_MAX_URL_LABEL_LEN - 1] + "…"
    return label


def to_sidebar_url(url: str) -> str:
    """Generate a Feishu sidebar applink URL."""
    return f"{SIDEBAR_APPLINK}?mode=sidebar-semi&url={urllib.parse.quote(url, safe='')}"


def _build_url_buttons(content: str) -> list[dict]:
    """Build CardKit v2 button elements for sidebar links from content URLs."""
    urls = extract_urls(content)
    buttons = []
    for url in urls:
        sidebar = to_sidebar_url(url)
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"🔗 {_url_label(url)}"},
            "type": "default",
            "size": "small",
            "multi_url": {
                "url": sidebar,
                "pc_url": sidebar,
                "android_url": url,
                "ios_url": url,
            },
        })
    return buttons


# ---------------------------------------------------------------------------
# P1: Action marker protocol (strip / parse / button generation)
# ---------------------------------------------------------------------------

_MARKER_RE = re.compile(r'<!--\s*feishu:\w+\s+.*?-->', re.DOTALL)
_MARKER_PARSE_RE = re.compile(
    r'<!--\s*feishu:(?P<type>confirm|ask|choices)\s+(?P<json>.*?)-->',
    re.DOTALL,
)


def strip_action_markers(text: str) -> str:
    """Lightweight strip of all feishu action markers (for streaming path)."""
    return _MARKER_RE.sub('', text)


def parse_action_markers(content: str) -> tuple[str, list[dict]]:
    """Parse and extract action markers from content.

    Returns (clean_content, markers) where each marker is:
        {"type": "confirm|ask|choices", "payload": <parsed JSON>}
    """
    markers: list[dict] = []
    for m in _MARKER_PARSE_RE.finditer(content):
        try:
            payload = json.loads(m.group("json").strip())
            markers.append({"type": m.group("type"), "payload": payload})
        except (json.JSONDecodeError, ValueError):
            log.debug("Failed to parse action marker: %s", m.group(0)[:80])
    clean = _MARKER_RE.sub('', content)
    return clean, markers


def _build_action_buttons(markers: list[dict],
                          chat_id: str | None = None,
                          bot_id: str | None = None) -> list[dict]:
    """Convert parsed action markers into CardKit v2 button elements."""
    if not markers or not chat_id or not bot_id:
        return []
    buttons: list[dict] = []
    for marker in markers:
        mtype = marker["type"]
        payload = marker["payload"]
        if mtype == "confirm":
            buttons.append(_action_button(
                "✅ 确认", mtype, "确认", chat_id, bot_id,
                btn_type="primary_filled"))
            buttons.append(_action_button(
                "❌ 取消", mtype, "取消", chat_id, bot_id,
                btn_type="danger"))
        elif mtype == "choices":
            # payload is a list of strings
            if isinstance(payload, list):
                for opt in payload:
                    buttons.append(_action_button(
                        str(opt), mtype, str(opt), chat_id, bot_id))
        elif mtype == "ask":
            # payload has "options" list with "label" keys
            options = payload.get("options", [])
            for opt in options:
                label = opt.get("label", "") if isinstance(opt, dict) else str(opt)
                buttons.append(_action_button(
                    label, mtype, label, chat_id, bot_id))
    return buttons


def _prune_card_cache() -> None:
    """Remove expired entries; evict oldest when over capacity.

    Caller must hold _card_cache_lock.
    """
    now = time.time()
    expired = [k for k, (exp, _) in _card_cache.items() if now > exp]
    for k in expired:
        del _card_cache[k]
    while len(_card_cache) > _CARD_CACHE_MAX_SIZE:
        oldest = min(_card_cache, key=lambda k: _card_cache[k][0])
        del _card_cache[oldest]


def rebuild_card_with_selection(card_ref: str,
                                selected_label: str) -> dict | None:
    """Rebuild a cached card with selected button highlighted and others disabled.

    Returns a v2 card dict ready for callback response, or None on cache miss.
    """
    with _card_cache_lock:
        entry = _card_cache.pop(card_ref, None)
    if not entry:
        return None
    expiry, card = entry
    if time.time() > expiry:
        return None

    card = copy.deepcopy(card)

    # Walk elements to find action-button column_sets and update their state
    for element in card["body"]["elements"]:
        if element.get("tag") != "column_set":
            continue
        for col in element.get("columns", []):
            buttons = col.get("elements", [])
            has_action = any(
                b.get("tag") == "button" and "action" in b.get("value", {})
                for b in buttons
            )
            if not has_action:
                continue
            for btn in buttons:
                if btn.get("tag") != "button":
                    continue
                value = btn.get("value", {})
                if "action" not in value:
                    continue
                if value.get("label") == selected_label:
                    text = btn["text"]["content"]
                    if not text.startswith("✅"):
                        btn["text"]["content"] = f"✅ {text}"
                    btn["type"] = "primary_filled"
                else:
                    btn["type"] = "default"
                btn["disabled"] = True
                # Remove callback value so disabled buttons don't trigger
                btn.pop("value", None)

    # Override config for callback response
    card["config"] = {"update_multi": True}
    return card


def _action_button(text: str, action: str, label: str,
                   chat_id: str, bot_id: str,
                   btn_type: str = "primary",
                   btn_size: str = "medium") -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": btn_type,
        "size": btn_size,
        "value": {
            "action": action,
            "label": label,
            "chat_id": chat_id,
            "bot_id": bot_id,
        },
    }


_unavailable_messages: dict[str, dict] = {}
_UNAVAILABLE_TTL = 30 * 60
_UNAVAILABLE_MAX_SIZE = 512

# Card cache for preserving original content on button callbacks
_card_cache: dict[str, tuple[float, dict]] = {}  # card_ref → (expiry_ts, card_dict)
_card_cache_lock = threading.Lock()
_CARD_CACHE_TTL = 2 * 3600  # 2 hours
_CARD_CACHE_MAX_SIZE = 256

MAX_DIV_CHARS = 10_000
MAX_CARD_PAYLOAD_BYTES = 28 * 1024
COMPACT_MAX_CHARS = 4_000

CARDKIT_ELEMENT_ID = "streaming_output"
CARDKIT_LOADING_ELEMENT_ID = "loading_icon"
CARDKIT_LOADING_ICON_IMG_KEY = "img_v3_02vb_496bec09-4b43-4773-ad6b-0cdd103cd2bg"
CARDKIT_TODO_ELEMENT_ID = "todo_progress"
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


def build_restart_complete_card(version: str = "") -> dict:
    """Green card patched after bridge restarts successfully."""
    text = f"已更新到 v{version}" if version else "重启完成"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": f"✅ {_bot_display_name}", "tag": "plain_text"},
            "template": "green",
        },
        "elements": [{
            "tag": "div",
            "text": {"content": text, "tag": "lark_md"},
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

    # Update notification (IM patch fallback path)
    from feishu_bridge.updater import get_update_banner_text
    _banner = get_update_banner_text()
    if _banner:
        elements.append({
            "tag": "div",
            "text": {"content": _banner, "tag": "lark_md"},
        })

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
                    "tag": "markdown",
                    "content": " ",
                    "icon": {
                        "tag": "custom_icon",
                        "img_key": CARDKIT_LOADING_ICON_IMG_KEY,
                        "size": "16px 16px",
                    },
                    "element_id": CARDKIT_LOADING_ELEMENT_ID,
                },
                {
                    "tag": "markdown",
                    "content": "",
                    "element_id": CARDKIT_TODO_ELEMENT_ID,
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


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _format_usage_footer(usage: dict) -> str:
    """Format last_call_usage into a compact footer string.

    Examples:
        "12.3k in (85% ⚡) · 1.2k out"    — cache hit
        "12.3k in · 1.2k out"              — no cache / cold start
    """
    inp = usage.get("input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_create = usage.get("cache_creation_input_tokens", 0)
    out = usage.get("output_tokens", 0)
    total_in = inp + cache_read + cache_create

    parts = []
    if total_in > 0:
        in_str = f"{_format_tokens(total_in)} in"
        if cache_read > 0:
            hit_pct = int(cache_read / total_in * 100)
            in_str += f" ({hit_pct}% ⚡)"
        parts.append(in_str)
    if out > 0:
        parts.append(f"{_format_tokens(out)} out")
    return " · ".join(parts) if parts else ""


def _get_git_label(workspace: str) -> str | None:
    """Return 'branch' or 'branch*' (dirty), or None on failure."""
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=workspace, capture_output=True, text=True, timeout=5,
        )
        if branch.returncode != 0:
            return None
        name = branch.stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=workspace, capture_output=True, text=True, timeout=5,
        )
        if dirty.returncode == 0 and dirty.stdout.strip():
            name += "*"
        return name
    except Exception as e:
        log.debug("_get_git_label failed for %s: %s", workspace, e)
        return None


def build_cardkit_final_card(content: str, is_error: bool = False,
                             elapsed_s: float = 0,
                             total_tokens: int = 0,
                             last_call_usage: dict | None = None,
                             chat_id: str | None = None,
                             bot_id: str | None = None,
                             model_name: str | None = None,
                             workspace: str | None = None,
                             todos: list[dict] | None = None,
                             context_alert: str | None = None) -> dict:
    # P1: parse action markers BEFORE optimize (Finding 6 ordering fix)
    content, markers = parse_action_markers(content)
    content = optimize_markdown_style(content)

    # Single element — no chunking.  openclaw-lark proves Feishu handles
    # arbitrarily long markdown in one element; splitting into multiple
    # elements causes rendering issues when transitioning from streaming.
    elements = [
        {
            "tag": "markdown",
            "element_id": CARDKIT_ELEMENT_ID,
            "content": content or "(空回复)",
        },
    ]

    # P1: action buttons (confirm/ask/choices) — suppressed when no chat_id/bot_id
    action_buttons = _build_action_buttons(markers, chat_id, bot_id)
    card_ref = None
    if action_buttons:
        # Inject card_ref into each button for cache lookup on callback
        card_ref = uuid.uuid4().hex[:12]
        for btn in action_buttons:
            if "value" in btn:
                btn["value"]["card_ref"] = card_ref
        elements.append({
            "tag": "column_set",
            "flex_mode": "flow",
            "columns": [{
                "tag": "column",
                "width": "auto",
                "elements": action_buttons,
            }],
        })

    # P0: URL sidebar link buttons
    url_buttons = _build_url_buttons(content)
    if url_buttons:
        elements.append({
            "tag": "column_set",
            "flex_mode": "flow",
            "columns": [{
                "tag": "column",
                "width": "auto",
                "elements": url_buttons,
            }],
        })

    # Footer: all status info in one notation-sized element
    # Line 1: ✅ 9/9 tasks · model · elapsed · tokens · git
    # Line 2: context alert (if any)
    # Line 3: update banner (if any)
    status = "❌" if is_error else "✅"
    detail_parts = []
    if todos:
        done = sum(1 for t in todos if t.get("status") == "completed")
        detail_parts.append(f"{done}/{len(todos)} tasks")
    if model_name:
        # Strip "claude-" prefix for brevity (e.g. "claude-opus-4-6" → "opus-4-6")
        short_model = model_name.removeprefix("claude-")
        detail_parts.append(short_model)
    if elapsed_s > 0:
        detail_parts.append(_format_elapsed(elapsed_s))
    # Token usage: prefer detailed last_call_usage with cache hit info,
    # fall back to simple total_tokens for backward compat.
    if last_call_usage:
        detail_parts.append(_format_usage_footer(last_call_usage))
    elif total_tokens > 0:
        detail_parts.append(f"{_format_tokens(total_tokens)} tokens")
    git_label = _get_git_label(workspace) if workspace else None
    if git_label:
        detail_parts.append(git_label)

    status_line = " · ".join([status, *detail_parts]) if detail_parts else status
    footer_lines = [status_line]
    if context_alert:
        footer_lines.append(context_alert)
    from feishu_bridge.updater import get_update_banner_text
    _banner = get_update_banner_text()
    if _banner:
        footer_lines.append(_banner)

    elements.append({
        "tag": "markdown",
        "content": "---\n" + "\n".join(footer_lines),
        "text_size": "notation",
    })

    summary_text = re.sub(r"[*`#>\[\]()~_]", "", content)
    summary_text = re.sub(r"\s+", " ", summary_text).strip()
    config = {"streaming_mode": False, "enable_forward": True}
    if summary_text:
        config["summary"] = {"content": summary_text[:120]}

    card = {
        "schema": "2.0",
        "config": config,
        "body": {"elements": elements},
    }

    # Cache the card so callbacks can rebuild with original content preserved
    if card_ref:
        with _card_cache_lock:
            _prune_card_cache()
            _card_cache[card_ref] = (time.time() + _CARD_CACHE_TTL,
                                     copy.deepcopy(card))

    return card


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
            log.debug("Typing indicator added: msg=%s reaction=%s",
                      message_id, resp.data.reaction_id)
            return resp.data.reaction_id
        _check_terminal_code(resp, message_id)
        log.warning("Typing indicator add failed: code=%s msg=%s",
                    resp.code, resp.msg)
    except Exception:
        log.warning("Typing indicator add error", exc_info=True)
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
                 source_message_id: Optional[str] = None,
                 bot_id: Optional[str] = None):
        self.client = lark_client
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.source_message_id = source_message_id
        self.bot_id = bot_id
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
        self._loading_icon_cleared = False
        self._handle_start_time: float = time.time()
        self._runner: Optional[ClaudeRunner] = None
        self._runner_tag: Optional[str] = None
        self._last_todos: list[dict] | None = None
        self._active_agents: list[dict] = []
        self._tool_history: deque[dict] = deque(maxlen=8)

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

            # Keep typing indicator active during streaming — it will be
            # removed in deliver() or the finally block in process_message().

            card_id = self._try_create_cardkit()
            if card_id:
                self._use_cardkit = True
                self._cardkit_card_id = card_id
                msg_id = self._send_cardkit_im(card_id)
                if msg_id:
                    self.card_message_id = msg_id
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

    def deliver(self, content: str, is_error: bool = False,
                total_tokens: int = 0, model_name: str | None = None,
                workspace: str | None = None,
                context_alert: str | None = None,
                last_call_usage: dict | None = None) -> bool:
        """Send the final content. Returns True iff a Feishu write succeeded.

        bg-task completion delivery (worker post-turn hook) reads this return
        to decide sent vs delivery_failed — a silent API failure must NOT
        mark the outbox row as sent, else the user never sees the reply and
        the watcher stops retrying.
        """
        if self._card_fallback_timer:
            self._card_fallback_timer.cancel()
            self._card_fallback_timer = None
        if self._terminated:
            log.info("Deliver skipped: message unavailable (recalled/deleted)")
            return False
        if self._typing_reaction_id and self.source_message_id:
            remove_typing_indicator(
                self.client, self.source_message_id, self._typing_reaction_id)
            self._typing_reaction_id = None
        if not self.card_message_id:
            self._ensure_card()
        if self._flush_ctrl:
            self._flush_ctrl.drain()
        log.info("Deliver: content_len=%d is_error=%s cardkit=%s",
                 len(content), is_error,
                 bool(self._use_cardkit and self._cardkit_card_id))
        if self._use_cardkit and self._cardkit_card_id:
            return self._deliver_cardkit(content, is_error,
                                         last_call_usage=last_call_usage,
                                         model_name=model_name,
                                         workspace=workspace,
                                         context_alert=context_alert)
        return self._deliver_im_patch(content, is_error,
                                      context_alert=context_alert)

    def _deliver_cardkit(self, content: str, is_error: bool,
                         last_call_usage: dict | None = None,
                         model_name: str | None = None,
                         workspace: str | None = None,
                         context_alert: str | None = None) -> bool:
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
            return self._deliver_im_patch(content, is_error,
                                          context_alert=context_alert)

        elapsed_s = time.time() - self._handle_start_time
        seq = self._next_seq()
        final_card_json = build_cardkit_final_card(
            content, is_error, elapsed_s=elapsed_s,
            last_call_usage=last_call_usage,
            chat_id=self.chat_id, bot_id=self.bot_id,
            model_name=model_name, workspace=workspace,
            todos=self._last_todos, context_alert=context_alert)
        card_data = json.dumps(final_card_json, ensure_ascii=False)
        log.info("CardKit card.update: card_id=%s content_len=%d payload_bytes=%d seq=%d",
                 card_id, len(content), len(card_data.encode("utf-8")), seq)
        try:
            card_obj = Card.builder() \
                .type("card_json") \
                .data(card_data) \
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
            if resp.success():
                return True
            log.error("CardKit card.update failed: code=%s msg=%s", resp.code, resp.msg)
            self.card_message_id = None
            return self._deliver_im_patch(content, is_error,
                                          context_alert=context_alert)
        except Exception:
            log.exception("CardKit card.update error")
            self.card_message_id = None
            return self._deliver_im_patch(content, is_error,
                                          context_alert=context_alert)

    def _deliver_im_patch(self, content: str, is_error: bool,
                          context_alert: str | None = None) -> bool:
        # IM patch has no structured footer; append alerts to content body
        if context_alert:
            content = content + "\n\n---\n" + context_alert
        card = build_card(content, is_error)
        if self.card_message_id:
            if self._try_patch(self.card_message_id, card):
                return True
            self._try_patch(self.card_message_id, build_card("回复已发送到新消息", compact=True))
            card = build_card(content, is_error, compact=True)
        return bool(self._send_card(card))

    # Tool name → user-friendly status for CardKit summary.
    _TOOL_STATUS_MAP = {
        "Bash": "执行命令",
        "Read": "读取文件",
        "Write": "写入文件",
        "Edit": "编辑文件",
        "Grep": "搜索代码",
        "Glob": "查找文件",
        "WebFetch": "抓取网页",
        "WebSearch": "搜索网页",
        "Agent": "分发子任务",
        "TeamCreate": "创建团队",
        "SendMessage": "团队通信",
        "Skill": "执行技能",
        "TodoWrite": "更新任务",
        "NotebookEdit": "编辑笔记",
    }

    @staticmethod
    def _format_tool_hint(tool_name: str, hint_data: str) -> str:
        if not hint_data:
            return ""
        if tool_name in ("Bash", "Read", "Write", "Edit"):
            return os.path.basename(hint_data)
        return hint_data

    @staticmethod
    def _mcp_display_name(tool_name: str) -> str:
        parts = tool_name.split("__", 2)
        if len(parts) >= 3:
            server = parts[1].replace("_", " ").title()
            return f"{server}: {parts[2]}"
        return tool_name

    def tool_status_update(self, tool_calls: list):
        """Update tool history and render progress."""
        if self._terminated or self._summary_updated:
            return
        if not self.card_message_id:
            if not self._ensure_card():
                return
        if not self._cardkit_card_id:
            return

        for tc in tool_calls:
            if isinstance(tc, str):
                name, hint_data = tc, ""
            else:
                name = tc.get("name", "")
                hint_data = tc.get("hint_data", "")

            if name in ("Agent", "TodoWrite", "TeamCreate", "SendMessage"):
                continue

            if name.startswith("mcp__"):
                label = self._mcp_display_name(name)
                hint = ""
            else:
                label = self._TOOL_STATUS_MAP.get(name, name)
                hint = self._format_tool_hint(name, hint_data)

            if self._tool_history:
                last = self._tool_history[-1]
                if last["label"] == label and last["hint"] == hint:
                    last["count"] += 1
                    continue

            self._tool_history.append({"label": label, "hint": hint, "count": 1})

        if self._tool_history:
            latest = self._tool_history[-1]
            self._update_summary(f"{latest['label']}...")

        self._render_progress()

    def _update_summary(self, text: str):
        """Update CardKit card summary text."""
        if not self._cardkit_card_id:
            return
        try:
            settings_json = json.dumps(
                {"config": {"summary": {"content": text}}})
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

    def _update_element(self, element_id: str, content: str,
                        card_id: str | None = None) -> bool:
        """Push content to a CardKit element. Returns True on success."""
        cid = card_id or self._cardkit_card_id
        if not cid:
            return False
        try:
            body = ContentCardElementRequestBody.builder() \
                .uuid(str(uuid.uuid4())) \
                .content(content) \
                .sequence(self._next_seq()) \
                .build()
            req = ContentCardElementRequest.builder() \
                .card_id(cid) \
                .element_id(element_id) \
                .request_body(body) \
                .build()
            resp = self.client.cardkit.v1.card_element.content(req)
            if not resp.success():
                log.debug("Element update failed (%s): code=%s msg=%s",
                          element_id, resp.code, resp.msg)
                return False
            return True
        except Exception:
            log.debug("Element update error (%s)", element_id, exc_info=True)
            return False

    @staticmethod
    def _format_todos(todos: list[dict]) -> str:
        """Format TodoWrite todos list as markdown for card display."""
        lines = []
        for t in todos:
            status = t.get("status", "pending")
            content = t.get("content", "")
            if status == "completed":
                lines.append(f"~~☑ {content}~~")
            elif status == "in_progress":
                lines.append(f"◉ **{content}**")
            else:
                lines.append(f"☐ {content}")
        return "\n".join(lines)

    def _clear_loading_icon(self):
        """Clear loading icon element (called once when todo list appears)."""
        if self._loading_icon_cleared or not self._cardkit_card_id:
            return
        self._loading_icon_cleared = True
        self._update_element(CARDKIT_LOADING_ELEMENT_ID, "")

    def _render_progress(self):
        """Render combined tool history + agent + todo progress."""
        if not self._cardkit_card_id:
            return
        parts = []

        for entry in self._tool_history:
            label = entry["label"]
            hint = entry["hint"]
            count = entry["count"]
            line = f"▸ {label}"
            if hint:
                line += f" `{hint}`"
            if count > 1:
                line += f" ×{count}"
            parts.append(line)

        for a in self._active_agents:
            desc = a.get("description", "")
            atype = a.get("subagent_type", "")
            suffix = f" ({atype})" if atype else ""
            if a.get("status") == "completed":
                parts.append(f"~~☑ {desc}{suffix}~~")
            else:
                parts.append(f"◉ **{desc}{suffix}**")

        if self._last_todos:
            if parts:
                parts.append("")
            parts.append(self._format_todos(self._last_todos))

        self._clear_loading_icon()
        self._update_element(CARDKIT_TODO_ELEMENT_ID, "\n".join(parts))

    def agent_list_update(self, launches: list[dict]):
        """Update agent list when new agents are dispatched."""
        if self._terminated:
            return
        if not self._cardkit_card_id:
            if not self._ensure_card():
                return
        self._active_agents = [{"status": "in_progress", **a} for a in launches]
        self._render_progress()

    def _mark_agents_completed(self):
        """Clear agent display when text starts flowing."""
        if not self._active_agents:
            return
        self._active_agents = []
        self._render_progress()

    def todo_list_update(self, todos: list[dict]):
        """Update the todo element in the streaming card."""
        if self._terminated:
            return
        if not self._cardkit_card_id:
            return
        self._last_todos = todos
        self._render_progress()

    def _update_summary_to_typing(self):
        """Switch CardKit summary from '思考中...' to '输入中...' on first content."""
        if self._summary_updated or not self._cardkit_card_id:
            return
        self._summary_updated = True
        self._update_summary("输入中...")

    def _perform_flush(self, text: str):
        if self._use_cardkit and self._cardkit_card_id:
            if self._tool_history:
                self._tool_history.clear()
                self._render_progress()
            self._mark_agents_completed()
            self._update_summary_to_typing()
            text = strip_action_markers(text)
            text = optimize_markdown_style(text)
            self._update_element(CARDKIT_ELEMENT_ID, text)
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
        self._update_element(CARDKIT_ELEMENT_ID, content, card_id=card_id)

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
