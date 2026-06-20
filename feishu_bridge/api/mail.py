"""
Feishu Mail API wrapper — read mail messages and threads.

All operations require user_access_token (UAT).

Usage:
    mail = FeishuMail(app_id, app_secret, lark_client)
    messages = mail.triage(chat_id, user_open_id, query="keyword")
    msg = mail.get_message(chat_id, user_open_id, message_id="xxx")
"""

import logging
from typing import Optional

from feishu_bridge.api.client import FeishuAPI

log = logging.getLogger("feishu-mail")


class FeishuMail(FeishuAPI):
    """Feishu Mail API v1 wrapper."""

    SCOPES = [
        "mail:user_mailbox.message:readonly",
        "mail:user_mailbox.message.body:read",
        "mail:user_mailbox.message.subject:read",
        "mail:user_mailbox.message.address:read",
        "mail:user_mailbox.folder:read",
    ]
    BASE_PATH = "/open-apis/mail/v1"

    DEFAULT_MAILBOX = "me"
    DEFAULT_PAGE_SIZE = 20

    # -------------------------------------------------------------------
    # Dispatch (Phase 1: read-only)
    # -------------------------------------------------------------------

    _READ_ACTIONS = {
        "triage", "get_message", "get_thread",
    }

    def dispatch(self, action: str, chat_id: str, sender_id: str,
                 **kwargs) -> dict:
        """统一入口，归一化返回 {ok, data/error}."""
        try:
            if action not in self._READ_ACTIONS:
                return {"ok": False, "error": "unsupported_action",
                        "message": f"Phase 1 仅支持只读操作: "
                        f"{', '.join(sorted(self._READ_ACTIONS))}"}

            if action == "triage":
                result = self.triage(
                    chat_id, sender_id,
                    query=kwargs.get("query"),
                    folder_id=kwargs.get("folder_id", "INBOX"),
                    page_size=kwargs.get("page_size", self.DEFAULT_PAGE_SIZE),
                    page_token=kwargs.get("page_token"),
                )
            elif action == "get_message":
                result = self.get_message(
                    chat_id, sender_id,
                    kwargs.get("message_id", ""),
                    html=kwargs.get("html", True),
                )
            elif action == "get_thread":
                result = self.get_thread(
                    chat_id, sender_id,
                    kwargs.get("thread_id", ""),
                )
            else:
                return {"ok": False, "error": "unsupported_action",
                        "message": f"未知 action: {action}"}

            if result is None:
                return {"ok": False, "error": "auth_failed"}
            if isinstance(result, dict) and "error" in result:
                return {"ok": False, "error": result["error"],
                        "data": result}
            return {"ok": True, "data": result}
        except Exception as e:
            log.exception("Mail dispatch error: action=%s", action)
            return {"ok": False, "error": "internal_error",
                    "message": str(e)}

    # -------------------------------------------------------------------
    # Read operations
    # -------------------------------------------------------------------

    def triage(self, chat_id: str, user_open_id: str,
               query: str = None, folder_id: str = "INBOX",
               page_size: int = DEFAULT_PAGE_SIZE,
               page_token: str = None) -> Optional[dict]:
        """List mailbox messages (summary view).

        Args:
            query: full-text search query
            folder_id: folder to list (default: INBOX)
            page_size: messages per page
            page_token: pagination cursor
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        params = {
            "page_size": min(page_size, 50),
        }
        if query:
            params["query"] = query
        if page_token:
            params["page_token"] = page_token

        data = self.request("GET",
            f"/users/{self.DEFAULT_MAILBOX}/messages", token, params=params)

        items = data.get("items", [])
        # Extract summary fields for each message
        summaries = []
        for msg in items:
            summaries.append({
                "message_id": msg.get("message_id", ""),
                "thread_id": msg.get("thread_id", ""),
                "subject": msg.get("subject", ""),
                "date": msg.get("internal_date", ""),
                "from": self._extract_from(msg),
                "to": self._extract_to(msg),
                "has_attachments": bool(msg.get("has_attachment")),
                "is_read": msg.get("read", False),
            })

        return {
            "items": summaries,
            "has_more": data.get("has_more", False),
            "page_token": data.get("page_token", ""),
        }

    def get_message(self, chat_id: str, user_open_id: str,
                    message_id: str, html: bool = True) -> Optional[dict]:
        """Get a single mail message with full content.

        Args:
            message_id: opaque message ID
            html: return HTML body (True) or plain text (False)
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        result = self.request(
            "GET",
            f"/users/{self.DEFAULT_MAILBOX}/messages/{message_id}",
            token,
            params={},
        )

        # Extract the message item from the envelope
        items = result.get("items", [result])
        msg = items[0] if items else result

        body = msg.get("body", {})
        body_text = ""
        if html:
            body_text = body.get("html", body.get("content", ""))
        else:
            body_text = body.get("text", body.get("plain_text", ""))

        return {
            "message_id": msg.get("message_id", message_id),
            "thread_id": msg.get("thread_id", ""),
            "subject": msg.get("subject", ""),
            "date": msg.get("internal_date", ""),
            "from": self._extract_from(msg),
            "to": self._extract_to(msg),
            "cc": self._extract_address_list(msg.get("cc", [])),
            "body": self._decode_base64_body(body_text),
            "has_attachments": bool(msg.get("has_attachment")),
            "attachments": self._extract_attachments(msg),
        }

    def get_thread(self, chat_id: str, user_open_id: str,
                   thread_id: str) -> Optional[dict]:
        """Get all messages in a mail thread."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        result = self.request(
            "GET",
            f"/users/{self.DEFAULT_MAILBOX}/threads/{thread_id}",
            token,
        )

        items = result.get("items", [])
        messages = []
        for msg in items:
            body = msg.get("body", {})
            messages.append({
                "message_id": msg.get("message_id", ""),
                "subject": msg.get("subject", ""),
                "date": msg.get("internal_date", ""),
                "from": self._extract_from(msg),
                "to": self._extract_to(msg),
                "body_text": self._decode_base64_body(
                    body.get("text", body.get("plain_text", ""))
                ),
            })

        return {
            "thread_id": thread_id,
            "messages": messages,
            "count": len(messages),
        }

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _decode_base64_body(text: str) -> str:
        """Decode base64url-encoded body if needed. Passes through plain text."""
        if not text or not isinstance(text, str):
            return text or ""
        # Feishu mail API may return base64url-encoded bodies
        import base64
        try:
            # Add padding if needed
            padding = 4 - len(text) % 4
            if padding != 4:
                text += "=" * padding
            decoded = base64.urlsafe_b64decode(text)
            return decoded.decode("utf-8", errors="replace")
        except Exception:
            return text

    @staticmethod
    def _extract_from(msg: dict) -> dict:
        """Extract from address."""
        frm = msg.get("from", [])
        if not frm:
            return {}
        addr = frm[0] if isinstance(frm, list) else frm
        addr = addr.get("from", addr) if isinstance(addr, dict) else {}
        return {
            "name": addr.get("name", "") or addr.get("address_name", ""),
            "address": addr.get("address", "") or addr.get("mail_address", ""),
        }

    @staticmethod
    def _extract_to(msg: dict) -> list:
        """Extract to addresses."""
        to_list = msg.get("to", [])
        return FeishuMail._extract_address_list(to_list)

    @staticmethod
    def _extract_address_list(addr_list: list) -> list:
        """Normalize address list."""
        if not addr_list or not isinstance(addr_list, list):
            return []
        result = []
        for a in addr_list:
            if isinstance(a, dict):
                result.append({
                    "name": a.get("name", "") or a.get("address_name", ""),
                    "address": a.get("address", "") or a.get("mail_address", ""),
                })
        return result

    @staticmethod
    def _extract_attachments(msg: dict) -> list:
        """Extract attachment metadata."""
        atts = msg.get("attachments", [])
        if not atts or not isinstance(atts, list):
            return []
        result = []
        for a in atts:
            if isinstance(a, dict):
                result.append({
                    "name": a.get("name", a.get("attachment_name", "")),
                    "size": a.get("size", 0),
                    "type": a.get("type", a.get("mime_type", "application/octet-stream")),
                })
        return result
