"""Message parsing and media helpers for Feishu bridge."""

import json
import logging
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
        line = "".join(line_parts).strip()
        if line:
            parts.append(line)
    return "\n".join(parts)


def parse_interactive_content(content: dict) -> Optional[str]:
    """Parse Feishu interactive (card) message content into plain text."""
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
