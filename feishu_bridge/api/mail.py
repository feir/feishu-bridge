"""
Feishu Mail API wrapper — send, read, folders, rules.

All operations require user_access_token (UAT) with mail:user_mailbox.* scopes.

Usage:
    mail = FeishuMail(app_id, app_secret, lark_client)
    mail.send_message(chat_id, user_open_id, to=[{"mail_address": "a@b.com"}],
                      subject="Hi", body_html="<p>Hello</p>")
"""

import base64
import logging
import os
import uuid as _uuid
from typing import Optional

from feishu_bridge.api.client import FeishuAPI, FeishuAPIError

log = logging.getLogger("feishu-mail")

MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024       # 25 MB per file
MAX_TOTAL_ATTACHMENT_SIZE = 50 * 1024 * 1024  # 50 MB total


class FeishuMail(FeishuAPI):
    """Feishu Mail CRUD via OAPI v1."""

    SCOPES = [
        "mail:user_mailbox.message:readonly",
        "mail:user_mailbox.message:send",
        "mail:user_mailbox.message.subject:read",
        "mail:user_mailbox.message.address:read",
        "mail:user_mailbox.message.body:read",
        "mail:user_mailbox.folder:read",
        "mail:user_mailbox.folder:write",
        "mail:user_mailbox.rule:write",
        "mail:user_mailbox.rule:read",
    ]
    BASE_PATH = "/open-apis/mail/v1"

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    def _build_message_payload(self, *,
                               to: list[dict],
                               subject: str,
                               body_html: str = None,
                               body_plain: str = None,
                               cc: list[dict] = None,
                               bcc: list[dict] = None,
                               from_address: str = None,
                               from_name: str = None,
                               attachment_paths: list[str] = None,
                               ) -> dict:
        """Build a mail message payload used by both send and draft.

        Args:
            to: [{"mail_address": "...", "name": "..."}]
            subject: email subject
            body_html: HTML body (at least one of body_html/body_plain required)
            body_plain: plain text body
            cc/bcc: same format as to
            from_address: alias email address for head_from (note: Feishu API
                silently ignores this and always uses the primary mailbox address;
                only from_name is honoured for display name override)
            from_name: display name for head_from (this works)
            attachment_paths: list of local file paths to attach
        """
        if not body_html and not body_plain:
            raise ValueError("At least one of body_html or body_plain is required")

        payload = {
            "subject": subject,
            "to": to,
        }
        if body_html:
            payload["body_html"] = body_html
        if body_plain:
            payload["body_plain_text"] = body_plain
        if cc:
            payload["cc"] = cc
        if bcc:
            payload["bcc"] = bcc

        # Alias sending via head_from
        if from_address or from_name:
            head_from = {}
            if from_address:
                head_from["mail_address"] = from_address
            if from_name:
                head_from["name"] = from_name
            payload["head_from"] = head_from

        # Attachments — read, validate, encode in API layer
        if attachment_paths:
            attachments = []
            total_size = 0
            for path in attachment_paths:
                if not os.path.isfile(path):
                    raise ValueError(f"Attachment not found: {path}")
                size = os.path.getsize(path)
                if size > MAX_ATTACHMENT_SIZE:
                    raise ValueError(
                        f"Attachment too large ({size} bytes, max {MAX_ATTACHMENT_SIZE}): {path}"
                    )
                total_size += size
                if total_size > MAX_TOTAL_ATTACHMENT_SIZE:
                    raise ValueError(
                        f"Total attachment size exceeds {MAX_TOTAL_ATTACHMENT_SIZE} bytes"
                    )
                with open(path, "rb") as f:
                    data = f.read()
                # base64url encoding (RFC 4648 §5): +→-, /→_, no padding
                encoded = base64.urlsafe_b64encode(data).rstrip(b"=").decode()
                attachments.append({
                    "body": encoded,
                    "filename": os.path.basename(path),
                })
            payload["attachments"] = attachments

        return payload

    def send_message(self, chat_id: str, user_open_id: str, **kwargs) -> Optional[dict]:
        """Send an email via Feishu mail API.

        Accepts the same keyword arguments as _build_message_payload().
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        payload = self._build_message_payload(**kwargs)
        payload["dedupe_key"] = str(_uuid.uuid4())

        return self.request(
            "POST", "/user_mailboxes/me/messages/send", token,
            json_body=payload,
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_messages(self, chat_id: str, user_open_id: str, *,
                      folder: str = None,
                      only_unread: bool = False,
                      page_size: int = 20,
                      page_token: str = None,
                      ) -> Optional[dict]:
        """List message IDs in a mailbox folder.

        Args:
            folder: folder name (e.g. "INBOX") or folder_id string.
                    Name is resolved via list_folders() (case-insensitive).
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        params = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        if only_unread:
            params["only_unread"] = "true"

        # Resolve folder name → folder_id
        if folder:
            folder_id = self._resolve_folder_id(token, folder)
            params["folder_id"] = folder_id

        return self.request(
            "GET", "/user_mailboxes/me/messages", token, params=params,
        )

    def get_message(self, chat_id: str, user_open_id: str, *,
                    message_id: str) -> Optional[dict]:
        """Get full email content by message_id.

        Decodes base64url-encoded body_html and body_plain_text fields
        (Feishu returns them encoded in the get response).
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        data = self.request(
            "GET", f"/user_mailboxes/me/messages/{message_id}", token,
        )
        # Decode base64url body fields if present
        msg = data.get("message", data)
        for field in ("body_html", "body_plain_text"):
            raw = msg.get(field)
            if raw:
                try:
                    # Re-add padding for base64url decode
                    pad = 4 - len(raw) % 4
                    decoded = base64.urlsafe_b64decode(raw + "=" * (pad % 4))
                    msg[field] = decoded.decode("utf-8", errors="replace")
                except Exception:
                    pass  # Leave as-is if decode fails
        return data

    # ------------------------------------------------------------------
    # Folders
    # ------------------------------------------------------------------

    def list_folders(self, chat_id: str, user_open_id: str, *,
                     folder_type: int = None) -> Optional[dict]:
        """List mail folders.

        Args:
            folder_type: 1=system, 2=user (None=all)
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        params = {}
        if folder_type is not None:
            params["folder_type"] = folder_type

        return self.request(
            "GET", "/user_mailboxes/me/folders", token, params=params,
        )

    def create_folder(self, chat_id: str, user_open_id: str, *,
                      name: str,
                      parent_folder_id: int = None) -> Optional[dict]:
        """Create a mail folder.

        Args:
            name: folder display name
            parent_folder_id: parent folder ID (int type, different from folder.id which is str)
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        payload = {"name": name}
        if parent_folder_id is not None:
            payload["parent_folder_id"] = parent_folder_id

        return self.request(
            "POST", "/user_mailboxes/me/folders", token, json_body=payload,
        )

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------

    def list_rules(self, chat_id: str, user_open_id: str) -> Optional[dict]:
        """List mail rules."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        return self.request("GET", "/user_mailboxes/me/rules", token)

    def create_rule(self, chat_id: str, user_open_id: str, *,
                    name: str,
                    condition: dict,
                    action: dict,
                    is_enable: bool = True,
                    ignore_the_rest_of_rules: bool = False,
                    ) -> Optional[dict]:
        """Create a mail rule.

        Args:
            name: rule display name
            condition: {"match_type": 1|2, "items": [{"type": int, "operator": int, "input": str}]}
            action: {"items": [{"type": int, "input": str}]}
            is_enable: whether the rule is active (default True)
            ignore_the_rest_of_rules: stop processing subsequent rules on match (default False)
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        payload = {
            "name": name,
            "condition": condition,
            "action": action,
            "is_enable": is_enable,
            "ignore_the_rest_of_rules": ignore_the_rest_of_rules,
        }
        return self.request(
            "POST", "/user_mailboxes/me/rules", token, json_body=payload,
        )

    def delete_rule(self, chat_id: str, user_open_id: str, *,
                    rule_id: int) -> Optional[dict]:
        """Delete a mail rule by ID (int)."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        return self.request(
            "DELETE", f"/user_mailboxes/me/rules/{rule_id}", token,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_folder_id(self, token: str, folder_input: str) -> str:
        """Resolve a folder name to folder_id.

        Strategy:
        1. GET /user_mailboxes/me/folders and match by name (case-insensitive)
        2. If no match, treat input as literal folder_id
        3. Let the subsequent API call report if the folder_id is invalid

        FeishuAPIError from the folders request is re-raised as-is (not wrapped).
        """
        # This may raise FeishuAPIError — let it propagate
        data = self.request("GET", "/user_mailboxes/me/folders", token)

        folders = data.get("folders", [])
        lower_input = folder_input.lower()
        for f in folders:
            if f.get("name", "").lower() == lower_input:
                return f["id"]

        # No name match — use as literal folder_id
        log.debug("No folder name match for %r, using as literal ID", folder_input)
        return folder_input
