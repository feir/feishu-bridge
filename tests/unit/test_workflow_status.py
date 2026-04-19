"""Tests for /status workflow visibility (Phase 6.6)."""

from __future__ import annotations

import time

from feishu_bridge.commands import BridgeCommandHandler
from feishu_bridge.workflows import STATE_WAITING_CONFIRMATION, WorkflowRecord


class FakeHandle:
    def __init__(self):
        self.deliveries: list[tuple[str, bool]] = []

    def deliver(self, content, is_error=False, total_tokens=0):
        self.deliveries.append((content, is_error))


class FakeSessionMap:
    def __init__(self, sid=None):
        self.sid = sid

    def get(self, key):
        return self.sid


class FakeStorage:
    def __init__(self, record):
        self.record = record
        self.mark_called = False

    def mark_expired_waiting(self):
        self.mark_called = True
        return []

    def active_for_scope(self, scope_key):
        self.scope_key = scope_key
        return self.record


class FakeBot:
    def __init__(self, storage):
        self._workflow_storage = storage
        self.session_map = FakeSessionMap()
        self._session_cost = {}


def _item():
    return {
        "bot_id": "bot-1",
        "chat_id": "chat-1",
        "thread_id": None,
    }


def test_status_shows_active_workflow_without_active_session():
    record = WorkflowRecord(
        id="12345678-aaaa-bbbb-cccc-000000000000",
        scope_key="bot-1|chat-1|",
        skill_name="plan",
        state=STATE_WAITING_CONFIRMATION,
        payload={
            "draft": {"slug": "demo-change"},
            "current_step": "waiting_confirmation",
        },
        expires_at=time.time() + 3600,
    )
    storage = FakeStorage(record)
    handler = BridgeCommandHandler(FakeBot(storage))
    handle = FakeHandle()

    handler._handle_status(_item(), handle)

    content, is_error = handle.deliveries[0]
    assert is_error is False
    assert "当前没有活跃会话" in content
    assert "**Workflow**" in content
    assert "`/plan` `waiting_confirmation`" in content
    assert "demo-change" in content
    assert storage.mark_called is True
    assert storage.scope_key == "bot-1|chat-1|"


def test_status_without_workflow_keeps_existing_no_session_message():
    storage = FakeStorage(None)
    handler = BridgeCommandHandler(FakeBot(storage))
    handle = FakeHandle()

    handler._handle_status(_item(), handle)

    assert handle.deliveries == [("当前没有活跃会话。", False)]
