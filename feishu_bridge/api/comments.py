"""
Feishu Drive Comments API wrapper — CRUD for file comments.

All operations require user_access_token (UAT).

Usage:
    comments = FeishuComments(app_id, app_secret, lark_client)
    result = comments.list_comments(chat_id, user_open_id, file_token, file_type="docx")
"""

import logging
from typing import Optional

from feishu_bridge.api.client import FeishuAPI

log = logging.getLogger("feishu-comments")


class FeishuComments(FeishuAPI):
    """Feishu Drive file comments CRUD via OAPI."""

    SCOPES = [
        "drive:drive",
    ]
    BASE_PATH = "/open-apis/drive/v1"

    def list_comments(self, chat_id: str, user_open_id: str,
                      file_token: str, file_type: str = "docx",
                      is_solved: bool = None,
                      page_size: int = 20,
                      page_token: str = None) -> Optional[dict]:
        """List comments on a file.

        Args:
            file_token: document/sheet/bitable token
            file_type: "docx", "sheet", "bitable", etc.
            is_solved: filter by resolved status (None = all)
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        params = {"file_type": file_type, "page_size": page_size}
        if is_solved is not None:
            params["is_solved"] = str(is_solved).lower()
        if page_token:
            params["page_token"] = page_token

        return self.request("GET", f"/files/{file_token}/comments", token,
                            params=params)

    def add_comment(self, chat_id: str, user_open_id: str,
                    file_token: str, file_type: str,
                    content: str) -> Optional[dict]:
        """Add a comment to a file."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        body = {
            "reply_list": {
                "replies": [{
                    "content": {
                        "elements": [{
                            "type": "text_run",
                            "text_run": {"text": content},
                        }]
                    }
                }]
            }
        }

        return self.request("POST", f"/files/{file_token}/comments", token,
                            params={"file_type": file_type},
                            json_body=body)

    def reply_comment(self, chat_id: str, user_open_id: str,
                      file_token: str, file_type: str,
                      comment_id: str, content: str) -> Optional[dict]:
        """Reply to a comment."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        body = {
            "content": {
                "elements": [{
                    "type": "text_run",
                    "text_run": {"text": content},
                }]
            }
        }

        return self.request(
            "POST",
            f"/files/{file_token}/comments/{comment_id}/replies",
            token,
            params={"file_type": file_type},
            json_body=body,
        )

    def resolve_comment(self, chat_id: str, user_open_id: str,
                        file_token: str, file_type: str,
                        comment_id: str) -> Optional[dict]:
        """Mark a comment as resolved."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        return self.request(
            "PATCH",
            f"/files/{file_token}/comments/{comment_id}",
            token,
            params={"file_type": file_type},
            json_body={"is_solved": True},
        )

    def delete_comment(self, chat_id: str, user_open_id: str,
                       file_token: str, file_type: str,
                       comment_id: str) -> Optional[dict]:
        """Delete a comment."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        return self.request(
            "DELETE",
            f"/files/{file_token}/comments/{comment_id}",
            token,
            params={"file_type": file_type},
        )
