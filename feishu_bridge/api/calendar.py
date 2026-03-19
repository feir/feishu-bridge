"""
Feishu Calendar API wrapper — calendars and events CRUD.

All operations require user_access_token (UAT).

Usage:
    cal = FeishuCalendar(app_id, app_secret, lark_client)
    calendars = cal.list_calendars(chat_id, user_open_id)
"""

import logging
from typing import Optional

from feishu_bridge.api.client import FeishuAPI

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

        params = {"page_size": page_size}
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
            "start_time": start_time,
            "end_time": end_time,
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
            "start_time": {"timestamp": start_time},
            "end_time": {"timestamp": end_time},
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
            body["start_time"] = {"timestamp": kwargs["start_time"]}
        if "end_time" in kwargs:
            body["end_time"] = {"timestamp": kwargs["end_time"]}
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
