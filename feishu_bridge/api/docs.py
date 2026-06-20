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
        "docx:document",
        "docx:document:readonly",
        "docx:document:create",
        "wiki:wiki:readonly",
        "drive:drive",
    ]
    # No BASE_PATH — docs use MCP, not OAPI directly

    # -------------------------------------------------------------------
    # Dispatch (full read+write)
    # -------------------------------------------------------------------

    _READ_ACTIONS = {"fetch"}
    _WRITE_ACTIONS = {"create", "update", "delete"}
    _ALL_ACTIONS = _READ_ACTIONS | _WRITE_ACTIONS

    def dispatch(self, action: str, chat_id: str, sender_id: str,
                 **kwargs) -> dict:
        """统一入口，归一化返回 {ok, data/error}."""
        try:
            if action not in self._ALL_ACTIONS:
                return {"ok": False, "error": "unsupported_action",
                        "message": f"支持的操作: "
                        f"{', '.join(sorted(self._ALL_ACTIONS))}"}

            # Read
            if action == "fetch":
                result = self.fetch(
                    chat_id, sender_id,
                    doc_id=kwargs.get("doc_id", ""),
                    offset=kwargs.get("offset"),
                    limit=kwargs.get("limit"))
            # Write
            elif action == "create":
                result = self.create(
                    chat_id, sender_id,
                    title=kwargs.get("title", ""),
                    markdown=kwargs.get("markdown", ""),
                    folder_token=kwargs.get("folder_token"),
                    wiki_space=kwargs.get("wiki_space"))
            elif action == "update":
                result = self.update(
                    chat_id, sender_id,
                    doc_id=kwargs.get("doc_id", ""),
                    markdown=kwargs.get("markdown", ""),
                    mode=kwargs.get("mode", "overwrite"),
                    selection=kwargs.get("selection"),
                    selection_by_title=kwargs.get("selection_by_title"),
                    new_title=kwargs.get("new_title"))
            elif action == "delete":
                result = self.delete(
                    chat_id, sender_id,
                    doc_token=kwargs.get("doc_token") or kwargs.get("doc_id", ""))
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
            log.exception("Docs dispatch error: action=%s", action)
            return {"ok": False, "error": "internal_error",
                    "message": str(e)}

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
               doc_id: str, markdown: str = "",
               mode: str = "overwrite",
               selection: str = None,
               selection_by_title: str = None,
               new_title: str = None) -> Optional[dict]:
        """Update document content with Markdown.

        Args:
            doc_id: document ID or URL
            markdown: Markdown content to write (not required for delete_range/replace_all)
            mode: one of "overwrite", "append", "replace_range",
                  "replace_all", "insert_before", "insert_after", "delete_range"
            selection: text selection via ellipsis format (mutually exclusive with selection_by_title)
            selection_by_title: heading-based section selection, e.g. "## 章节标题"
            new_title: optional new document title

        Returns:
            MCP response dict, or None on auth failure
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        args = {"doc_id": doc_id, "mode": mode}
        if mode != "delete_range":
            args["markdown"] = markdown
        if selection:
            args["selection_with_ellipsis"] = selection
        elif selection_by_title:
            args["selection_by_title"] = selection_by_title
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
