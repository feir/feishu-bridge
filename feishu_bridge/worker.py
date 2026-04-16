"""Worker pipeline for Feishu bridge message processing."""

import json
import logging
import os
import re
import time
import threading
import uuid
from pathlib import Path

from feishu_bridge.parsers import (
    download_file,
    download_image,
    fetch_card_content,
    fetch_forward_messages,
    fetch_quoted_message,
)
from feishu_bridge.commands import _context_window_for_model
from feishu_bridge.quota import WINDOW_LABELS
from feishu_bridge.runtime import (
    BaseRunner, SessionMap,
)
from feishu_bridge.ui import ResponseHandle, remove_typing_indicator

log = logging.getLogger("feishu-bridge")

_MEDIA_MAX_AGE_SECS = 3600  # 1 hour

# ---------------------------------------------------------------------------
# Idle Auto-Compact: proactive compact before prompt cache TTL expires
# ---------------------------------------------------------------------------

# Anthropic prompt cache TTL is 1 hour (ephemeral_1h).  If we compact while
# the cache is still warm (~50 min), the compact call pays cache_read price
# (cheap).  After compact the session history shrinks, so if the cache later
# expires, the cold-start re-cache cost is much smaller.
_IDLE_COMPACT_DELAY = 50 * 60      # 50 minutes — 10 min buffer before 1h TTL
_IDLE_COMPACT_MIN_CTX = 50_000     # Don't compact small sessions


class IdleCompactManager:
    """Schedule a proactive /compact when a session goes idle.

    After each successful interaction, a per-session timer is (re)started.
    When the timer fires, the manager submits a silent compact command
    through the ChatTaskQueue so it serialises with user messages.
    """

    def __init__(self):
        # Keyed by session_key (chat tuple string), not session_id,
        # so session-id rotation (auto-heal) doesn't orphan timers.
        self._timers: dict[str, threading.Timer] = {}
        self._generation: dict[str, int] = {}  # session_key → monotonic counter
        self._lock = threading.Lock()
        # Set by FeishuBot.start() after queue is ready.
        self._enqueue_fn = None   # ChatTaskQueue.enqueue

    def bind(self, enqueue_fn):
        """Bind the queue reference once the bot is running."""
        self._enqueue_fn = enqueue_fn

    def touch(self, session_id: str, session_key: str,
              total_ctx: int, bot_id: str, chat_id: str,
              thread_id: str | None):
        """Register or reset the idle timer for a session."""
        with self._lock:
            old = self._timers.pop(session_key, None)
            if old:
                old.cancel()

            # Bump generation so any in-flight _fire() for an older timer
            # will see a stale generation and skip the enqueue.
            gen = self._generation.get(session_key, 0) + 1
            self._generation[session_key] = gen

            if total_ctx < _IDLE_COMPACT_MIN_CTX:
                return

            timer = threading.Timer(
                _IDLE_COMPACT_DELAY,
                self._fire,
                args=(session_id, session_key, total_ctx,
                      bot_id, chat_id, thread_id, gen),
            )
            timer.daemon = True
            timer.start()
            self._timers[session_key] = timer

    def cancel(self, session_key: str):
        """Cancel the timer for a session (e.g. on /new)."""
        with self._lock:
            old = self._timers.pop(session_key, None)
            if old:
                old.cancel()
            self._generation.pop(session_key, None)

    def _fire(self, session_id: str, session_key: str, total_ctx: int,
              bot_id: str, chat_id: str, thread_id: str | None,
              gen: int = 0):
        with self._lock:
            self._timers.pop(session_key, None)
            # If generation has advanced (touch() was called after this timer
            # was created), this compact is stale — skip it.
            if self._generation.get(session_key, 0) != gen:
                log.debug("Idle compact skipped (stale generation): key=%s",
                          session_key)
                return

        if not self._enqueue_fn:
            return

        log.info("Idle auto-compact firing: sid=%s ctx=%d tokens",
                 session_id[:8], total_ctx)

        item = {
            "_bridge_command": "idle-compact",
            "_cmd_arg": "",
            "_session_id": session_id,
            "bot_id": bot_id,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "message_id": None,
            "sender_id": None,
            "_queued_reaction_id": None,
            "_queue_key": session_key,
        }
        try:
            self._enqueue_fn(session_key, item)
        except Exception:
            log.warning("Idle auto-compact enqueue failed: sid=%s",
                        session_id[:8], exc_info=True)


idle_compact_mgr = IdleCompactManager()


def cleanup_stale_media(workspace: str, max_age: int = _MEDIA_MAX_AGE_SECS):
    """Delete media files older than *max_age* seconds from temp dirs."""
    cutoff = time.time() - max_age
    dirs = [
        Path(workspace) / ".tmp" / "feishu_imgs",
        Path(workspace) / ".tmp" / "feishu_files",
    ]
    removed = 0
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.is_file():
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed += 1
                except OSError:
                    pass
    if removed:
        log.info("Cleaned up %d stale media file(s)", removed)


def start_media_cleanup_timer(workspace: str, interval: int = 600):
    """Run cleanup_stale_media every *interval* seconds in a daemon thread."""
    def _loop():
        while True:
            time.sleep(interval)
            try:
                cleanup_stale_media(workspace)
            except Exception:
                log.exception("Media cleanup error")

    t = threading.Thread(target=_loop, daemon=True, name="media-cleanup")
    t.start()
    log.info("Media cleanup timer started (interval=%ds, max_age=%ds)",
             interval, _MEDIA_MAX_AGE_SECS)


def _build_quota_alert(result: dict, quota_snapshot=None) -> str:
    """Build quota alert string from stream event + API snapshot.

    Priority: stream event ``status=rejected`` is authoritative (real-time).
    For utilization percentages, prefer API snapshot (stream event lacks them).
    """
    import time as _time

    parts = []

    # 1. Stream event: rejected status (real-time, highest priority)
    rli = result.get("rate_limit_info")
    if rli and rli.get("status") == "rejected":
        utilization = rli.get("utilization", 0)
        resets_at = rli.get("resetsAt", 0)
        limit_type = rli.get("rateLimitType", "")
        label = "7 天" if "seven_day" in limit_type else "5 小时"
        reset_str = ""
        if resets_at:
            remaining = max(0, resets_at - _time.time())
            hours, mins = divmod(int(remaining) // 60, 60)
            if remaining > 0:
                reset_str = f"，约 {hours}h{mins:02d}m 后重置"
        parts.append(f"🚫 {label}配额已用尽（{utilization:.0%}）{reset_str}")
        return "\n".join(parts)

    # 2. Stream event: allowed_warning with utilization (if present)
    if rli and rli.get("status") == "allowed_warning":
        util = rli.get("utilization", 0)
        if util >= 0.75:
            limit_type = rli.get("rateLimitType", "")
            label = "7 天" if "seven_day" in limit_type else "5 小时"
            resets_at = rli.get("resetsAt", 0)
            reset_str = ""
            if resets_at:
                remaining = max(0, resets_at - _time.time())
                hours, mins = divmod(int(remaining) // 60, 60)
                if remaining > 0:
                    reset_str = f"，约 {hours}h{mins:02d}m 后重置"
            parts.append(f"⚠️ {label}配额 {util:.0%}{reset_str}")

    # 3. API snapshot: add utilization for all windows above threshold
    if quota_snapshot and quota_snapshot.available and not quota_snapshot.stale:
        for key, label in WINDOW_LABELS.items():
            w = quota_snapshot.windows.get(key)
            if not w:
                continue
            util_pct = w.utilization  # already in percentage (e.g. 18.0)
            if util_pct >= 50:
                icon = "🔴" if util_pct >= 80 else "🟡"
                remaining = max(0, w.resets_at_epoch - _time.time())
                hours, mins = divmod(int(remaining) // 60, 60)
                reset_str = f" 重置 {hours}h{mins:02d}m" if remaining > 0 else ""
                alert = f"{icon} {label}: {util_pct:.0f}%{reset_str}"
                # Avoid duplicate if stream event already covered this window
                if not any(label in p for p in parts):
                    parts.append(alert)

    # 4. Cookie expiry warning (only in card alert, not every message)
    if quota_snapshot:
        cookie_warn = quota_snapshot.cookie_expiry_warning
        if cookie_warn:
            parts.append(cookie_warn)

    return "\n".join(parts)


def _context_health_alert(result: dict, quota_snapshot=None, runner=None) -> str | None:
    """Check context utilization and return a plain alert string, or None.

    Returns clean text (no ``---`` divider or leading newlines) suitable
    for embedding in a card footer.  The caller is responsible for
    presentation formatting.

    Uses ``peak_context_tokens`` (high-water mark before auto-compact)
    when available, falling back to ``last_call_usage`` or ``usage``.
    Also reports when auto-compact was detected so the user knows
    earlier conversation turns may have been summarised.

    Args:
        quota_snapshot: Optional QuotaSnapshot from the API poller.
    """
    # Determine context window size (API value preferred, model inference fallback)
    max_ctx = result.get("default_context_window", 200_000)
    model_usage = result.get("modelUsage", {})
    for _model, mu in model_usage.items():
        cw = mu.get("contextWindow", 0)
        max_ctx = cw if cw > 0 else _context_window_for_model(_model)
        break

    # If auto-compact was detected, alert with pre-compact peak usage
    compact_detected = result.get("compact_detected", False)
    peak_tokens = result.get("peak_context_tokens", 0)
    if compact_detected and peak_tokens > 0:
        peak_pct = peak_tokens / max_ctx * 100
        return (
            f"⚠️ 上下文已自动压缩（压缩前 {peak_pct:.0f}%）"
            "— 早期对话可能被概括，建议关注上下文完整性"
        )

    # No compact — check current usage against thresholds
    usage = result.get("last_call_usage") or result.get("usage")
    if not usage:
        return None
    total_ctx = (usage.get("input_tokens", 0)
                 + usage.get("cache_read_input_tokens", 0)
                 + usage.get("cache_creation_input_tokens", 0))
    if total_ctx == 0:
        return None

    pct = total_ctx / max_ctx * 100

    # Build quota alert from both stream event and API snapshot
    rate_alert = _build_quota_alert(result, quota_snapshot)

    supports_compact = runner is None or runner.supports_compact()
    tail_80 = "`/compact` 压缩" if supports_compact else "`/new` 开始新会话"
    tail_70 = "`/compact` 压缩上下文" if supports_compact else "`/new` 开始新会话"

    if pct >= 80:
        alert = f"🔴 Context {pct:.0f}% — 建议 `/new` 新会话或 {tail_80}"
        return "\n".join(filter(None, [alert, rate_alert]))
    if pct >= 70:
        alert = f"🟡 Context {pct:.0f}% — 可考虑 {tail_70}"
        return "\n".join(filter(None, [alert, rate_alert]))
    return rate_alert or None


def format_task_detail_bridge(task: dict) -> str:
    """Format task detail for bridge-level display (completed tasks)."""
    import datetime as _dt

    lines = []
    lines.append(f"**{task.get('summary', '未命名任务')}**")
    lines.append("状态: ✅ 已完成")
    guid = task.get("guid", "?")
    lines.append(f"GUID: `{guid}`")
    desc = task.get("description", "")
    if desc:
        lines.append(f"描述: {desc[:200]}")
    due = task.get("due")
    if due and due.get("timestamp"):
        ts = int(str(due["timestamp"])[:10])
        due_dt = _dt.datetime.fromtimestamp(ts)
        lines.append(f"截止: {due_dt.strftime('%Y-%m-%d %H:%M')}")
    return "\n".join(lines)



def _write_auth_file(chat_id: str, sender_id: str, user_token: str | None) -> str:
    """Write a temporary auth file for feishu_cli.py.

    Returns the file path. Caller must clean up in finally block.
    """
    auth_path = f"/tmp/feishu_auth_{uuid.uuid4()}.json"
    fd = os.open(auth_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({
                "user_access_token": user_token,
                "chat_id": chat_id,
                "sender_id": sender_id,
            }, f)
    except BaseException:
        try:
            os.unlink(auth_path)
        except OSError:
            pass
        raise
    return auth_path


def _cleanup_auth_card(feishu_mod, sender_id: str):
    """Best-effort cleanup of the OAuth auth card after result delivery."""
    if not feishu_mod or not sender_id:
        return
    try:
        feishu_mod.cleanup_auth_card(sender_id)
    except Exception:
        log.warning("Failed to cleanup auth card for %s", sender_id[:8],
                    exc_info=True)


def process_message(
    item: dict,
    bot_config: dict,
    lark_client,
    session_map: SessionMap,
    runner: BaseRunner,
    feishu_tasks=None,
    feishu_docs=None,
    feishu_sheets=None,
    feishu_api_error_cls=None,
    response_handle_cls=ResponseHandle,
    download_image_fn=download_image,
    download_file_fn=download_file,
    fetch_card_content_fn=fetch_card_content,
    fetch_forward_messages_fn=fetch_forward_messages,
    fetch_quoted_message_fn=fetch_quoted_message,
    remove_typing_indicator_fn=remove_typing_indicator,
    session_not_found_signatures=None,
):
    """Process a single message from the work queue."""
    bot_id = item["bot_id"]
    chat_id = item["chat_id"]
    thread_id = item.get("thread_id")
    parent_id = item.get("parent_id")
    sender_id = item.get("sender_id", "")
    text = item.get("text", "")
    image_key = item.get("image_key")
    file_key = item.get("file_key")
    file_name = item.get("file_name")
    message_id = item.get("message_id")

    key = (bot_id, chat_id, thread_id)
    tag = SessionMap._key_str(key)
    handle = response_handle_cls(lark_client, chat_id, thread_id, message_id,
                                 bot_id=bot_id)
    image_path = None
    file_path = None
    auth_file_path = None
    # Prefer runner's own signatures; fall back to caller-provided list for compat
    session_not_found_signatures = (
        runner.get_session_not_found_signatures()
        if hasattr(runner, 'get_session_not_found_signatures')
        else session_not_found_signatures or []
    )

    try:
        handle._runner = runner
        handle._runner_tag = tag

        if image_key and message_id:
            image_path = download_image_fn(
                lark_client, message_id, image_key, bot_config["workspace"]
            )
            if image_path:
                text = (
                    f"{text}\n\n[用户发送了一张图片，"
                    f"已保存到 {image_path}，请查看并回复]"
                )
            else:
                text = f"{text}\n\n[用户发送了一张图片，但下载失败]"

        if file_key and message_id:
            display_name = file_name or "attachment"
            file_path = download_file_fn(
                lark_client, message_id, file_key, display_name,
                bot_config["workspace"],
            )
            if file_path:
                text = (
                    f"{text}\n\n[用户发送了文件: {display_name}，"
                    f"已保存到 {file_path}，请查看并回复]"
                )
            else:
                text = f"{text}\n\n[用户发送了文件: {display_name}，但下载失败]"

        should_fetch_quote = (
            (parent_id and not thread_id)
            or (parent_id and thread_id and not session_map.get(key))
        )
        if should_fetch_quote:
            quoted = fetch_quoted_message_fn(lark_client, parent_id)
            if quoted:
                st = quoted["sender_type"]
                sender_label = "Bot" if st == "app" else ("User" if st == "user" else "Unknown")
                quote_header = f"[引用消息 message_id={parent_id} from={sender_label}]"
                text = f"{quote_header}\n{quoted['content']}\n[/引用消息]\n\n{text}"

        # --- Re-fetch degraded card content via API ---
        # Replace the placeholder (always at the end of text, after any quote prefix).
        # Use rfind to target the last occurrence — avoids replacing identical text
        # that might appear inside a prepended quote block.
        card_mid = item.get("_card_message_id")
        if card_mid and fetch_card_content_fn:
            card_text = fetch_card_content_fn(lark_client, card_mid)
            if card_text:
                original_text = item.get("text", "")
                if original_text:
                    pos = text.rfind(original_text)
                    if pos >= 0:
                        text = text[:pos] + card_text + text[pos + len(original_text):]
                    else:
                        text = card_text
                else:
                    text = card_text
                log.info("Re-fetched card content for %s", card_mid)

        # --- Expand merge_forward sub-messages via API ---
        forward_mid = item.get("_merge_forward_message_id")
        if forward_mid and fetch_forward_messages_fn:
            forward_text = fetch_forward_messages_fn(lark_client, forward_mid)
            if forward_text:
                original_text = item.get("text", "")
                if original_text:
                    pos = text.rfind(original_text)
                    if pos >= 0:
                        text = text[:pos] + forward_text + text[pos + len(original_text):]
                    else:
                        text = forward_text
                else:
                    text = forward_text
                log.info("Expanded merge_forward for %s", forward_mid)

        # --- Auto-fetch Feishu doc/wiki/sheet content from URLs in message ---
        feishu_urls = item.get("_feishu_urls") or []
        if feishu_urls and (feishu_docs or feishu_sheets) and feishu_api_error_cls:
            import requests as _requests
            # Silent per-service token check: auto-fetch must NOT trigger OAuth.
            # get_cached_token() returns None without sending auth cards.
            _doc_token = feishu_docs.get_cached_token(sender_id) if feishu_docs else None
            _sheet_token = feishu_sheets.get_cached_token(sender_id) if feishu_sheets else None
            fetched_parts = []
            # Count doc-type URLs for adaptive fetch strategy:
            # single short doc → full content; single long / multi → preview only
            _doc_url_count = sum(1 for t, _ in feishu_urls
                                if t in ("doc", "docx", "wiki"))
            for url_type, url_token in feishu_urls:
                try:
                    if url_type == "wiki":
                        # Wiki links need doc token for node resolution
                        if not feishu_docs or not _doc_token:
                            fetched_parts.append(
                                f"[飞书链接 wiki/{url_token}: 未授权，使用 feishu-cli 命令时将自动触发授权]"
                            )
                            continue
                        try:
                            node_data = feishu_docs.request(
                                "GET", "/open-apis/wiki/v2/spaces/get_node",
                                _doc_token, params={"token": url_token})
                            node = node_data.get("node", {})
                            obj_type = node.get("obj_type", "doc")
                            obj_token = node.get("obj_token", url_token)
                        except feishu_api_error_cls:
                            # Fallback: treat as doc
                            obj_type = "doc"
                            obj_token = url_token

                        if obj_type not in ("doc", "docx", "wiki", "sheet"):
                            fetched_parts.append(
                                f"[飞书wiki {url_token}: 不支持的节点类型 {obj_type}，请直接在飞书中打开]"
                            )
                            continue
                        if obj_type == "sheet":
                            # Redirect to sheets handler
                            if not feishu_sheets or not _sheet_token:
                                fetched_parts.append(
                                    f"[飞书表格 {url_token}: 未授权，使用 feishu-cli 命令时将自动触发授权]"
                                )
                                continue
                            result = feishu_sheets.info(
                                chat_id, sender_id, obj_token, prefetched_token=_sheet_token)
                            if result is None:
                                continue
                            spreadsheet = result.get("spreadsheet", result)
                            title = spreadsheet.get("title", url_token)
                            sheets_list = result.get("sheets", [])
                            sheet_names = [s.get("title", "?") for s in sheets_list[:10]]
                            fetched_parts.append(
                                f"[飞书表格: {title}]\n"
                                f"工作表: {', '.join(sheet_names)}\n"
                                f"(使用 `/feishu-sheet read {obj_token} <sheet>!<range>` 读取具体数据)\n"
                                f"[/飞书表格]"
                            )
                            continue
                        # doc/docx/wiki doc — fetch via MCP
                        result = feishu_docs.fetch(
                            chat_id, sender_id, doc_id=obj_token, prefetched_token=_doc_token)
                        if result is None:
                            continue
                        title = result.get("title", "")
                        md = result.get("markdown", "")
                        if not md:
                            md = result.get("text", str(result))
                        header = f"[飞书文档: {title}]" if title else f"[飞书文档 {url_token}]"
                        cli_hint = f"feishu-cli read-doc --token {obj_token}"
                        if _doc_url_count >= 2 and len(md) > 200:
                            preview = md[:200].rstrip()
                            fetched_parts.append(
                                f"{header}\n{preview}...\n"
                                f"(共 {len(md)} 字，使用 `{cli_hint}` 获取全文)\n[/飞书文档]")
                        elif len(md) >= 2000:
                            preview = md[:500].rstrip()
                            fetched_parts.append(
                                f"{header}\n{preview}...\n"
                                f"(共 {len(md)} 字，使用 `{cli_hint}` 获取全文)\n[/飞书文档]")
                        else:
                            fetched_parts.append(f"{header}\n{md}\n[/飞书文档]")
                        continue
                    if url_type in ("doc", "docx"):
                        if not feishu_docs or not _doc_token:
                            fetched_parts.append(
                                f"[飞书链接 {url_type}/{url_token}: 未授权，使用 feishu-cli 命令时将自动触发授权]"
                            )
                            continue
                        result = feishu_docs.fetch(
                            chat_id, sender_id, doc_id=url_token, prefetched_token=_doc_token)
                        if result is None:
                            continue
                        title = result.get("title", "")
                        md = result.get("markdown", "")
                        if not md:
                            md = result.get("text", str(result))
                        header = f"[飞书文档: {title}]" if title else f"[飞书文档 {url_token}]"
                        cli_hint = f"feishu-cli read-doc --token {url_token}"
                        if _doc_url_count >= 2 and len(md) > 200:
                            preview = md[:200].rstrip()
                            fetched_parts.append(
                                f"{header}\n{preview}...\n"
                                f"(共 {len(md)} 字，使用 `{cli_hint}` 获取全文)\n[/飞书文档]")
                        elif len(md) >= 2000:
                            preview = md[:500].rstrip()
                            fetched_parts.append(
                                f"{header}\n{preview}...\n"
                                f"(共 {len(md)} 字，使用 `{cli_hint}` 获取全文)\n[/飞书文档]")
                        else:
                            fetched_parts.append(f"{header}\n{md}\n[/飞书文档]")
                    elif url_type == "sheets":
                        if not feishu_sheets or not _sheet_token:
                            fetched_parts.append(
                                f"[飞书链接 sheets/{url_token}: 未授权，使用 feishu-cli 命令时将自动触发授权]"
                            )
                            continue
                        result = feishu_sheets.info(
                            chat_id, sender_id, url_token, prefetched_token=_sheet_token)
                        if result is None:
                            continue
                        spreadsheet = result.get("spreadsheet", result)
                        title = spreadsheet.get("title", url_token)
                        sheets_list = result.get("sheets", [])
                        sheet_names = [s.get("title", "?") for s in sheets_list[:10]]
                        fetched_parts.append(
                            f"[飞书表格: {title}]\n"
                            f"工作表: {', '.join(sheet_names)}\n"
                            f"(使用 `/feishu-sheet read {url_token} <sheet>!<range>` 读取具体数据)\n"
                            f"[/飞书表格]"
                        )
                    elif url_type == "base":
                        fetched_parts.append(
                            f"[飞书多维表格 {url_token}]\n"
                            f"(使用 `/feishu-bitable` 命令操作多维表格)\n"
                            f"[/飞书多维表格]"
                        )
                except feishu_api_error_cls as e:
                    log.warning("Auto-fetch feishu %s/%s failed: %s", url_type, url_token, e)
                    if e.code == 403 or "permission" in str(e.msg).lower():
                        fetched_parts.append(
                            f"[飞书{url_type} {url_token}: 无访问权限，请确认文档已对你开放权限]"
                        )
                    elif "no permission" in str(e.msg).lower() or "not found" in str(e.msg).lower():
                        fetched_parts.append(
                            f"[飞书{url_type} {url_token}: 文档不存在或无权限访问]"
                        )
                    else:
                        fetched_parts.append(
                            f"[飞书{url_type} {url_token}: 获取失败 — {e.msg}]"
                        )
                except (_requests.exceptions.Timeout, _requests.exceptions.ConnectionError):
                    fetched_parts.append(
                        f"[飞书{url_type} {url_token}: 网络超时，请稍后重试]"
                    )
                except Exception:
                    log.exception("Auto-fetch feishu %s/%s unexpected error", url_type, url_token)
                    fetched_parts.append(
                        f"[飞书{url_type} {url_token}: 获取失败]"
                    )
            if fetched_parts:
                context = "\n\n".join(fetched_parts)
                text = f"{context}\n\n{text}"
                log.info("Auto-fetched %d feishu URL(s) for chat %s", len(fetched_parts), chat_id)

        if not handle.send_processing_indicator():
            log.info("Skipping Claude invocation: message recalled (mid=%s)", message_id)
            return handle

        def on_stream(text_so_far):
            handle.stream_update(text_so_far)

        def on_tool_status(tool_name):
            handle.tool_status_update(tool_name)

        def on_todo_update(todos):
            handle.todo_list_update(todos)

        def on_agent_update(launches):
            handle.agent_list_update(launches)

        todo_task_id = item.get("_todo_task_id")
        if todo_task_id and not feishu_api_error_cls:
            log.debug(
                "todo auto-drive disabled: feishu_api_error_cls not provided (chat=%s)",
                chat_id,
            )
        if todo_task_id and feishu_tasks and feishu_api_error_cls:
            import requests as _requests
            try:
                data = feishu_tasks.get_task(chat_id, sender_id, todo_task_id)
            except feishu_api_error_cls as e:
                if e.code == 404 or "not found" in str(e.msg).lower():
                    fallback = feishu_tasks.find_task_by_id(
                        chat_id, sender_id, todo_task_id, completed=None
                    )
                    if fallback.get("error") == "auth_failed":
                        handle.deliver(feishu_tasks._auth_failed_message())
                        return handle
                    matched = fallback.get("task")
                    if not matched:
                        if fallback.get("truncated"):
                            handle.deliver(
                                "未能在搜索上限内定位此任务，请稍后重试，"
                                "或改用 `/feishu-tasks get <guid>` 直接查询。"
                            )
                            return handle
                        handle.deliver("无法找到此任务，请确认任务 ID 是否正确。")
                        return handle
                    data = {"task": matched}
                elif e.code == 403:
                    handle.deliver("无权访问此任务，请确认任务权限。")
                    return handle
                elif e.code == 429:
                    handle.deliver("飞书 API 请求频繁，请稍后重试。")
                    return handle
                else:
                    handle.deliver("获取任务详情失败，请稍后重试。")
                    return handle
            except (_requests.exceptions.Timeout, _requests.exceptions.ConnectionError):
                handle.deliver("获取任务详情失败（网络超时），请稍后重试。")
                return handle
            except Exception:
                log.exception("Unexpected error fetching task %s", todo_task_id)
                handle.deliver("获取任务详情失败，请稍后重试。")
                return handle

            if isinstance(data, dict) and "error" in data:
                handle.deliver(feishu_tasks._auth_failed_message())
                return handle

            task = data.get("task", data)
            completed_at = task.get("completed_at")
            if completed_at and str(completed_at) != "0":
                detail = format_task_detail_bridge(task)
                handle.deliver(f"该任务已完成：\n\n{detail}")
                return handle

            t_summary = task.get("summary", "未命名任务")
            t_guid = task.get("guid", todo_task_id)
            t_due = task.get("due")
            if t_due and t_due.get("timestamp"):
                import datetime as _dt

                ts = int(str(t_due["timestamp"])[:10])
                due_str = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            else:
                due_str = "无"
            t_desc = task.get("description", "") or ""
            if len(t_desc) > 500:
                t_desc = t_desc[:500] + "\u2026"
            t_desc = t_desc or "无"

            subtask_count = task.get("subtask_count", 0)
            subtask_lines = ""
            if subtask_count:
                try:
                    st_data = feishu_tasks.list_subtasks(chat_id, sender_id, t_guid)
                    if "error" not in st_data:
                        st_items = st_data.get("items", [])
                        parts = []
                        for st in st_items:
                            st_status = "\u2705" if st.get("completed_at") else "\u2b1c"
                            parts.append(f"  {st_status} {st.get('summary', '?')}")
                        subtask_lines = "\n".join(parts)
                except Exception:
                    log.warning("list_subtasks failed for %s", t_guid, exc_info=True)
                if not subtask_lines:
                    subtask_lines = f"  (共 {subtask_count} 个，获取失败)"

            prompt_parts = [
                "[飞书任务]",
                f"标题: {t_summary}",
                f"GUID: {t_guid}",
                f"截止: {due_str}",
                f"描述: {t_desc}",
            ]
            if subtask_count:
                prompt_parts.append(f"子任务 ({subtask_count} 个):")
                prompt_parts.append(subtask_lines)
            prompt_parts.append("")
            prompt_parts.append(
                "用户转发了上述飞书任务，请分析任务内容，"
                "告知用户你的推进计划，等用户确认后开始执行。"
                f"\n完成后提醒用户可执行 `/feishu-tasks complete {t_guid}` "
                "标记任务完成。"
            )
            text = "\n".join(prompt_parts)

        # --- Auth file for feishu_cli.py ---
        # Always create the auth file so feishu-cli is available.  The token
        # may be None for first-time users — feishu-cli will trigger OAuth
        # on-demand when it actually needs the token (not pre-emptively).
        env_extra = None
        try:
            if feishu_docs and runner.wants_auth_file():
                cli_token = feishu_docs.get_cached_token(sender_id)
                auth_file_path = _write_auth_file(chat_id, sender_id, cli_token)
                env_extra = {
                    "FEISHU_AUTH_FILE": auth_file_path,
                    "FEISHU_BOT_NAME": bot_config.get("name", ""),
                }
        except Exception:
            log.warning("Failed to create auth file for CLI", exc_info=True)

        existing_sid = session_map.get(key)
        stale_notice = None
        if existing_sid and not runner.has_session(existing_sid):
            log.info(
                "runner %s has no state for sid=%s — demoting resume=False",
                type(runner).__name__, existing_sid[:8],
            )
            stale_notice = "⚠️ 会话已重建（本地会话在 bridge 重启后未持久化）"
        if existing_sid and stale_notice is None:
            result = runner.run(
                text, session_id=existing_sid, resume=True, tag=tag,
                on_output=on_stream, on_tool_status=on_tool_status,
                on_todo_update=on_todo_update, on_agent_update=on_agent_update,
                env_extra=env_extra,
            )
        else:
            new_sid = existing_sid or str(uuid.uuid4())
            result = runner.run(
                text, session_id=new_sid, resume=False, tag=tag,
                on_output=on_stream, on_tool_status=on_tool_status,
                on_todo_update=on_todo_update, on_agent_update=on_agent_update,
                env_extra=env_extra,
            )
            if stale_notice and isinstance(result, dict) and not result.get("is_error"):
                result["result"] = stale_notice + "\n\n" + (result.get("result") or "")

        if result.get("cancelled"):
            handle.deliver(result["result"])
            return handle

        effective_sid = None
        if result["is_error"] and existing_sid:
            err_text = (result.get("result") or "").lower()
            if any(sig in err_text for sig in session_not_found_signatures):
                log.warning("Session %s not found, auto-healing", existing_sid[:8])
                session_map.delete(key)
                retry_sid = str(uuid.uuid4())
                result = runner.run(
                    text, session_id=retry_sid, resume=False, tag=tag,
                    on_output=on_stream, on_tool_status=on_tool_status,
                    on_todo_update=on_todo_update, on_agent_update=on_agent_update,
                    env_extra=env_extra,
                )
                if result.get("cancelled"):
                    handle.deliver(result["result"])
                    return handle
                effective_sid = (
                    result.get("session_id", retry_sid) if not result["is_error"] else None
                )
                if not result["is_error"]:
                    result["result"] = (
                        result.get("result", "") + "\n\n⚠️ 会话已重建，上下文已清除。"
                    )
                else:
                    result["result"] = (
                        result.get("result", "") + "\n\n⚠️ 会话重建失败，请稍后重试。"
                    )
            else:
                effective_sid = existing_sid
        elif result["is_error"]:
            effective_sid = None
        else:
            effective_sid = result.get("session_id") or existing_sid

        if effective_sid:
            session_map.put(key, effective_sid)
            cost_store = item.get("_cost_store")
            if cost_store is not None and (
                result.get("usage") or result.get("total_cost_usd")
            ):
                existing = cost_store.get(effective_sid, {})
                prev_session_cost = existing.get("session_cost_usd", 0)
                cur_session_cost = result.get("total_cost_usd") or 0
                cost_store[effective_sid] = {
                    "usage": result.get("usage", {}),
                    "last_call_usage": result.get("last_call_usage"),
                    "model_usage": result.get("modelUsage", {}),
                    # total_cost_usd from Claude CLI is session-cumulative
                    "session_cost_usd": cur_session_cost,
                    "turn_cost_usd": max(0, cur_session_cost - prev_session_cost),
                    "rate_limit_info": result.get("rate_limit_info") or existing.get("rate_limit_info"),
                }

            # --- Idle auto-compact timer ---
            if not result["is_error"] and runner.supports_compact():
                _usage = result.get("last_call_usage") or result.get("usage") or {}
                _total_ctx = (_usage.get("input_tokens", 0)
                              + _usage.get("cache_read_input_tokens", 0)
                              + _usage.get("cache_creation_input_tokens", 0))
                idle_compact_mgr.touch(
                    session_id=effective_sid,
                    session_key=SessionMap._key_str(key),
                    total_ctx=_total_ctx,
                    bot_id=item["bot_id"],
                    chat_id=chat_id,
                    thread_id=item.get("thread_id"),
                )

        # --- Strip trailing Status: line (redundant with card footer) ---
        _text = result.get("result") or ""
        _text = re.sub(
            r"\n*Status: (?:DONE|DONE_WITH_CONCERNS|BLOCKED|NEEDS_CONTEXT)\b[^\n]*$",
            "", _text,
        ).rstrip()
        if _text:
            result["result"] = _text

        # --- Context health alert ---
        ctx_alert = None
        if effective_sid and not result.get("is_error"):
            quota_poller = item.get("_quota_poller")
            quota_snap = quota_poller.snapshot if quota_poller else None
            ctx_alert = _context_health_alert(result, quota_snap, runner=runner)

        if not handle._terminated:
            _last_usage = result.get("last_call_usage") or result.get("usage") or {}
            # First key only; multi-model sessions show the primary model
            model_usage = result.get("modelUsage") or {}
            model_name = next(iter(model_usage), None)
            handle.deliver(result["result"], is_error=result["is_error"],
                           last_call_usage=_last_usage,
                           model_name=model_name,
                           workspace=runner.workspace,
                           context_alert=ctx_alert)

        return handle

    except Exception as e:
        log.exception("Worker error for chat %s: %s", chat_id, e)
        if not handle._terminated:
            try:
                handle.deliver("内部错误，请稍后重试。如持续出现请联系管理员。", is_error=True)
            except Exception:
                log.exception("Failed to deliver error message")

    finally:
        if getattr(handle, "_card_fallback_timer", None):
            handle._card_fallback_timer.cancel()
            handle._card_fallback_timer = None
        if getattr(handle, "_typing_reaction_id", None) and handle.source_message_id:
            remove_typing_indicator_fn(
                handle.client, handle.source_message_id, handle._typing_reaction_id
            )
            handle._typing_reaction_id = None
        if auth_file_path:
            try:
                os.unlink(auth_file_path)
            except OSError:
                pass
        # image_path / file_path are NOT deleted here — they may be needed
        # in subsequent turns of the same session.  Stale files are cleaned
        # up periodically by cleanup_stale_media().
        # Delete the auth card so it doesn't linger in chat.
        # In finally so it runs on exceptions/cancellations too.
        # Falls back to any available FeishuAPI instance.
        _cleanup_auth_card(
            feishu_docs or feishu_tasks or feishu_sheets, sender_id
        )

    return handle
