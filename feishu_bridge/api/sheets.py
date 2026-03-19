"""
Feishu Sheets API wrapper — read/write spreadsheet data.

All operations require user_access_token (UAT).

Usage:
    sheets = FeishuSheets(app_id, app_secret, lark_client)
    data = sheets.read(chat_id, user_open_id, token="shtcnXXX", range="Sheet1!A1:D10")
    sheets.write(chat_id, user_open_id, token="shtcnXXX", range="Sheet1!A1", values=[[1,2],[3,4]])
"""

import logging
from typing import Optional
from urllib.parse import quote as _url_quote

from feishu_bridge.api.client import FeishuAPI

log = logging.getLogger("feishu-sheets")

DEFAULT_ROW_LIMIT = 200


class FeishuSheets(FeishuAPI):
    """Feishu Sheets read/write via OAPI v2/v3."""

    SCOPES = [
        "sheets:spreadsheet:readonly",
        "sheets:spreadsheet",
    ]
    # Sheets uses mixed v2/v3 endpoints
    BASE_PATH = ""

    def _v2(self, method: str, path: str, token: str,
            params: dict = None, json_body: dict = None) -> dict:
        """Sheets v2 request (different base path)."""
        full_path = f"/open-apis/sheets/v2{path}"
        return self.request(method, full_path, token,
                            params=params, json_body=json_body)

    def _v3(self, method: str, path: str, token: str,
            params: dict = None, json_body: dict = None) -> dict:
        """Sheets v3 request."""
        full_path = f"/open-apis/sheets/v3{path}"
        return self.request(method, full_path, token,
                            params=params, json_body=json_body)

    # -------------------------------------------------------------------
    # Read operations
    # -------------------------------------------------------------------

    def info(self, chat_id: str, user_open_id: str,
             spreadsheet_token: str) -> Optional[dict]:
        """Get spreadsheet metadata (title, sheets list, etc.)."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        meta = self._v3("GET", f"/spreadsheets/{spreadsheet_token}", token)
        sheets = self._v3(
            "GET", f"/spreadsheets/{spreadsheet_token}/sheets/query", token,
        )
        return {
            "spreadsheet": meta.get("spreadsheet", {}),
            "sheets": sheets.get("sheets", []),
        }

    def read(self, chat_id: str, user_open_id: str,
             spreadsheet_token: str, range_: str,
             render_option: str = "ToString") -> Optional[dict]:
        """Read cell values from a range.

        Args:
            spreadsheet_token: spreadsheet ID or URL
            range_: A1 notation (e.g., "Sheet1!A1:D10")
            render_option: "ToString" (default), "FormattedValue", "UnformattedValue"

        Returns:
            {"values": [[...], ...], "range": "...", ...}
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        return self._v2(
            "GET",
            f"/spreadsheets/{spreadsheet_token}/values/{_url_quote(range_, safe='!:')}",
            token,
            params={
                "valueRenderOption": render_option,
                "dateTimeRenderOption": "FormattedString",
            },
        )

    # -------------------------------------------------------------------
    # Write operations
    # -------------------------------------------------------------------

    def write(self, chat_id: str, user_open_id: str,
              spreadsheet_token: str, range_: str,
              values: list[list]) -> Optional[dict]:
        """Write values to a range (overwrites existing data).

        Args:
            range_: A1 notation target range
            values: 2D array of cell values
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        return self._v2(
            "PUT",
            f"/spreadsheets/{spreadsheet_token}/values",
            token,
            json_body={
                "valueRange": {
                    "range": range_,
                    "values": values,
                },
            },
        )

    def append(self, chat_id: str, user_open_id: str,
               spreadsheet_token: str, range_: str,
               values: list[list]) -> Optional[dict]:
        """Append rows after the last non-empty row in range.

        Args:
            range_: A1 notation (determines target sheet/columns)
            values: 2D array of rows to append
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        return self._v2(
            "POST",
            f"/spreadsheets/{spreadsheet_token}/values_append",
            token,
            json_body={
                "valueRange": {
                    "range": range_,
                    "values": values,
                },
            },
        )

    def create(self, chat_id: str, user_open_id: str,
               title: str, folder_token: str = None) -> Optional[dict]:
        """Create a new spreadsheet.

        Args:
            title: spreadsheet title
            folder_token: optional target folder
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        body = {"title": title}
        if folder_token:
            body["folder_token"] = folder_token

        return self._v3(
            "POST", "/spreadsheets", token,
            json_body={"spreadsheet": body},
        )

    def delete(self, chat_id: str, user_open_id: str,
               spreadsheet_token: str) -> Optional[dict]:
        """Delete a spreadsheet via Drive API.

        Args:
            spreadsheet_token: spreadsheet token

        Returns:
            Drive API response, or None on auth failure
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        return self._drive("DELETE", f"/files/{spreadsheet_token}?type=sheet", token)
