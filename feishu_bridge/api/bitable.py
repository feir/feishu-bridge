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
        "base:table:read",
        "base:table:create",
        "base:table:update",
        "base:table:delete",
        "base:record:retrieve",
        "base:record:create",
        "base:record:update",
        "base:record:delete",
        "base:field:read",
        "base:field:create",
        "base:field:update",
        "base:field:delete",
        "base:view:read",
        # Note: base:view:create/update/delete don't exist as Feishu scopes;
        # view CUD is covered by base:table:update.
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

    def copy_app(self, chat_id: str, user_open_id: str,
                 app_token: str, name: str = None,
                 folder_token: str = None) -> Optional[dict]:
        """Copy a bitable app.

        Args:
            app_token: source app to copy
            name: name for the copy (default: original name + " copy")
            folder_token: target folder
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        body = {}
        if name:
            body["name"] = name
        if folder_token:
            body["folder_token"] = folder_token

        return self.request("POST", f"/apps/{app_token}/copy", token,
                            json_body=body or None)

    def patch_table(self, chat_id: str, user_open_id: str,
                    app_token: str, table_id: str,
                    name: str) -> Optional[dict]:
        """Rename a table in a bitable app."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        return self.request("PATCH",
                            f"/apps/{app_token}/tables/{table_id}", token,
                            json_body={"table": {"name": name}})

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
                    app_token: str, table_id: str,
                    page_size: int = 100,
                    page_token: str = None) -> Optional[dict]:
        """List all fields (columns) in a table."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        params = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        return self.request(
            "GET",
            f"/apps/{app_token}/tables/{table_id}/fields",
            token,
            params=params,
        )

    def create_field(self, chat_id: str, user_open_id: str,
                     app_token: str, table_id: str,
                     field_name: str, field_type: int,
                     property_: dict = None) -> Optional[dict]:
        """Create a field (column) in a table.

        Args:
            field_name: display name
            field_type: type code (1=Text, 2=Number, 3=SingleSelect,
                4=MultiSelect, 5=DateTime, 7=Checkbox, 11=User, 13=Phone,
                15=URL, 17=Attachment, 18=Link, 20=Formula, 21=DuplexLink,
                22=Location, 23=GroupChat, 1001=CreatedTime,
                1002=LastModifiedTime, 1003=CreatedBy, 1004=LastModifiedBy,
                1005=AutoNumber)
            property_: type-specific config (e.g. options for select fields)
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        body = {"field_name": field_name, "type": field_type}
        if property_:
            body["property"] = property_

        return self.request(
            "POST",
            f"/apps/{app_token}/tables/{table_id}/fields",
            token, json_body=body)

    def update_field(self, chat_id: str, user_open_id: str,
                     app_token: str, table_id: str,
                     field_id: str,
                     field_name: str = None,
                     field_type: int = None,
                     property_: dict = None) -> Optional[dict]:
        """Update a field (rename, change type, or modify property).

        Args:
            field_id: field ID to update
            field_name: new display name
            field_type: new type code (see create_field for codes)
            property_: new type-specific config
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        body = {}
        if field_name is not None:
            body["field_name"] = field_name
        if field_type is not None:
            body["type"] = field_type
        if property_ is not None:
            body["property"] = property_

        if not body:
            raise ValueError("update_field requires at least one of: "
                             "field_name, field_type, property_")

        return self.request(
            "PUT",
            f"/apps/{app_token}/tables/{table_id}/fields/{field_id}",
            token, json_body=body)

    def delete_field(self, chat_id: str, user_open_id: str,
                     app_token: str, table_id: str,
                     field_id: str) -> Optional[dict]:
        """Delete a field (column) from a table."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        return self.request(
            "DELETE",
            f"/apps/{app_token}/tables/{table_id}/fields/{field_id}",
            token)

    # -------------------------------------------------------------------
    # View operations
    # -------------------------------------------------------------------

    def list_views(self, chat_id: str, user_open_id: str,
                   app_token: str, table_id: str,
                   page_size: int = 50,
                   page_token: str = None) -> Optional[dict]:
        """List views in a table."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        params = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token

        return self.request(
            "GET",
            f"/apps/{app_token}/tables/{table_id}/views",
            token, params=params)

    def get_view(self, chat_id: str, user_open_id: str,
                 app_token: str, table_id: str,
                 view_id: str) -> Optional[dict]:
        """Get a view's details."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        return self.request(
            "GET",
            f"/apps/{app_token}/tables/{table_id}/views/{view_id}",
            token)

    def create_view(self, chat_id: str, user_open_id: str,
                    app_token: str, table_id: str,
                    view_name: str,
                    view_type: str = "grid") -> Optional[dict]:
        """Create a view in a table.

        Args:
            view_name: display name
            view_type: "grid", "kanban", "gallery", "gantt", or "form"
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        return self.request(
            "POST",
            f"/apps/{app_token}/tables/{table_id}/views",
            token,
            json_body={"view_name": view_name, "view_type": view_type})

    def patch_view(self, chat_id: str, user_open_id: str,
                   app_token: str, table_id: str,
                   view_id: str, view_name: str) -> Optional[dict]:
        """Rename a view."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        return self.request(
            "PATCH",
            f"/apps/{app_token}/tables/{table_id}/views/{view_id}",
            token,
            json_body={"view_name": view_name})

    def delete_view(self, chat_id: str, user_open_id: str,
                    app_token: str, table_id: str,
                    view_id: str) -> Optional[dict]:
        """Delete a view from a table."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        return self.request(
            "DELETE",
            f"/apps/{app_token}/tables/{table_id}/views/{view_id}",
            token)
