"""Wiring tests for BridgeCommandHandler workflow dispatch (Phase 6.4).

Covers the seam between main.py's command-parse output (`_bridge_command`,
`_workflow_skill`, `_cmd_arg`) and the Phase 6.4 workflow handlers in
commands.py. The workflow internals (PlanWorkflow, WorkflowStorage) are
exercised by test_plan_workflow.py; this file only verifies that
`handle_bridge_command` dispatches correctly and threads the right fields
through to the handlers.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from feishu_bridge import commands as bridge_commands
from feishu_bridge.workflows.runtime import (
    STATE_COMPLETED,
    STATE_WAITING_CONFIRMATION,
    WorkflowContext,
    WorkflowResult,
)


class FakeHandle:
    def __init__(self, client, chat_id, thread_id, message_id, bot_id=None):
        self.client = client
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.source_message_id = message_id
        self.bot_id = bot_id
        self.deliveries: list[tuple[str, bool, int]] = []

    def send_processing_indicator(self):
        return True

    def stream_update(self, content):
        self.last_stream = content

    def deliver(self, content, is_error=False, total_tokens=0):
        self.deliveries.append((content, is_error, total_tokens))


class FakeBot:
    """Minimal FeishuBot stand-in for dispatcher tests."""

    def __init__(self):
        self.lark_client = object()
        self.bot_id = "bot-1"
        self.workspace = "/tmp/workspace"
        self.runner = object()
        self.session_map = None  # workflow handlers don't touch this unless building ctx
        self.command_policy = None
        self.agent_config = {"type": "pi"}
        self._workflow_storage = None


class FakeSessionMap:
    def get(self, key):
        return "sid-1"


class FakeJournal:
    def __init__(self):
        self.events: list[dict] = []
        self.artifacts: list[dict] = []

    def append_workflow_event(self, bot_id, chat_id, thread_id, *,
                              command, decision, runner_type, session_id=None):
        self.events.append({
            "bot_id": bot_id,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "command": command,
            "decision": decision,
            "runner_type": runner_type,
            "session_id": session_id,
        })

    def append_artifact(self, bot_id, chat_id, thread_id, *,
                        path, runner_type, session_id=None):
        self.artifacts.append({
            "bot_id": bot_id,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "path": path,
            "runner_type": runner_type,
            "session_id": session_id,
        })


def _install_fake_handle(monkeypatch):
    monkeypatch.setattr(bridge_commands, "ResponseHandle", FakeHandle)


def _make_item(cmd: str, *, skill: str | None = None, arg: str = "") -> dict:
    item = {
        "_bridge_command": cmd,
        "_cmd_arg": arg,
        "bot_id": "bot-1",
        "chat_id": "chat-1",
        "thread_id": None,
        "message_id": "msg-1",
        "sender_id": "user-1",
        "chat_type": "p2p",
    }
    if skill is not None:
        item["_workflow_skill"] = skill
    return item


# ---------- dispatch wiring ----------

def test_handle_bridge_command_dispatches_workflow_run(monkeypatch):
    _install_fake_handle(monkeypatch)
    handler = bridge_commands.BridgeCommandHandler(FakeBot())

    captured: dict[str, Any] = {}

    def fake_run(item, handle):
        captured["item"] = item
        captured["handle"] = handle
        handle.deliver("ran")

    monkeypatch.setattr(handler, "_handle_workflow_run", fake_run)

    item = _make_item("workflow-run", skill="plan", arg="build feature X")
    handler.handle_bridge_command(item)

    assert captured["item"] is item
    assert captured["item"]["_workflow_skill"] == "plan"
    assert captured["item"]["_cmd_arg"] == "build feature X"
    assert isinstance(captured["handle"], FakeHandle)
    assert captured["handle"].deliveries == [("ran", False, 0)]


def test_handle_bridge_command_dispatches_workflow_confirm(monkeypatch):
    _install_fake_handle(monkeypatch)
    handler = bridge_commands.BridgeCommandHandler(FakeBot())

    captured: dict[str, Any] = {}

    def fake_confirm(item, handle):
        captured["item"] = item
        handle.deliver("confirmed")

    monkeypatch.setattr(handler, "_handle_workflow_confirm", fake_confirm)

    item = _make_item("workflow-confirm")
    handler.handle_bridge_command(item)

    assert captured["item"] is item


def test_handle_bridge_command_workflow_unsupported_renders_reason(monkeypatch):
    handler = bridge_commands.BridgeCommandHandler(FakeBot())

    deliveries: list[tuple[str, bool, int]] = []

    class CapturingHandle(FakeHandle):
        def deliver(self, content, is_error=False, total_tokens=0):
            deliveries.append((content, is_error, total_tokens))

    monkeypatch.setattr(bridge_commands, "ResponseHandle", CapturingHandle)
    handler.handle_bridge_command(_make_item("workflow-unsupported", arg="/save|runner=pi"))

    assert len(deliveries) == 1
    content, is_error, _ = deliveries[0]
    assert is_error is True
    assert "/save" in content
    assert "runner=pi" in content


def test_stop_command_cancels_waiting_workflow(monkeypatch):
    _install_fake_handle(monkeypatch)
    handler = bridge_commands.BridgeCommandHandler(FakeBot())

    cancelled_for: list[dict] = []
    monkeypatch.setattr(
        handler,
        "_cancel_waiting_workflow",
        lambda item: (cancelled_for.append(item), True)[1],
    )

    item = _make_item("stop", arg="1|0")
    handler.handle_bridge_command(item)

    assert cancelled_for and cancelled_for[0] is item


# ---------- _handle_workflow_run validation ----------

def test_handle_workflow_run_rejects_missing_skill(monkeypatch):
    _install_fake_handle(monkeypatch)
    handler = bridge_commands.BridgeCommandHandler(FakeBot())

    handle = FakeHandle(None, "chat-1", None, "msg-1")
    handler._handle_workflow_run(_make_item("workflow-run", skill="", arg="goal"), handle)

    assert handle.deliveries
    content, is_error, _ = handle.deliveries[0]
    assert is_error is True
    assert "workflow-run" in content


def test_handle_workflow_run_rejects_unknown_skill(monkeypatch):
    _install_fake_handle(monkeypatch)
    bot = FakeBot()

    class _Policy:
        skills: dict = {}

    bot.command_policy = _Policy()
    handler = bridge_commands.BridgeCommandHandler(bot)

    handle = FakeHandle(None, "chat-1", None, "msg-1")
    handler._handle_workflow_run(
        _make_item("workflow-run", skill="nonexistent", arg="goal"), handle
    )

    content, is_error, _ = handle.deliveries[0]
    assert is_error is True
    assert "nonexistent" in content


def test_handle_workflow_run_rejects_non_plan_skill(monkeypatch):
    """Skills outside the implemented set still get an explicit phase message."""
    _install_fake_handle(monkeypatch)
    bot = FakeBot()

    class _Skill:
        source_dir = None
        ttl = "2h"
        name = "retro"

    class _Policy:
        skills = {"retro": _Skill()}

    bot.command_policy = _Policy()

    class _Storage:
        def mark_expired_waiting(self):
            pass

        def active_for_scope(self, scope):
            return None

    bot._workflow_storage = _Storage()

    handler = bridge_commands.BridgeCommandHandler(bot)
    handle = FakeHandle(None, "chat-1", None, "msg-1")
    handler._handle_workflow_run(
        _make_item("workflow-run", skill="retro", arg="archive"), handle
    )

    content, is_error, _ = handle.deliveries[0]
    assert is_error is True
    assert "尚未" in content


def test_handle_workflow_run_memory_gc_uses_workflow(monkeypatch, tmp_path):
    _install_fake_handle(monkeypatch)
    import feishu_bridge.workflows as workflows

    class _Skill:
        source_dir = tmp_path
        ttl = "24h"
        name = "memory-gc"

    class _Policy:
        skills = {"memory-gc": _Skill()}

    class _Storage:
        def mark_expired_waiting(self):
            pass

        def active_for_scope(self, scope):
            return None

    class _MemoryGcWorkflow:
        def __init__(self, *, skill_dir, ttl_string):
            self.skill_dir = skill_dir
            self.ttl_string = ttl_string

        def start(self, ctx, goal):
            return WorkflowResult(
                state=STATE_COMPLETED,
                user_message=f"gc {goal}",
                payload={"dry_run": True},
            )

    monkeypatch.setattr(workflows, "MemoryGcWorkflow", _MemoryGcWorkflow)
    bot = FakeBot()
    bot.session_map = FakeSessionMap()
    bot.command_policy = _Policy()
    bot._workflow_storage = _Storage()

    handler = bridge_commands.BridgeCommandHandler(bot)
    handle = FakeHandle(None, "chat-1", None, "msg-1")
    handler._handle_workflow_run(
        _make_item("workflow-run", skill="memory-gc", arg="--dry-run"), handle
    )

    assert handle.deliveries == [("gc --dry-run", False, 0)]


def test_handle_workflow_run_memory_gc_rejects_group_non_owner(monkeypatch, tmp_path):
    _install_fake_handle(monkeypatch)

    class _Skill:
        source_dir = tmp_path
        ttl = "24h"
        name = "memory-gc"

    class _Policy:
        skills = {"memory-gc": _Skill()}

    bot = FakeBot()
    bot.command_policy = _Policy()
    bot.allowed_users = ["*"]
    bot._group_owner = "owner-1"
    bot.session_map = FakeSessionMap()

    handler = bridge_commands.BridgeCommandHandler(bot)
    handle = FakeHandle(None, "chat-1", None, "msg-1")
    item = _make_item("workflow-run", skill="memory-gc", arg="--dry-run")
    item["chat_type"] = "group"
    item["sender_id"] = "user-1"
    handler._handle_workflow_run(item, handle)

    content, is_error, _ = handle.deliveries[0]
    assert is_error is True
    assert "仅群主" in content


def test_handle_workflow_run_rejects_when_another_active(monkeypatch):
    """One-active-per-scope invariant — /plan fails if a waiter already exists."""
    _install_fake_handle(monkeypatch)
    bot = FakeBot()

    class _Skill:
        source_dir = None
        ttl = "7d"
        name = "plan"

    class _Policy:
        skills = {"plan": _Skill()}

    bot.command_policy = _Policy()

    class _Record:
        skill_name = "plan"

    class _Storage:
        def __init__(self):
            self.seen_scopes: list[str] = []

        def mark_expired_waiting(self):
            pass

        def active_for_scope(self, scope):
            self.seen_scopes.append(scope)
            return _Record()

    storage = _Storage()
    bot._workflow_storage = storage

    handler = bridge_commands.BridgeCommandHandler(bot)
    handle = FakeHandle(None, "chat-1", None, "msg-1")
    handler._handle_workflow_run(
        _make_item("workflow-run", skill="plan", arg="goal"), handle
    )

    content, is_error, _ = handle.deliveries[0]
    assert is_error is True
    assert "plan" in content  # references skill name in error
    # scope_key format matches WorkflowContext.scope_key literal.
    assert storage.seen_scopes == ["bot-1|chat-1|"]


def test_handle_workflow_run_journals_waiting_state(monkeypatch, tmp_path):
    """Bridge-owned workflow starts must be visible in the session journal."""
    _install_fake_handle(monkeypatch)
    import feishu_bridge.worker as bridge_worker
    import feishu_bridge.workflows as workflows

    journal = FakeJournal()
    monkeypatch.setattr(bridge_worker, "_session_journal", journal)

    class _Skill:
        source_dir = tmp_path
        ttl = "7d"
        name = "plan"

    class _Policy:
        skills = {"plan": _Skill()}

    class _Storage:
        def mark_expired_waiting(self):
            pass

        def active_for_scope(self, scope):
            return None

        def create(self, **kwargs):
            self.created = kwargs

    class _PlanWorkflow:
        def __init__(self, *, skill_dir, ttl_string):
            pass

        def start(self, ctx, goal):
            return WorkflowResult(
                state=STATE_WAITING_CONFIRMATION,
                user_message="draft",
                expires_at=time.time() + 60,
                payload={"goal": goal},
            )

    monkeypatch.setattr(workflows, "PlanWorkflow", _PlanWorkflow)
    bot = FakeBot()
    bot.session_map = FakeSessionMap()
    bot.command_policy = _Policy()
    bot._workflow_storage = _Storage()

    handler = bridge_commands.BridgeCommandHandler(bot)
    handle = FakeHandle(None, "chat-1", None, "msg-1")
    handler._handle_workflow_run(
        _make_item("workflow-run", skill="plan", arg="goal"), handle
    )

    assert journal.events == [{
        "bot_id": "bot-1",
        "chat_id": "chat-1",
        "thread_id": None,
        "command": "/plan",
        "decision": "start:waiting_confirmation",
        "runner_type": "pi",
        "session_id": "sid-1",
    }]
    assert journal.artifacts == []


def test_handle_workflow_confirm_journals_artifacts(monkeypatch, tmp_path):
    """Confirmed workflow artifact paths must be journal-visible for /done."""
    _install_fake_handle(monkeypatch)
    import feishu_bridge.worker as bridge_worker
    import feishu_bridge.workflows as workflows

    journal = FakeJournal()
    monkeypatch.setattr(bridge_worker, "_session_journal", journal)

    class _Skill:
        source_dir = tmp_path
        ttl = "7d"
        name = "plan"

    class _Policy:
        skills = {"plan": _Skill()}

    class _Record:
        id = 7
        skill_name = "plan"
        payload = {"draft": {"slug": "demo"}}
        is_waiting = True

    class _Storage:
        def mark_expired_waiting(self):
            pass

        def active_for_scope(self, scope):
            return _Record()

        def update(self, *args, **kwargs):
            self.updated = (args, kwargs)

    class _PlanWorkflow:
        def __init__(self, *, skill_dir, ttl_string):
            pass

        def resume_confirm(self, ctx, payload):
            return WorkflowResult(
                state=STATE_COMPLETED,
                user_message="done",
                artifacts=[str(tmp_path / "proposal.md"), str(tmp_path / "tasks.md")],
                payload={**payload, "artifacts": {"proposal": "p", "tasks": "t"}},
            )

    monkeypatch.setattr(workflows, "PlanWorkflow", _PlanWorkflow)
    bot = FakeBot()
    bot.session_map = FakeSessionMap()
    bot.command_policy = _Policy()
    bot._workflow_storage = _Storage()

    handler = bridge_commands.BridgeCommandHandler(bot)
    handle = FakeHandle(None, "chat-1", None, "msg-1")
    handler._handle_workflow_confirm(_make_item("workflow-confirm"), handle)

    assert journal.events[0]["command"] == "/plan"
    assert journal.events[0]["decision"] == "confirm:completed"
    assert [a["path"] for a in journal.artifacts] == [
        str(tmp_path / "proposal.md"),
        str(tmp_path / "tasks.md"),
    ]
    assert all(a["session_id"] == "sid-1" for a in journal.artifacts)


# ---------- scope_key invariant ----------

def test_scope_key_from_item_matches_workflow_context():
    """Lock the two scope_key formats together so they drift as a unit."""
    item = _make_item("workflow-run", skill="plan", arg="x")
    item["thread_id"] = "thread-99"

    ctx = WorkflowContext(
        bot_id=item["bot_id"],
        chat_id=item["chat_id"],
        thread_id=item["thread_id"],
        sender_id=item["sender_id"],
        chat_type=item["chat_type"],
        message_id=item["message_id"],
        workspace=Path("/tmp/workspace"),
        runner=object(),
        runner_type="pi",
        handle=object(),
        journal=None,
        session_id=None,
        agents_home=Path("/tmp/agents"),
        skill_dir=Path("/tmp/agents/skills/plan"),
    )

    assert bridge_commands._scope_key_from_item(item) == ctx.scope_key

    # And with no thread_id (p2p case).
    item2 = _make_item("workflow-run", skill="plan", arg="x")
    ctx2 = WorkflowContext(
        bot_id=item2["bot_id"],
        chat_id=item2["chat_id"],
        thread_id=item2["thread_id"],
        sender_id=item2["sender_id"],
        chat_type=item2["chat_type"],
        message_id=item2["message_id"],
        workspace=Path("/tmp/workspace"),
        runner=object(),
        runner_type="pi",
        handle=object(),
        journal=None,
        session_id=None,
        agents_home=Path("/tmp/agents"),
        skill_dir=Path("/tmp/agents/skills/plan"),
    )
    assert bridge_commands._scope_key_from_item(item2) == ctx2.scope_key
