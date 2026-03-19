"""
Feishu Bitable (多维表格) API wrapper — tables, records, fields CRUD.

All operations require user_access_token (UAT).

Usage:
    bt = FeishuBitable(app_id, app_secret, lark_client)
    records = bt.list_records(chat_id, user_open_id, app_token="appXXX", table_id="tblXXX")
    bt.create_records(chat_id, user_open_id, app_token, table_id, records=[{"fields": {...}}])
"""

import logging
from typing import Optional

from feishu_bridge.api.client import FeishuAPI

log = logging.getLogger("feishu-bitable")

DEFAULT_PAGE_SIZE = 100


class FeishuBitable(FeishuAPI):
    """Feishu Bitable (多维表格) CRUD via OAPI v1."""

    SCOPES = [
        "base:app:read",
        "base:app:create",
        "base:table:create",
        "base:table:delete",
        "base:record:retrieve",
        "base:record:create",
        "base:record:update",
        "base:record:delete",
        "base:field:read",
        "base:field:create",
        "base:field:update",
    ]
    BASE_PATH = "/open-apis/bitable/v1"

    # -------------------------------------------------------------------
    # App (Bitable) operations
    # -------------------------------------------------------------------

    def get_app(self, chat_id: str, user_open_id: str,
                app_token: str) -> Optional[dict]:
        """Get bitable app metadata."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        return self.request("GET", f"/apps/{app_token}", token)

    def list_tables(self, chat_id: str, user_open_id: str,
                    app_token: str,
                    page_size: int = 50,
                    page_token: str = None) -> Optional[dict]:
        """List tables in a bitable app."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        params = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token

        return self.request("GET", f"/apps/{app_token}/tables", token,
                            params=params)

    # -------------------------------------------------------------------
    # Record operations
    # -------------------------------------------------------------------

    def list_records(self, chat_id: str, user_open_id: str,
                     app_token: str, table_id: str,
                     filter_: str = None,
                     sort: list[dict] = None,
                     field_names: list[str] = None,
                     page_size: int = DEFAULT_PAGE_SIZE,
                     page_token: str = None) -> Optional[dict]:
        """Search/list records in a table.

        Uses the search endpoint (recommended over deprecated list).

        Args:
            filter_: filter expression (e.g., 'CurrentValue.[Status]="Done"')
            sort: sort specs (e.g., [{"field_name": "Created", "desc": True}])
            field_names: only return these fields
            page_size: records per page (max 500)
            page_token: pagination cursor

        Returns:
            {"items": [...], "has_more": bool, "page_token": str, "total": int}
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        body = {"page_size": page_size}
        if filter_:
            body["filter"] = filter_
        if sort:
            body["sort"] = sort
        if field_names:
            body["field_names"] = field_names
        if page_token:
            body["page_token"] = page_token

        return self.request(
            "POST",
            f"/apps/{app_token}/tables/{table_id}/records/search",
            token,
            json_body=body,
        )

    def get_record(self, chat_id: str, user_open_id: str,
                   app_token: str, table_id: str,
                   record_id: str) -> Optional[dict]:
        """Get a single record."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        return self.request(
            "GET",
            f"/apps/{app_token}/tables/{table_id}/records/{record_id}",
            token,
        )

    def create_records(self, chat_id: str, user_open_id: str,
                       app_token: str, table_id: str,
                       records: list[dict]) -> Optional[dict]:
        """Batch create records.

        Args:
            records: list of {"fields": {"FieldName": value, ...}}
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        return self.request(
            "POST",
            f"/apps/{app_token}/tables/{table_id}/records/batch_create",
            token,
            json_body={"records": records},
        )

    def update_records(self, chat_id: str, user_open_id: str,
                       app_token: str, table_id: str,
                       records: list[dict]) -> Optional[dict]:
        """Batch update records.

        Args:
            records: list of {"record_id": "...", "fields": {"FieldName": value}}
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        return self.request(
            "POST",
            f"/apps/{app_token}/tables/{table_id}/records/batch_update",
            token,
            json_body={"records": records},
        )

    def delete_records(self, chat_id: str, user_open_id: str,
                       app_token: str, table_id: str,
                       record_ids: list[str]) -> Optional[dict]:
        """Batch delete records."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        return self.request(
            "POST",
            f"/apps/{app_token}/tables/{table_id}/records/batch_delete",
            token,
            json_body={"records": record_ids},
        )


    def create_app(self, chat_id: str, user_open_id: str,
                   name: str, folder_token: str = None) -> Optional[dict]:
        """Create a new bitable app.

        Args:
            name: bitable app name
            folder_token: optional target folder
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        body = {"name": name}
        if folder_token:
            body["folder_token"] = folder_token

        return self.request("POST", "/apps", token, json_body=body)

    def create_table(self, chat_id: str, user_open_id: str,
                     app_token: str, name: str,
                     fields: list[dict] = None) -> Optional[dict]:
        """Create a new table in a bitable app.

        Args:
            name: table name
            fields: optional initial field definitions
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        table = {"name": name}
        if fields:
            table["fields"] = fields

        return self.request("POST", f"/apps/{app_token}/tables", token,
                            json_body={"table": table})

    def delete_table(self, chat_id: str, user_open_id: str,
                     app_token: str, table_id: str) -> Optional[dict]:
        """Delete a table from a bitable app."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        return self.request("DELETE", f"/apps/{app_token}/tables/{table_id}",
                            token)

    # -------------------------------------------------------------------
    # Field operations
    # -------------------------------------------------------------------

    def list_fields(self, chat_id: str, user_open_id: str,
                    app_token: str, table_id: str) -> Optional[dict]:
        """List all fields (columns) in a table."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        return self.request(
            "GET",
            f"/apps/{app_token}/tables/{table_id}/fields",
            token,
            params={"page_size": 100},
        )
