"""Message parsing and media helpers for Feishu bridge."""

import json
import logging
import urllib.parse
import uuid
from pathlib import Path
from typing import Optional

from lark_oapi.api.im.v1 import GetMessageResourceRequest

log = logging.getLogger("feishu-bridge")


def parse_post_content(content: dict) -> str:
    """Parse Feishu post (rich-text) message content into plain text."""
    if "content" not in content and "title" not in content:
        content = (
            content.get("zh_cn")
            or content.get("en_us")
            or content.get("ja_jp")
            or {}
        )
    title = content.get("title", "")
    parts = []
    if title:
        parts.append(title)
    for para in content.get("content", []):
        line_parts = []
        for seg in para:
            tag = seg.get("tag", "")
            if tag == "text":
                line_parts.append(seg.get("text", ""))
            elif tag == "a":
                line_parts.append(seg.get("text", seg.get("href", "")))
            elif tag == "img":
                line_parts.append("[图片]")
            elif tag == "media":
                line_parts.append(f"[文件: {seg.get('file_name', '附件')}]")
            elif tag:
                log.debug("parse_post_content: unhandled tag=%s seg=%s", tag, seg)
        line = "".join(line_parts).strip()
        if line:
            parts.append(line)
    return "\n".join(parts)


def _walk_property_elements(elements: list, parts: list) -> None:
    """Recursively extract text from CardKit raw format elements.

    CardKit raw format wraps every element in a ``property`` dict:
    ``element.property.content`` for leaf text, ``element.property.elements``
    for nested containers.  This is the structure returned by Feishu API
    for cross-bot forwarded cards (via ``json_card``).
    """
    for el in elements:
        if not isinstance(el, dict):
            continue
        prop = el.get("property", {})
        if not isinstance(prop, dict):
            continue
        # Leaf text
        if "content" in prop:
            parts.append(prop["content"])
        # Nested container
        if "elements" in prop:
            _walk_property_elements(prop["elements"], parts)


def parse_interactive_content(content: dict) -> Optional[str]:
    """Parse Feishu interactive (card) message content into plain text.

    Supports three formats:
    - json_card wrapper: cross-bot forwarded cards with ``json_card`` string field
    - CardKit v2: ``content.body.elements`` with ``tag: "markdown"``
    - Legacy (schema 1.0): ``content.elements``
    """
    if not content or not isinstance(content, dict):
        return None

    # json_card wrapper: cross-bot forwarded cards wrap content in a
    # stringified JSON field.  Unwrap before proceeding.
    if "json_card" in content:
        try:
            content = json.loads(content["json_card"])
        except (json.JSONDecodeError, TypeError):
            pass

    # CardKit v2: elements under body.elements, text in "content" key
    body = content.get("body")
    if content.get("schema") == "2.0" or (isinstance(body, dict) and "elements" in body):
        elements = content.get("body", {}).get("elements", [])
        parts = []
        for el in elements:
            if isinstance(el, dict):
                tag = el.get("tag", "")
                if tag == "markdown":
                    parts.append(el.get("content", ""))
                elif tag == "div":
                    text_obj = el.get("text", {})
                    if isinstance(text_obj, dict):
                        parts.append(text_obj.get("content", ""))
                    elif isinstance(text_obj, str):
                        parts.append(text_obj)
        text_result = "\n".join(p for p in parts if p)
        if text_result:
            return text_result
        # Fall through to try raw property format or legacy parsing

    # CardKit raw format: body.property.elements with nested property dicts.
    # Used by cross-bot forwarded cards returned via json_card.
    if isinstance(body, dict):
        prop_elements = body.get("property", {}).get("elements", [])
        if prop_elements:
            parts: list[str] = []
            _walk_property_elements(prop_elements, parts)
            text_result = "\n".join(p for p in parts if p)
            if text_result:
                return text_result

    # Legacy card format: elements at top level
    elements = content.get("elements", [])
    parts = []
    for el in elements:
        if isinstance(el, list):
            for sub in el:
                if isinstance(sub, dict):
                    text_val = sub.get("text", "")
                    if isinstance(text_val, str):
                        parts.append(text_val)
                    elif isinstance(text_val, dict):
                        parts.append(text_val.get("content", ""))
        elif isinstance(el, dict):
            text_obj = el.get("text", {})
            if isinstance(text_obj, dict):
                parts.append(text_obj.get("content", ""))
            elif isinstance(text_obj, str):
                parts.append(text_obj)
    text_result = "\n".join(p for p in parts if p)
    return text_result if text_result else None


def fetch_quoted_message(lark_client, message_id: str) -> Optional[dict]:
    """Fetch a quoted message's content and sender metadata."""
    try:
        from lark_oapi.api.im.v1 import GetMessageRequest

        req = GetMessageRequest.builder().message_id(message_id).build()
        resp = lark_client.im.v1.message.get(req)
        if not resp.success():
            log.debug("Fetch message failed: %s %s", resp.code, resp.msg)
            return None

        msg = resp.data.items[0] if resp.data and resp.data.items else None
        if not msg:
            return None

        msg_type = msg.msg_type
        try:
            content = json.loads(msg.body.content) if msg.body else {}
        except (json.JSONDecodeError, TypeError, AttributeError):
            return None

        sender = msg.sender
        sender_type = getattr(sender, "sender_type", None) if sender else None
        sender_id = getattr(sender, "id", None) if sender else None

        if msg_type == "text":
            text = content.get("text", "")
        elif msg_type == "post":
            text = parse_post_content(content)
        elif msg_type == "interactive":
            text = parse_interactive_content(content)
            # CardKit v2 cards are degraded in GetMessage responses;
            # re-fetch with raw_card_content to get the full body.
            _CARD_FALLBACK = "请升级至最新版本客户端，以查看内容"
            if not text or text.strip() == _CARD_FALLBACK:
                card_text = fetch_card_content(lark_client, message_id)
                if card_text:
                    text = card_text
        elif msg_type == "image":
            text = "[图片]"
        elif msg_type == "file":
            text = f"[文件: {content.get('file_name', '附件')}]"
        elif msg_type == "media":
            text = f"[视频/音频: {content.get('file_name', '媒体')}]"
        elif msg_type == "sticker":
            text = "[表情]"
        elif msg_type == "merge_forward":
            text = "[合并转发消息]"
        elif msg_type == "share_chat":
            text = f"[分享群聊: {content.get('chat_name', '')}]"
        elif msg_type == "share_user":
            text = "[分享联系人]"
        elif msg_type == "todo":
            # Todo body: {"task_id": "...", "summary": {"content": [[{tag, text}]]}}
            summary = content.get("summary", {})
            parts = []
            for line in summary.get("content", []):
                for elem in line:
                    if elem.get("tag") == "text":
                        parts.append(elem.get("text", ""))
            text = "".join(parts) or "[todo]"
        else:
            text = f"[{msg_type}]"

        if not text:
            log.debug("fetch_quoted_message: empty content for type=%s id=%s",
                      msg_type, message_id)
            return None

        return {
            "content": text,
            "sender_type": sender_type,
            "sender_id": sender_id,
            "message_id": message_id,
        }
    except Exception:
        log.exception("Fetch quoted message error")
        return None


def fetch_card_content(lark_client, message_id: str) -> Optional[str]:
    """Re-fetch an interactive message with raw_card_content to get full v2 card body.

    Feishu event pushes for CardKit v2 cards may deliver degraded content
    ("请升级至最新版本客户端，以查看内容"). Re-fetching via API with
    card_msg_content_type=raw_card_content returns the full card JSON.

    Returns formatted text (with title prefix) on success, None on failure.
    """
    try:
        import lark_oapi as lark

        safe_mid = urllib.parse.quote(message_id, safe="")
        req = lark.BaseRequest()
        req.http_method = lark.HttpMethod.GET
        req.uri = (
            f"/open-apis/im/v1/messages/{safe_mid}"
            f"?user_id_type=open_id&card_msg_content_type=raw_card_content"
        )
        req.token_types = {lark.AccessTokenType.TENANT}
        resp = lark_client.request(req)

        if resp.code != 0:
            log.debug("fetch_card_content: code=%s msg=%s",
                      resp.code, getattr(resp, "msg", ""))
            return None

        body = json.loads(resp.raw.content)
        items = body.get("data", {}).get("items", [])
        if not items:
            return None

        raw_content = items[0].get("body", {}).get("content")
        if not raw_content:
            return None

        card = json.loads(raw_content)

        # Unwrap json_card wrapper (used by forwarded cards)
        inner_card = card
        if "json_card" in card:
            try:
                inner_card = json.loads(card["json_card"])
            except (json.JSONDecodeError, TypeError):
                inner_card = card

        text = parse_interactive_content(card)
        if not text:
            return None

        return text

    except Exception:
        log.exception("fetch_card_content error for %s", message_id)
        return None


def fetch_forward_messages(lark_client, message_id: str) -> Optional[str]:
    """Fetch and expand sub-messages of a merge_forward message.

    The Feishu API returns all nested sub-messages in a single flat items[]
    array with upper_message_id for parent-child relationships. We build a
    tree from this flat list and format recursively — one API call regardless
    of nesting depth.

    Returns XML-formatted text on success, None on failure.
    """
    try:
        import lark_oapi as lark
        from datetime import datetime, timezone, timedelta

        safe_mid = urllib.parse.quote(message_id, safe="")
        req = lark.BaseRequest()
        req.http_method = lark.HttpMethod.GET
        req.uri = (
            f"/open-apis/im/v1/messages/{safe_mid}"
            f"?user_id_type=open_id&card_msg_content_type=raw_card_content"
        )
        req.token_types = {lark.AccessTokenType.TENANT}
        resp = lark_client.request(req)

        if resp.code != 0:
            log.debug("fetch_forward_messages: code=%s msg=%s",
                      resp.code, getattr(resp, "msg", ""))
            return None

        body = json.loads(resp.raw.content)
        items = body.get("data", {}).get("items", [])
        if not items:
            return None

        # Build children map: parent_id -> [child_items]
        children_map: dict[str, list] = {}
        for item in items:
            item_id = item.get("message_id", "")
            upper = item.get("upper_message_id")
            # Skip root container itself
            if item_id == message_id and not upper:
                continue
            parent = upper or message_id
            children_map.setdefault(parent, []).append(item)

        # Sort each group by create_time ascending
        def _safe_ts(x):
            try:
                return int(str(x.get("create_time") or "0")[:13])
            except (ValueError, TypeError):
                return 0

        for children in children_map.values():
            children.sort(key=_safe_ts)

        bj_tz = timezone(timedelta(hours=8))
        _MAX_DEPTH = 10

        def _format_subtree(
            parent_id: str, depth: int = 0,
            _ancestors: frozenset = frozenset(),
        ) -> str:
            if depth > _MAX_DEPTH or parent_id in _ancestors:
                return "[合并转发消息 (嵌套过深)]"
            _ancestors = _ancestors | {parent_id}
            children = children_map.get(parent_id, [])
            if not children:
                return ""
            parts = []
            indent = "  " * depth
            for child in children:
                msg_type = child.get("msg_type", "text")
                sender = child.get("sender", {})
                sender_id = sender.get("id", "unknown")
                create_time = child.get("create_time")

                ts_str = ""
                if create_time:
                    try:
                        ts_ms = int(str(create_time)[:13])
                        dt = datetime.fromtimestamp(ts_ms / 1000, tz=bj_tz)
                        ts_str = dt.strftime("%m-%d %H:%M")
                    except (ValueError, OSError):
                        pass

                raw = child.get("body", {}).get("content", "{}")
                try:
                    ct = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    ct = {}

                if msg_type == "merge_forward":
                    nested_id = child.get("message_id")
                    nested = (
                        _format_subtree(nested_id, depth + 1, _ancestors)
                        if nested_id else ""
                    )
                    child_text = nested or "[合并转发消息]"
                elif msg_type == "text":
                    child_text = ct.get("text", "")
                elif msg_type == "post":
                    child_text = parse_post_content(ct)
                elif msg_type == "interactive":
                    child_text = parse_interactive_content(ct) or ""
                    _CARD_FALLBACK = "请升级至最新版本客户端，以查看内容"
                    if not child_text or child_text.strip() == _CARD_FALLBACK:
                        child_msg_id = child.get("message_id")
                        if child_msg_id:
                            card_text = fetch_card_content(
                                lark_client, child_msg_id,
                            )
                            if card_text:
                                child_text = card_text
                    if not child_text:
                        child_text = "[卡片消息]"
                elif msg_type == "image":
                    child_text = "[图片]"
                elif msg_type == "file":
                    child_text = f"[文件: {ct.get('file_name', '附件')}]"
                elif msg_type == "sticker":
                    child_text = "[表情]"
                elif msg_type == "media":
                    child_text = f"[媒体: {ct.get('file_name', '媒体')}]"
                else:
                    child_text = f"[{msg_type}]"

                line_hdr = (
                    f"{indent}[{ts_str}] {sender_id}:"
                    if ts_str
                    else f"{indent}{sender_id}:"
                )
                content_lines = child_text.split("\n")
                indented = "\n".join(
                    f"{indent}  {line}" for line in content_lines
                )
                parts.append(f"{line_hdr}\n{indented}")

            return "\n".join(parts)

        formatted = _format_subtree(message_id)
        if not formatted:
            return None

        # Truncate based on final output length (including XML wrapper)
        _WRAPPER_OVERHEAD = len("<forwarded_messages>\n\n</forwarded_messages>")
        _SUFFIX = "\n... (转发消息过长，已截断)"
        _MAX_LEN = 8000
        budget = _MAX_LEN - _WRAPPER_OVERHEAD - len(_SUFFIX)
        if len(formatted) > budget:
            formatted = formatted[:budget] + _SUFFIX

        return f"<forwarded_messages>\n{formatted}\n</forwarded_messages>"

    except Exception:
        log.exception("fetch_forward_messages error for %s", message_id)
        return None


def download_image(lark_client, message_id: str, image_key: str,
                   workspace: str) -> Optional[str]:
    """Download Feishu image to workspace/.tmp/feishu_imgs/."""
    img_dir = Path(workspace) / ".tmp" / "feishu_imgs"
    img_dir.mkdir(parents=True, exist_ok=True)

    dest = img_dir / f"{uuid.uuid4().hex}.png"

    try:
        req = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(image_key) \
            .type("image") \
            .build()

        resp = lark_client.im.v1.message_resource.get(req)
        if resp.success():
            dest.write_bytes(resp.file.read())
            log.info("Image: %s (%d bytes)", dest.name, dest.stat().st_size)
            return str(dest)
        log.error("Image download failed: %s %s", resp.code, resp.msg)
        return None
    except Exception:
        log.exception("Image download error")
        return None


def download_file(lark_client, message_id: str, file_key: str,
                  file_name: str, workspace: str) -> Optional[str]:
    """Download Feishu file attachment to workspace/.tmp/feishu_files/."""
    file_dir = Path(workspace) / ".tmp" / "feishu_files"
    file_dir.mkdir(parents=True, exist_ok=True)

    # Strip path components to prevent traversal, preserve extension
    base = Path(file_name).name
    safe_name = f"{uuid.uuid4().hex[:8]}_{base}"
    dest = file_dir / safe_name

    try:
        req = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(file_key) \
            .type("file") \
            .build()

        resp = lark_client.im.v1.message_resource.get(req)
        if resp.success():
            dest.write_bytes(resp.file.read())
            log.info("File: %s (%d bytes)", dest.name, dest.stat().st_size)
            return str(dest)
        log.error("File download failed: %s %s", resp.code, resp.msg)
        return None
    except Exception:
        log.exception("File download error")
        return None
