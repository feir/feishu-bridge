"""
Feishu Calendar API wrapper — read/write calendar events.

All operations require user_access_token (UAT).

Usage:
    cal = FeishuCalendar(app_id, app_secret, lark_client)
    events = cal.list_events(chat_id, user_open_id, start="2026-06-20T00:00+08:00", end="2026-06-21T00:00+08:00")
    event = cal.get_event(chat_id, user_open_id, event_id="xxx")
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from feishu_bridge.api.client import FeishuAPI

log = logging.getLogger("feishu-calendar")

TZ_SHANGHAI = timezone(timedelta(hours=8))


class FeishuCalendar(FeishuAPI):
    """Feishu Calendar API v4 wrapper."""

    SCOPES = [
        "calendar:calendar:readonly",
        "calendar:calendar",
    ]
    BASE_PATH = "/open-apis/calendar/v4"

    # -------------------------------------------------------------------
    # Dispatch (Phase 1: read-only)
    # -------------------------------------------------------------------

    _READ_ACTIONS = {
        "agenda", "get_event", "search_events",
        "list_calendars", "freebusy", "primary_calendar",
    }

    def dispatch(self, action: str, chat_id: str, sender_id: str,
                 **kwargs) -> dict:
        """统一入口，归一化返回 {ok, data/error}."""
        try:
            if action not in self._READ_ACTIONS:
                return {"ok": False, "error": "unsupported_action",
                        "message": f"Phase 1 仅支持只读操作: "
                        f"{', '.join(sorted(self._READ_ACTIONS))}"}

            if action == "agenda":
                result = self.agenda(
                    chat_id, sender_id,
                    start=kwargs.get("start"),
                    end=kwargs.get("end"),
                    calendar_id=kwargs.get("calendar_id"),
                )
            elif action == "get_event":
                result = self.get_event(
                    chat_id, sender_id,
                    kwargs.get("event_id", ""),
                    calendar_id=kwargs.get("calendar_id"),
                )
            elif action == "search_events":
                result = self.search_events(
                    chat_id, sender_id,
                    query=kwargs.get("query", ""),
                    start=kwargs.get("start"),
                    end=kwargs.get("end"),
                    calendar_id=kwargs.get("calendar_id"),
                )
            elif action == "list_calendars":
                result = self.list_calendars(chat_id, sender_id)
            elif action == "freebusy":
                result = self.freebusy(
                    chat_id, sender_id,
                    start=kwargs.get("start", ""),
                    end=kwargs.get("end", ""),
                    user_ids=kwargs.get("user_ids"),
                )
            elif action == "primary_calendar":
                result = self.primary_calendar(chat_id, sender_id)
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
            log.exception("Calendar dispatch error: action=%s", action)
            return {"ok": False, "error": "internal_error",
                    "message": str(e)}

    # -------------------------------------------------------------------
    # Event read operations
    # -------------------------------------------------------------------

    def agenda(self, chat_id: str, user_open_id: str,
               start: str = None, end: str = None,
               calendar_id: str = None, *,
               page_size: int = 100,
               page_token: str = None) -> Optional[dict]:
        """List calendar events in a time range.

        Defaults to today (00:00–23:59 Asia/Shanghai) if start/end not given.

        Args:
            start: Unix timestamp (string) or ISO 8601
            end: Unix timestamp (string) or ISO 8601
            calendar_id: calendar ID (defaults to primary)
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        if not calendar_id:
            cal = self._get_primary_calendar(token)
            if cal is None:
                return {"error": "no_primary_calendar",
                        "message": "无法获取主日历"}
            calendar_id = cal.get("calendar_id", "")

        # Normalize to Unix timestamps (API requires integer strings)
        start = self._to_timestamp(start, default_hour=0)
        end = self._to_timestamp(end, default_hour=23, default_minute=59, default_second=59)

        params = {
            "page_size": page_size,
            "start_time": start,
            "end_time": end,
        }
        if page_token:
            params["page_token"] = page_token

        all_items = []
        for _ in range(10):  # max 10 pages
            data = self.request(
                "GET", f"/calendars/{calendar_id}/events", token,
                params=params,
            )
            items = data.get("items", [])
            # Filter cancelled events
            items = [i for i in items if i.get("status") != "cancelled"]
            all_items.extend(items)

            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break
            params["page_token"] = page_token

        # Sort by start time
        all_items.sort(key=lambda e: e.get("start_time", {}).get("timestamp", "0"))

        return {"items": all_items, "calendar_id": calendar_id}

    def get_event(self, chat_id: str, user_open_id: str,
                  event_id: str, calendar_id: str = None) -> Optional[dict]:
        """Get a single event by ID."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        if not calendar_id:
            cal = self._get_primary_calendar(token)
            if cal is None:
                return {"error": "no_primary_calendar"}
            calendar_id = cal.get("calendar_id", "")

        return self.request(
            "GET", f"/calendars/{calendar_id}/events/{event_id}", token,
        )

    def search_events(self, chat_id: str, user_open_id: str,
                      query: str = "", start: str = None, end: str = None,
                      calendar_id: str = None,
                      page_size: int = 100) -> Optional[dict]:
        """Search events by keyword and optional time range."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        if not calendar_id:
            cal = self._get_primary_calendar(token)
            if cal is None:
                return {"error": "no_primary_calendar"}
            calendar_id = cal.get("calendar_id", "")

        body = {"query": query, "page_size": page_size}
        if start:
            body["filter"] = body.get("filter", {})
            body["filter"]["start_time"] = start
        if end:
            body["filter"] = body.get("filter", {})
            body["filter"]["end_time"] = end

        return self.request(
            "POST", f"/calendars/{calendar_id}/events/search", token,
            json_body=body,
        )

    # -------------------------------------------------------------------
    # Calendar read operations
    # -------------------------------------------------------------------

    def list_calendars(self, chat_id: str, user_open_id: str,
                       page_size: int = 50) -> Optional[dict]:
        """List all calendars the user has access to."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        all_items = []
        page_token = None
        for _ in range(10):
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token

            data = self.request(
                "GET", "/calendars", token, params=params,
            )
            items = data.get("items", [])
            all_items.extend(items)

            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break

        return {"items": all_items}

    def primary_calendar(self, chat_id: str, user_open_id: str) -> Optional[dict]:
        """Get primary calendar info."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None
        return self.request("POST", "/calendars/primary", token, json_body={})

    def freebusy(self, chat_id: str, user_open_id: str,
                 start: str, end: str,
                 user_ids: list[str] = None) -> Optional[dict]:
        """Query free/busy status for users in a time range.

        Args:
            start: ISO 8601 start time
            end: ISO 8601 end time
            user_ids: list of user open_ids; defaults to current user
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        body = {
            "time_min": start,
            "time_max": end,
            "user_id": user_ids[0] if user_ids and len(user_ids) == 1 else user_open_id,
        }

        return self.request("POST", "/freebusy/list", token, json_body=body)

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _to_timestamp(value: str = None, *, default_hour: int = 0,
                     default_minute: int = 0, default_second: int = 0) -> str:
        """Normalize time input to Unix timestamp string.

        Accepts ISO 8601 or existing integer timestamps. Returns
        today's boundary if value is None.
        """
        if value is None:
            now = datetime.now(TZ_SHANGHAI)
            dt = now.replace(hour=default_hour, minute=default_minute,
                             second=default_second, microsecond=0)
            return str(int(dt.timestamp()))
        # Already a numeric timestamp
        if isinstance(value, str) and value.lstrip('-').isdigit():
            return value
        # ISO 8601 → timestamp
        try:
            from datetime import datetime as _dt
            # Handle timezone offset in ISO format
            val = value.replace('Z', '+00:00')
            dt = _dt.fromisoformat(val)
            return str(int(dt.timestamp()))
        except (ValueError, TypeError):
            return value  # pass through as-is, let API reject

    def _get_primary_calendar(self, token: str) -> Optional[dict]:
        """Resolve primary calendar ID.

        Returns dict with 'calendar_id' key, or None.
        """
        try:
            data = self.request("POST", "/calendars/primary", token, json_body={})
            calendars = data.get("calendars", [])
            if not calendars:
                return None
            return calendars[0].get("calendar", calendars[0])
        except Exception:
            log.warning("Failed to resolve primary calendar", exc_info=True)
            return None
