"""
Feishu Mail API wrapper — read mail messages and threads.

All operations require user_access_token (UAT).

API pattern (learned from lark CLI):
  list   → GET  /user_mailboxes/me/messages      → returns base64 message IDs
  batch  → POST /user_mailboxes/me/messages/batch_get → returns message objects
  get    → GET  /user_mailboxes/me/messages/{id} → single message with body
  thread → GET  /user_mailboxes/me/threads/{id}  → all messages in thread
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

    _READ_ACTIONS = {"triage", "get_message", "get_thread"}

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
                )
            elif action == "get_message":
                result = self.get_message(
                    chat_id, sender_id,
                    kwargs.get("message_id", ""),
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
    # triage — list summaries (list IDs → batch_get details)
    # -------------------------------------------------------------------

    def triage(self, chat_id: str, user_open_id: str,
               query: str = None, folder_id: str = "INBOX",
               page_size: int = DEFAULT_PAGE_SIZE) -> Optional[dict]:
        """List mailbox message summaries.

        Steps: list IDs → batch_get details → extract summary fields.
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        # Step 1: list message IDs
        params = {
            "page_size": min(page_size, 50),
            "folder_id": folder_id,
        }
        if query:
            params["query"] = query

        list_data = self.request(
            "GET",
            f"/user_mailboxes/{self.DEFAULT_MAILBOX}/messages",
            token, params=params,
        )

        msg_ids = list_data.get("items", [])
        if not msg_ids:
            return {"items": [], "has_more": False, "page_token": ""}

        # Step 2: batch_get details
        batch_data = self.request(
            "POST",
            f"/user_mailboxes/{self.DEFAULT_MAILBOX}/messages/batch_get",
            token,
            json_body={"message_ids": msg_ids},
        )

        items = batch_data.get("items", [])
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
            "has_more": list_data.get("has_more", False),
            "page_token": list_data.get("page_token", ""),
        }

    # -------------------------------------------------------------------
    # get_message — single message with body
    # -------------------------------------------------------------------

    def get_message(self, chat_id: str, user_open_id: str,
                    message_id: str) -> Optional[dict]:
        """Get a single mail message with full content."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        data = self.request(
            "GET",
            f"/user_mailboxes/{self.DEFAULT_MAILBOX}/messages/{message_id}",
            token,
        )

        items = data.get("items", [data])
        msg = items[0] if items else data

        return {
            "message_id": msg.get("message_id", message_id),
            "thread_id": msg.get("thread_id", ""),
            "subject": msg.get("subject", ""),
            "date": msg.get("internal_date", ""),
            "from": self._extract_from(msg),
            "to": self._extract_to(msg),
            "cc": self._extract_address_list(msg.get("cc", [])),
            "body_text": msg.get("body_plain_text", ""),
            "has_attachments": bool(msg.get("has_attachment")),
        }

    # -------------------------------------------------------------------
    # get_thread — all messages in a conversation
    # -------------------------------------------------------------------

    def get_thread(self, chat_id: str, user_open_id: str,
                   thread_id: str) -> Optional[dict]:
        """Get all messages in a mail thread."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        data = self.request(
            "GET",
            f"/user_mailboxes/{self.DEFAULT_MAILBOX}/threads/{thread_id}",
            token,
        )

        items = data.get("items", [])
        messages = []
        for msg in items:
            messages.append({
                "message_id": msg.get("message_id", ""),
                "subject": msg.get("subject", ""),
                "date": msg.get("internal_date", ""),
                "from": self._extract_from(msg),
                "to": self._extract_to(msg),
                "body_text": msg.get("body_plain_text", ""),
            })

        return {"thread_id": thread_id, "messages": messages,
                "count": len(messages)}

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _extract_from(msg: dict) -> dict:
        frm = msg.get("from", [])
        if not frm:
            return {}
        addr = frm[0] if isinstance(frm, list) else frm
        if not isinstance(addr, dict):
            return {}
        return {"name": addr.get("name", ""),
                "address": addr.get("mail_address",
                                     addr.get("address", ""))}

    @staticmethod
    def _extract_to(msg: dict) -> list:
        return FeishuMail._extract_address_list(msg.get("to", []))

    @staticmethod
    def _extract_address_list(addr_list: list) -> list:
        if not addr_list or not isinstance(addr_list, list):
            return []
        result = []
        for a in addr_list:
            if isinstance(a, dict):
                result.append({
                    "name": a.get("name", ""),
                    "address": a.get("mail_address", a.get("address", "")),
                })
        return result
