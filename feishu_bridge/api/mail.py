"""
Feishu Mail API wrapper — read, compose, reply, forward, manage drafts.
API format verified against lark CLI dry-run output.

All operations require user_access_token (UAT).

API surface:
  list      → GET  /user_mailboxes/me/messages?folder_id=INBOX&page_size=N
  batch_get → POST /user_mailboxes/me/messages/batch_get  {message_ids:[...]}
  get       → GET  /user_mailboxes/me/messages/{id}
  thread    → GET  /user_mailboxes/me/threads/{id}
  drafts    → GET  /user_mailboxes/me/drafts?page_size=N
  draft     → GET  /user_mailboxes/me/drafts/{id}
  create    → POST /user_mailboxes/me/drafts  {raw:"base64url-EML"}
  send      → POST /user_mailboxes/me/drafts/{id}/send
  delete    → DELETE /user_mailboxes/me/drafts/{id}
  modify    → PUT  /user_mailboxes/me/messages/{id}  {read:true}
  profile   → GET  /user_mailboxes/me/profile
"""

import base64
import logging
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Optional

from feishu_bridge.api.client import FeishuAPI

log = logging.getLogger("feishu-mail")


class FeishuMail(FeishuAPI):
    """Feishu Mail API v1 wrapper — full read/write."""

    SCOPES = [
        "mail:user_mailbox.message:readonly",
        "mail:user_mailbox.message:send",
        "mail:user_mailbox.message:modify",
        "mail:user_mailbox.message.body:read",
        "mail:user_mailbox.message.subject:read",
        "mail:user_mailbox.message.address:read",
        "mail:user_mailbox.folder:read",
    ]
    BASE_PATH = "/open-apis/mail/v1"
    DEFAULT_MAILBOX = "me"
    DEFAULT_PAGE_SIZE = 20

    # ── dispatch ──────────────────────────────────────────────────────

    _ACTIONS = {
        # Read
        "triage", "get_message", "get_thread",
        "list_drafts", "get_draft", "profile",
        # Write
        "compose", "reply", "send_draft", "delete_draft",
        "mark_read",
    }

    def dispatch(self, action: str, chat_id: str, sender_id: str,
                 **kwargs) -> dict:
        """统一入口，归一化 {ok, data/error}."""
        try:
            if action not in self._ACTIONS:
                return {"ok": False, "error": "unsupported_action",
                        "message": f"支持: {', '.join(sorted(self._ACTIONS))}"}

            action_map = {
                "triage": self.triage,
                "get_message": self.get_message,
                "get_thread": self.get_thread,
                "list_drafts": self.list_drafts,
                "get_draft": self.get_draft,
                "profile": self.profile,
                "compose": self.compose,
                "reply": self.reply,
                "send_draft": self.send_draft,
                "delete_draft": self.delete_draft,
                "mark_read": self.mark_read,
            }
            method = action_map[action]

            result = method(chat_id, sender_id, **kwargs)
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

    # ── Read operations ───────────────────────────────────────────────

    def triage(self, chat_id: str, user_open_id: str,
               query: str = None, folder_id: str = "INBOX",
               page_size: int = DEFAULT_PAGE_SIZE,
               page_token: str = None) -> Optional[dict]:
        """List message summaries (list IDs → batch_get details)."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        params = {"page_size": min(page_size, 50), "folder_id": folder_id}
        if query:
            params["query"] = query
        if page_token:
            params["page_token"] = page_token

        list_data = self.request(
            "GET", f"/user_mailboxes/me/messages", token, params=params,
        )
        msg_ids = list_data.get("items", [])
        if not msg_ids:
            return {"items": [], "has_more": False, "page_token": ""}

        batch_data = self.request(
            "POST", f"/user_mailboxes/me/messages/batch_get", token,
            json_body={"message_ids": msg_ids},
        )
        return {
            "items": [self._summarize(m) for m in
                      batch_data.get("items", [])],
            "has_more": list_data.get("has_more", False),
            "page_token": list_data.get("page_token", ""),
        }

    def get_message(self, chat_id: str, user_open_id: str,
                    message_id: str) -> Optional[dict]:
        """Get single message with full body."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        data = self.request(
            "GET", f"/user_mailboxes/me/messages/{message_id}", token,
        )
        items = data.get("items", [data])
        msg = items[0] if items else data
        return self._summarize(msg, include_body=True)

    def get_thread(self, chat_id: str, user_open_id: str,
                   thread_id: str) -> Optional[dict]:
        """Get all messages in a thread."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        data = self.request(
            "GET", f"/user_mailboxes/me/threads/{thread_id}", token,
        )
        return {
            "thread_id": thread_id,
            "messages": [self._summarize(m, include_body=True)
                         for m in data.get("items", [])],
        }

    def list_drafts(self, chat_id: str, user_open_id: str,
                    page_size: int = 20) -> Optional[dict]:
        """List draft messages."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        data = self.request(
            "GET", f"/user_mailboxes/me/drafts", token,
            params={"page_size": min(page_size, 50)},
        )
        return {
            "items": [{"draft_id": d.get("draft_id", ""),
                       "subject": d.get("subject", ""),
                       "to": self._extract_address_list(d.get("to", []))}
                      for d in data.get("items", [])],
            "has_more": data.get("has_more", False),
        }

    def get_draft(self, chat_id: str, user_open_id: str,
                  draft_id: str) -> Optional[dict]:
        """Get single draft."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        data = self.request(
            "GET", f"/user_mailboxes/me/drafts/{draft_id}", token,
        )
        items = data.get("items", [data])
        d = items[0] if items else data
        return {
            "draft_id": d.get("draft_id", draft_id),
            "subject": d.get("subject", ""),
            "to": self._extract_address_list(d.get("to", [])),
            "cc": self._extract_address_list(d.get("cc", [])),
            "body_text": d.get("body_plain_text", ""),
        }

    def profile(self, chat_id: str, user_open_id: str) -> Optional[dict]:
        """Get current user's email profile."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        data = self.request(
            "GET", f"/user_mailboxes/me/profile", token,
        )
        return {"email": data.get("primary_email_address", ""),
                "name": data.get("user_name", "")}

    # ── Write operations ──────────────────────────────────────────────

    def compose(self, chat_id: str, user_open_id: str, *,
                to: str, subject: str, body: str,
                cc: str = None, html: bool = True,
                send: bool = False) -> Optional[dict]:
        """Compose and save as draft (optionally send).

        Args:
            to: comma-separated recipient addresses
            subject: email subject
            body: email body (HTML if html=True)
            cc: comma-separated CC addresses
            html: True for HTML body, False for plain text
            send: if True, send immediately after creating draft
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        try:
            eml = self._build_eml(to, subject, body, cc=cc, html=html)
        except Exception as e:
            return {"error": "eml_build_failed", "message": str(e)}

        data = self.request(
            "POST", f"/user_mailboxes/me/drafts", token,
            json_body={"raw": self._b64url(eml)},
        )
        items = data.get("items", [data])
        draft = items[0] if items else data
        draft_id = draft.get("draft_id", "")

        result = {"draft_id": draft_id, "sent": False}

        if send and draft_id:
            self.request(
                "POST",
                f"/user_mailboxes/me/drafts/{draft_id}/send", token,
            )
            result["sent"] = True

        return result

    def reply(self, chat_id: str, user_open_id: str, *,
              message_id: str, body: str,
              html: bool = True, reply_all: bool = False,
              send: bool = False) -> Optional[dict]:
        """Reply to a message, save as draft (optionally send)."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        # Fetch original message for subject/to
        try:
            data = self.request(
                "GET", f"/user_mailboxes/me/messages/{message_id}", token,
            )
            items = data.get("items", [data])
            orig = items[0] if items else data
        except Exception:
            return {"error": "fetch_failed", "message": "无法获取原始邮件"}

        subject = orig.get("subject", "")
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        # Build reply body with quoting (plain text)
        orig_body = orig.get("body_plain_text", "")
        orig_date = orig.get("internal_date", "")
        orig_from = self._extract_from(orig)
        orig_name = orig_from.get("name", orig_from.get("address", ""))

        quoted = "\n".join(f"> {line}" for line in orig_body.split("\n"))
        reply_body = f"{body}\n\nOn {orig_date}, {orig_name} wrote:\n{quoted}"

        # Collect recipients
        to_addr = self._extract_address_list(orig.get("reply_to", []))
        if not to_addr:
            to_addr = self._extract_address_list(orig.get("from", []))
        to_str = ", ".join(a["address"] for a in to_addr if a.get("address"))

        cc_str = ""
        if reply_all:
            cc_addrs = self._extract_address_list(orig.get("cc", []))
            cc_str = ", ".join(a["address"] for a in cc_addrs
                               if a.get("address"))

        try:
            eml = self._build_eml(to_str, subject, reply_body,
                                  cc=cc_str or None, html=html)
        except Exception as e:
            return {"error": "eml_build_failed", "message": str(e)}

        data = self.request(
            "POST", f"/user_mailboxes/me/drafts", token,
            json_body={"raw": self._b64url(eml)},
        )
        items = data.get("items", [data])
        draft = items[0] if items else data
        draft_id = draft.get("draft_id", "")

        result = {"draft_id": draft_id, "sent": False}

        if send and draft_id:
            self.request(
                "POST",
                f"/user_mailboxes/me/drafts/{draft_id}/send", token,
            )
            result["sent"] = True

        return result

    def send_draft(self, chat_id: str, user_open_id: str,
                   draft_id: str) -> Optional[dict]:
        """Send an existing draft."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        self.request(
            "POST",
            f"/user_mailboxes/me/drafts/{draft_id}/send", token,
        )
        return {"draft_id": draft_id, "sent": True}

    def delete_draft(self, chat_id: str, user_open_id: str,
                     draft_id: str) -> Optional[dict]:
        """Delete a draft."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        self.request(
            "DELETE", f"/user_mailboxes/me/drafts/{draft_id}", token,
        )
        return {"draft_id": draft_id, "deleted": True}

    def mark_read(self, chat_id: str, user_open_id: str,
                  message_id: str, read: bool = True) -> Optional[dict]:
        """Mark a message as read or unread."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        self.request(
            "PUT", f"/user_mailboxes/me/messages/{message_id}", token,
            json_body={"read": read},
        )
        return {"message_id": message_id, "read": read}

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _summarize(msg: dict, include_body: bool = False) -> dict:
        result = {
            "message_id": msg.get("message_id", ""),
            "thread_id": msg.get("thread_id", ""),
            "subject": msg.get("subject", ""),
            "date": msg.get("internal_date", ""),
            "from": FeishuMail._extract_from(msg),
            "to": FeishuMail._extract_address_list(msg.get("to", [])),
            "cc": FeishuMail._extract_address_list(msg.get("cc", [])),
            "has_attachments": bool(msg.get("has_attachment")),
            "is_read": msg.get("read", False),
        }
        if include_body:
            result["body_text"] = msg.get("body_plain_text", "")
        return result

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
    def _extract_address_list(lst: list) -> list:
        if not lst or not isinstance(lst, list):
            return []
        result = []
        for a in lst:
            if isinstance(a, dict):
                result.append({
                    "name": a.get("name", ""),
                    "address": a.get("mail_address", a.get("address", "")),
                })
        return result

    @staticmethod
    def _build_eml(to: str, subject: str, body: str, *,
                   cc: str = None, html: bool = True) -> str:
        """Build a MIME email message (RFC 2822)."""
        msg = MIMEMultipart()
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(time.time(), localtime=True)
        if cc:
            msg["Cc"] = cc

        subtype = "html" if html else "plain"
        msg.attach(MIMEText(body, subtype, "utf-8"))
        return msg.as_string()

    @staticmethod
    def _b64url(data: str) -> str:
        """Base64url-encode a string (no padding)."""
        return base64.urlsafe_b64encode(data.encode()).rstrip(b"=").decode()
