"""
Feishu Docs API wrapper — read/write cloud documents as Markdown.

Uses Feishu MCP proxy (mcp.feishu.cn) for block↔Markdown conversion.
All operations require user_access_token (UAT).

Usage:
    docs = FeishuDocs(app_id, app_secret, lark_client)
    content = docs.fetch(chat_id, user_open_id, doc_id="doxcnXXX")
    docs.update(chat_id, user_open_id, doc_id="doxcnXXX", markdown="# New content")
"""

import logging
from typing import Optional

from feishu_bridge.api.client import FeishuAPI

log = logging.getLogger("feishu-docs")


class FeishuDocs(FeishuAPI):
    """Feishu cloud document read/write via MCP Markdown interface."""

    SCOPES = [
        "docx:document:readonly",
        "docx:document:create",
        "wiki:wiki:readonly",
    ]
    # No BASE_PATH — docs use MCP, not OAPI directly

    def fetch(self, chat_id: str, user_open_id: str,
              doc_id: str, offset: int = None,
              limit: int = None, *,
              prefetched_token: str = None) -> Optional[dict]:
        """Fetch document content as Markdown.

        Args:
            doc_id: document ID or URL (MCP auto-parses URLs)
            offset: character offset for pagination (large docs)
            limit: max characters to return
            prefetched_token: pre-fetched UAT — bypasses get_token() when set

        Returns:
            {"title": "...", "markdown": "...", ...} or None on auth failure
        """
        token = prefetched_token or self.get_token(chat_id, user_open_id)
        if not token:
            return None

        args = {"doc_id": doc_id}
        if offset is not None:
            args["offset"] = offset
        if limit is not None:
            args["limit"] = limit

        return self.mcp_call("fetch-doc", args, token)

    def update(self, chat_id: str, user_open_id: str,
               doc_id: str, markdown: str,
               mode: str = "overwrite",
               selection: str = None,
               new_title: str = None) -> Optional[dict]:
        """Update document content with Markdown.

        Args:
            doc_id: document ID or URL
            markdown: Markdown content to write
            mode: one of "overwrite", "append", "replace_range",
                  "replace_all", "insert_before", "insert_after", "delete_range"
            selection: text selection for range-based modes
                       (use "selection_with_ellipsis" format)
            new_title: optional new document title

        Returns:
            MCP response dict, or None on auth failure
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        args = {"doc_id": doc_id, "markdown": markdown, "mode": mode}
        if selection:
            args["selection_with_ellipsis"] = selection
        if new_title:
            args["new_title"] = new_title

        return self.mcp_call("update-doc", args, token)

    def create(self, chat_id: str, user_open_id: str,
               title: str, markdown: str = "",
               folder_token: str = None,
               wiki_space: str = None) -> Optional[dict]:
        """Create a new document.

        Args:
            title: document title
            markdown: initial content (Markdown)
            folder_token: target folder (mutually exclusive with wiki_space)
            wiki_space: target wiki space ID

        Returns:
            MCP response with new doc info, or None on auth failure
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        args = {"title": title, "markdown": markdown}
        if folder_token:
            args["folder_token"] = folder_token
        if wiki_space:
            args["wiki_space"] = wiki_space

        return self.mcp_call("create-doc", args, token)

    def delete(self, chat_id: str, user_open_id: str,
               doc_token: str) -> Optional[dict]:
        """Delete a document via Drive API.

        Args:
            doc_token: document token

        Returns:
            Drive API response, or None on auth failure
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        return self._drive("DELETE", f"/files/{doc_token}?type=docx", token)
