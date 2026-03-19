"""
Feishu Wiki API wrapper — wiki spaces and nodes CRUD.

All operations require user_access_token (UAT).

Usage:
    wiki = FeishuWiki(app_id, app_secret, lark_client)
    spaces = wiki.list_spaces(chat_id, user_open_id)
    node = wiki.get_node(chat_id, user_open_id, node_token)
"""

import logging
from typing import Optional

from feishu_bridge.api.client import FeishuAPI

log = logging.getLogger("feishu-wiki")


class FeishuWiki(FeishuAPI):
    """Feishu Wiki space/node CRUD via OAPI."""

    SCOPES = [
        "wiki:wiki:readonly",
        "wiki:wiki",
    ]
    BASE_PATH = "/open-apis/wiki/v2"

    def list_spaces(self, chat_id: str, user_open_id: str,
                    page_size: int = 20,
                    page_token: str = None) -> Optional[dict]:
        """List wiki spaces the user has access to."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        params = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token

        return self.request("GET", "/spaces", token, params=params)

    def list_nodes(self, chat_id: str, user_open_id: str,
                   space_id: str, parent_node_token: str = None,
                   page_size: int = 20,
                   page_token: str = None) -> Optional[dict]:
        """List nodes in a wiki space (optionally under a parent node)."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        params = {"page_size": page_size}
        if parent_node_token:
            params["parent_node_token"] = parent_node_token
        if page_token:
            params["page_token"] = page_token

        return self.request("GET", f"/spaces/{space_id}/nodes", token,
                            params=params)

    def get_node(self, chat_id: str, user_open_id: str,
                 node_token: str) -> Optional[dict]:
        """Get node info (resolves wiki token to obj_type + obj_token).

        Returns:
            {"node": {"obj_type": "doc"|"sheet"|..., "obj_token": "...", ...}}
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        return self.request("GET", "/spaces/get_node", token,
                            params={"token": node_token})

    def create_node(self, chat_id: str, user_open_id: str,
                    space_id: str, title: str,
                    obj_type: str = "doc",
                    parent_node_token: str = None) -> Optional[dict]:
        """Create a new node in a wiki space.

        Args:
            space_id: target wiki space
            title: node title
            obj_type: "doc", "sheet", "bitable", etc.
            parent_node_token: optional parent node for nesting
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        body = {
            "obj_type": obj_type,
            "title": title,
        }
        if parent_node_token:
            body["parent_node_token"] = parent_node_token

        return self.request("POST", f"/spaces/{space_id}/nodes", token,
                            json_body=body)

    def delete_node(self, chat_id: str, user_open_id: str,
                    space_id: str, node_token: str) -> Optional[dict]:
        """Delete a node from a wiki space."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        return self.request("DELETE",
                            f"/spaces/{space_id}/nodes/{node_token}", token)
