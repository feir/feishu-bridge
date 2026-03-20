"""
Feishu Calendar API wrapper — calendars and events CRUD.

All operations require user_access_token (UAT).

Usage:
    cal = FeishuCalendar(app_id, app_secret, lark_client)
    calendars = cal.list_calendars(chat_id, user_open_id)
"""

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from feishu_bridge.api.client import FeishuAPI

_TZ_CST = timezone(timedelta(hours=8))


def _to_unix_ts(value: str) -> str:
    """Convert time value to Unix timestamp string.

    Accepts:
      - Unix timestamp (digits only, 10 or 13 digits) — returned as-is (seconds)
      - RFC3339 / ISO8601 with tz — parsed and converted
      - "YYYY-MM-DD HH:MM[:SS]" — assumed Asia/Shanghai (UTC+8)
    """
    value = value.strip()
    # Already a unix timestamp
    if re.fullmatch(r"\d{10,13}", value):
        ts = int(value)
        return str(ts // 1000 if ts > 9_999_999_999 else ts)
    # Try ISO8601 / RFC3339 with timezone
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M%z",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            return str(int(dt.timestamp()))
        except ValueError:
            continue
    # Without timezone — assume UTC+8
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
    ):
        try:
            dt = datetime.strptime(value, fmt).replace(tzinfo=_TZ_CST)
            return str(int(dt.timestamp()))
        except ValueError:
            continue
    # Fallback: return as-is, let API report the error
    return value

log = logging.getLogger("feishu-calendar")


class FeishuCalendar(FeishuAPI):
    """Feishu Calendar CRUD via OAPI."""

    SCOPES = [
        "calendar:calendar",
        "calendar:calendar:readonly",
    ]
    BASE_PATH = "/open-apis/calendar/v4"

    def list_calendars(self, chat_id: str, user_open_id: str,
                       page_size: int = 50,
                       page_token: str = None) -> Optional[dict]:
        """List calendars the user has access to."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        # Feishu API requires page_size >= 50 for this endpoint
        params = {"page_size": max(page_size, 50)}
        if page_token:
            params["page_token"] = page_token

        return self.request("GET", "/calendars", token, params=params)

    def list_events(self, chat_id: str, user_open_id: str,
                    calendar_id: str, start_time: str, end_time: str,
                    page_size: int = 50,
                    page_token: str = None) -> Optional[dict]:
        """List events in a calendar within a time range.

        Args:
            calendar_id: calendar ID (use "primary" for user's main calendar)
            start_time: RFC3339 timestamp (e.g. "2026-03-19T00:00:00+08:00")
            end_time: RFC3339 timestamp
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        params = {
            "start_time": _to_unix_ts(start_time),
            "end_time": _to_unix_ts(end_time),
            "page_size": page_size,
        }
        if page_token:
            params["page_token"] = page_token

        return self.request("GET",
                            f"/calendars/{calendar_id}/events", token,
                            params=params)

    def get_event(self, chat_id: str, user_open_id: str,
                  calendar_id: str, event_id: str) -> Optional[dict]:
        """Get a single event's details."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        return self.request("GET",
                            f"/calendars/{calendar_id}/events/{event_id}",
                            token)

    def create_event(self, chat_id: str, user_open_id: str,
                     calendar_id: str, summary: str,
                     start_time: str, end_time: str,
                     description: str = None,
                     attendees: list[dict] = None) -> Optional[dict]:
        """Create a calendar event.

        Args:
            summary: event title
            start_time: RFC3339 timestamp
            end_time: RFC3339 timestamp
            description: optional event description
            attendees: list of {"type": "user", "user_id": open_id} dicts
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        body = {
            "summary": summary,
            "start_time": {"timestamp": _to_unix_ts(start_time)},
            "end_time": {"timestamp": _to_unix_ts(end_time)},
        }
        if description:
            body["description"] = description
        if attendees:
            body["attendees"] = attendees

        return self.request("POST",
                            f"/calendars/{calendar_id}/events", token,
                            json_body=body)

    def update_event(self, chat_id: str, user_open_id: str,
                     calendar_id: str, event_id: str,
                     **kwargs) -> Optional[dict]:
        """Update a calendar event.

        Accepts keyword args: summary, description, start_time, end_time, attendees.
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        body = {}
        if "summary" in kwargs:
            body["summary"] = kwargs["summary"]
        if "description" in kwargs:
            body["description"] = kwargs["description"]
        if "start_time" in kwargs:
            body["start_time"] = {"timestamp": _to_unix_ts(kwargs["start_time"])}
        if "end_time" in kwargs:
            body["end_time"] = {"timestamp": _to_unix_ts(kwargs["end_time"])}
        if "attendees" in kwargs:
            body["attendees"] = kwargs["attendees"]

        return self.request("PATCH",
                            f"/calendars/{calendar_id}/events/{event_id}",
                            token, json_body=body)

    def delete_event(self, chat_id: str, user_open_id: str,
                     calendar_id: str, event_id: str) -> Optional[dict]:
        """Delete a calendar event."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        return self.request("DELETE",
                            f"/calendars/{calendar_id}/events/{event_id}",
                            token)

    def reply_event(self, chat_id: str, user_open_id: str,
                    calendar_id: str, event_id: str,
                    status: str) -> Optional[dict]:
        """RSVP to a calendar event.

        Args:
            status: "accept", "decline", or "tentative"
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        body = {"status": status}
        return self.request(
            "POST",
            f"/calendars/{calendar_id}/events/{event_id}/rsvp",
            token,
            json_body=body,
        )

    # --- Recurring event instances ---

    def list_event_instances(self, chat_id: str, user_open_id: str,
                             calendar_id: str, event_id: str,
                             start_time: str, end_time: str,
                             page_size: int = 50,
                             page_token: str = None) -> Optional[dict]:
        """List instances of a recurring event within a time range.

        Args:
            event_id: recurring event ID
            start_time: RFC3339 timestamp
            end_time: RFC3339 timestamp (max 40-day window)
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        params = {
            "start_time": _to_unix_ts(start_time),
            "end_time": _to_unix_ts(end_time),
            "page_size": page_size,
        }
        if page_token:
            params["page_token"] = page_token

        return self.request(
            "GET",
            f"/calendars/{calendar_id}/events/{event_id}/instances",
            token, params=params)

    # --- Attendee management ---

    def list_attendees(self, chat_id: str, user_open_id: str,
                       calendar_id: str, event_id: str,
                       page_size: int = 50,
                       page_token: str = None) -> Optional[dict]:
        """List attendees of a calendar event."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        params = {"page_size": page_size, "user_id_type": "open_id"}
        if page_token:
            params["page_token"] = page_token

        return self.request(
            "GET",
            f"/calendars/{calendar_id}/events/{event_id}/attendees",
            token, params=params)

    def create_attendees(self, chat_id: str, user_open_id: str,
                         calendar_id: str, event_id: str,
                         attendees: list[dict]) -> Optional[dict]:
        """Add attendees to a calendar event.

        Args:
            attendees: list of attendee dicts, e.g.
                [{"type": "user", "user_id": "ou_xxx"},
                 {"type": "resource", "resource_id": "omm_xxx"},
                 {"type": "third_party", "third_party_email": "a@b.com"}]
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        return self.request(
            "POST",
            f"/calendars/{calendar_id}/events/{event_id}/attendees",
            token,
            json_body={"attendees": attendees},
            params={"user_id_type": "open_id"})

    def delete_attendees(self, chat_id: str, user_open_id: str,
                         calendar_id: str, event_id: str,
                         attendee_ids: list[str]) -> Optional[dict]:
        """Remove attendees from a calendar event.

        Args:
            attendee_ids: list of attendee_id strings (from list_attendees)
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        return self.request(
            "POST",
            f"/calendars/{calendar_id}/events/{event_id}"
            "/attendees/batch_delete",
            token,
            json_body={"attendee_ids": attendee_ids})

    # --- Free/busy ---

    def list_freebusy(self, chat_id: str, user_open_id: str,
                      user_ids: list[str],
                      start_time: str, end_time: str) -> Optional[dict]:
        """Query free/busy status for 1-10 users.

        Args:
            user_ids: list of user open_ids (max 10)
            start_time: RFC3339 timestamp
            end_time: RFC3339 timestamp
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return None

        body = {
            "time_min": _to_unix_ts(start_time),
            "time_max": _to_unix_ts(end_time),
            "user_ids": user_ids,
        }
        return self.request(
            "POST", "/freebusy/list", token,
            json_body=body,
            params={"user_id_type": "open_id"})
