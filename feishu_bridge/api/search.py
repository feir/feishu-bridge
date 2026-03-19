"""
Feishu Search API wrapper — document search, message search, chat history.

All operations require user_access_token (UAT).

Usage:
    search = FeishuSearch(app_id, app_secret, lark_client)
    results = search.search_docs(chat_id, user_open_id, "quarterly report")
"""

import logging
from typing import Optional

from feishu_bridge.api.client import FeishuAPI

log = logging.getLogger("feishu-search")


class FeishuSearch(FeishuAPI):
    """Feishu search + chat history via OAPI."""

    SCOPES = [
        "search:docs:read",
        "search:message",
        "im:message:readonly",
        "drive:drive:readonly",
    ]
    # Mixed endpoints — each method provides full path
    BASE_PATH = ""

    def search_docs(self, chat_id: str, user_open_id: str,
                    query: str, docs_type: str = None,
                    owner_open_id: str = None,
                    page_size: int = 20,
                    page_token: str = None) -> Optional[dict]:
        """Search documents by keyword.

        Args:
            query: search keyword
            docs_type: filter by type ("doc", "sheet", "bitable", etc.)
            owner_open_id: filter by owner
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        body = {"search_key": query, "count": page_size}
        if docs_type:
            body["docs_types"] = [docs_type]
        if owner_open_id:
            body["owner_ids"] = [owner_open_id]
        if page_token:
            try:
                body["offset"] = int(page_token)
            except (TypeError, ValueError):
                body["offset"] = 0

        return self.request(
            "POST",
            "/open-apis/suite/docs-api/search/object",
            token,
            json_body=body,
        )

    def search_messages(self, chat_id: str, user_open_id: str,
                        query: str, target_chat_id: str = None,
                        page_size: int = 20,
                        page_token: str = None) -> Optional[dict]:
        """Search messages across chats.

        Args:
            query: search keyword
            target_chat_id: limit search to specific chat
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        params = {"query": query, "page_size": page_size}
        if target_chat_id:
            params["chat_id"] = target_chat_id
        if page_token:
            params["page_token"] = page_token

        return self.request(
            "GET",
            "/open-apis/search/v2/message",
            token,
            params=params,
        )

    def list_messages(self, chat_id: str, user_open_id: str,
                      container_id: str,
                      start_time: str = None, end_time: str = None,
                      page_size: int = 20,
                      page_token: str = None) -> Optional[dict]:
        """List messages in a chat (chat history).

        Args:
            container_id: chat ID to list messages from
            start_time: Unix timestamp string (seconds)
            end_time: Unix timestamp string (seconds)
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        params = {
            "container_id_type": "chat",
            "container_id": container_id,
            "page_size": page_size,
        }
        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        if page_token:
            params["page_token"] = page_token

        return self.request(
            "GET",
            "/open-apis/im/v1/messages",
            token,
            params=params,
        )

    def list_files(self, chat_id: str, user_open_id: str,
                   folder_token: str = None,
                   page_size: int = 50,
                   page_token: str = None) -> Optional[dict]:
        """List files in a Drive folder.

        Args:
            folder_token: folder token (None = root folder)
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        params = {"page_size": page_size}
        if folder_token:
            params["folder_token"] = folder_token
        if page_token:
            params["page_token"] = page_token

        return self.request(
            "GET",
            "/open-apis/drive/v1/files",
            token,
            params=params,
        )
