"""Tests for feishu_bridge.workflows.plan_workflow (Phase 6.4)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from feishu_bridge.workflows import (
    STATE_CANCELLED,
    STATE_COMPLETED,
    STATE_EXPIRED,
    STATE_FAILED,
    STATE_WAITING_CONFIRMATION,
    JsonPolicyError,
    PlanWorkflow,
    WorkflowContext,
    WorkflowResult,
    WorkflowStorage,
    parse_ttl_seconds,
    request_json_with_policy,
)


# ---------- helpers ----------

PLAN_SKILL_DIR = Path.home() / ".agents" / "skills" / "plan"


def _valid_draft() -> dict:
    return {
        "slug": "test-slug",
        "title": "Test plan",
        "branch": "main",
        "scope": "Standard",
        "why": "why block",
        "what": "what block",
        "not": "not block",
        "risks": "risks block",
        "tasks": [
            {"phase": "scaffold", "items": ["write docs", "open PR"]},
            {"phase": "verify", "items": ["run tests"]},
        ],
    }


class FakeRunner:
    """Runner stub: returns scripted `.run()` responses one at a time."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def run(self, prompt: str, **kwargs: Any) -> dict:
        self.calls.append({"prompt": prompt, **kwargs})
        if not self._responses:
            raise AssertionError("FakeRunner exhausted")
        return self._responses.pop(0)


def _make_ctx(
    tmp_path: Path, runner: FakeRunner,
    *, bot_id: str = "bot", chat_id: str = "chat",
    thread_id: str | None = None,
) -> WorkflowContext:
    return WorkflowContext(
        bot_id=bot_id,
        chat_id=chat_id,
        thread_id=thread_id,
        sender_id="tester",
        chat_type="p2p",
        message_id="msg-1",
        workspace=tmp_path,
        runner=runner,
        runner_type="pi",
        handle=None,
        journal=None,
        session_id=None,
        agents_home=Path.home() / ".agents",
        skill_dir=PLAN_SKILL_DIR,
    )


def _mk_json_response(payload: dict) -> dict:
    return {
        "result": json.dumps(payload, ensure_ascii=False),
        "is_error": False,
    }


# ---------- runtime helpers ----------

def test_parse_ttl_seconds_variants():
    assert parse_ttl_seconds("7d", 123) == 7 * 86400
    assert parse_ttl_seconds("30m", 0) == 30 * 60
    assert parse_ttl_seconds("  12h ", 0) == 12 * 3600
    assert parse_ttl_seconds("99", 0) == 99  # bare digits = seconds
    assert parse_ttl_seconds("", 77) == 77
    assert parse_ttl_seconds(None, 42) == 42
    assert parse_ttl_seconds("bogus", 5) == 5


def test_request_json_policy_happy_path():
    runner = FakeRunner([_mk_json_response(_valid_draft())])
    out = request_json_with_policy(
        runner_call=runner.run,
        base_prompt="draft please",
        schema_required=["slug", "title"],
    )
    assert out["slug"] == "test-slug"
    assert len(runner.calls) == 1


def test_request_json_policy_retry_then_success():
    runner = FakeRunner([
        {"result": "nope not json", "is_error": False},
        _mk_json_response(_valid_draft()),
    ])
    out = request_json_with_policy(
        runner_call=runner.run,
        base_prompt="draft please",
        schema_required=["slug"],
    )
    assert out["slug"] == "test-slug"
    assert len(runner.calls) == 2
    # Second attempt prompt mentions the previous parse error.
    assert "JSON" in runner.calls[1]["prompt"]


def test_request_json_policy_exhausted():
    runner = FakeRunner([
        {"result": "nope", "is_error": False},
        {"result": "still nope", "is_error": False},
        {"result": "STILL no json", "is_error": False},
    ])
    with pytest.raises(JsonPolicyError):
        request_json_with_policy(
            runner_call=runner.run,
            base_prompt="x",
            schema_required=["slug"],
        )
    assert len(runner.calls) == 3


def test_request_json_policy_fenced_block_on_third_attempt():
    fenced = "noise before\n```json\n" + json.dumps(_valid_draft()) + "\n```\ntrailing"
    runner = FakeRunner([
        {"result": "plain text", "is_error": False},
        {"result": "plain text again", "is_error": False},
        {"result": fenced, "is_error": False},
    ])
    out = request_json_with_policy(
        runner_call=runner.run,
        base_prompt="x",
        schema_required=["slug"],
    )
    assert out["slug"] == "test-slug"


# ---------- PlanWorkflow.start ----------

def test_plan_start_empty_arg(tmp_path):
    wf = PlanWorkflow(skill_dir=PLAN_SKILL_DIR, ttl_string="7d")
    ctx = _make_ctx(tmp_path, FakeRunner([]))
    result = wf.start(ctx, "")
    assert result.state == STATE_FAILED
    assert "需要提供目标描述" in result.user_message


def test_plan_start_success_waits_for_confirm(tmp_path):
    runner = FakeRunner([_mk_json_response(_valid_draft())])
    wf = PlanWorkflow(skill_dir=PLAN_SKILL_DIR, ttl_string="7d")
    ctx = _make_ctx(tmp_path, runner)
    result = wf.start(ctx, "refactor the auth layer")
    assert result.state == STATE_WAITING_CONFIRMATION
    assert result.expires_at is not None
    assert result.expires_at > time.time()
    assert result.payload["draft"]["slug"] == "test-slug"
    assert result.payload["goal"] == "refactor the auth layer"
    assert "specs_root" in result.payload
    assert "/confirm" in result.user_message
    assert "/stop" in result.user_message


def test_plan_start_json_policy_failure(tmp_path):
    bad = {"result": "not json", "is_error": False}
    runner = FakeRunner([bad, bad, bad])
    wf = PlanWorkflow(skill_dir=PLAN_SKILL_DIR, ttl_string="7d")
    ctx = _make_ctx(tmp_path, runner)
    result = wf.start(ctx, "do something")
    assert result.state == STATE_FAILED
    assert "草拟失败" in result.user_message
    assert len(runner.calls) == 3


def test_plan_start_max_changes_rejects(tmp_path):
    # Pre-create 3 active changes; spec-resolve should report slots_remaining=0.
    # Workspace needs a git repo OR we rely on non-git fallback at tmp_path/.specs.
    specs_changes = tmp_path / ".specs" / "changes"
    for slug in ("alpha", "beta", "gamma"):
        d = specs_changes / slug
        d.mkdir(parents=True)
        (d / "proposal.md").write_text(
            "---\nbranch: main\nstatus: active\nscope: Standard\n---\n# " + slug,
            encoding="utf-8",
        )
    wf = PlanWorkflow(skill_dir=PLAN_SKILL_DIR, ttl_string="7d")
    # Don't supply runner responses — slot check happens before runner call.
    ctx = _make_ctx(tmp_path, FakeRunner([]))
    result = wf.start(ctx, "anything")
    assert result.state == STATE_FAILED
    assert "3 个 active change 上限" in result.user_message


# ---------- PlanWorkflow.resume_confirm / resume_cancel ----------

def test_plan_resume_confirm_writes_files(tmp_path):
    specs_root = tmp_path / ".specs"
    payload = {
        "draft": _valid_draft(),
        "specs_root": str(specs_root),
        "start_sha": "abcdef1",
        "goal": "test goal",
    }
    wf = PlanWorkflow(skill_dir=PLAN_SKILL_DIR, ttl_string="7d")
    ctx = _make_ctx(tmp_path, FakeRunner([]))
    result = wf.resume_confirm(ctx, payload)
    assert result.state == STATE_COMPLETED
    proposal = specs_root / "changes" / "test-slug" / "proposal.md"
    tasks = specs_root / "changes" / "test-slug" / "tasks.md"
    assert proposal.exists()
    assert tasks.exists()
    ptext = proposal.read_text(encoding="utf-8")
    assert "start-sha: abcdef1" in ptext
    assert "## WHY" in ptext
    ttext = tasks.read_text(encoding="utf-8")
    assert "T1.1" in ttext
    assert "## Spec-Check" in ttext


def test_plan_resume_confirm_refuses_overwrite(tmp_path):
    specs_root = tmp_path / ".specs"
    target_dir = specs_root / "changes" / "test-slug"
    target_dir.mkdir(parents=True)
    (target_dir / "proposal.md").write_text("existing", encoding="utf-8")
    payload = {
        "draft": _valid_draft(),
        "specs_root": str(specs_root),
        "start_sha": "deadbeef",
    }
    wf = PlanWorkflow(skill_dir=PLAN_SKILL_DIR, ttl_string="7d")
    ctx = _make_ctx(tmp_path, FakeRunner([]))
    result = wf.resume_confirm(ctx, payload)
    assert result.state == STATE_FAILED
    assert "refuse to overwrite" in (result.error or "")


def test_plan_resume_confirm_broken_payload(tmp_path):
    wf = PlanWorkflow(skill_dir=PLAN_SKILL_DIR, ttl_string="7d")
    ctx = _make_ctx(tmp_path, FakeRunner([]))
    result = wf.resume_confirm(ctx, {})
    assert result.state == STATE_FAILED
    assert "草稿状态损坏" in result.user_message


def test_plan_resume_cancel(tmp_path):
    wf = PlanWorkflow(skill_dir=PLAN_SKILL_DIR, ttl_string="7d")
    ctx = _make_ctx(tmp_path, FakeRunner([]))
    result = wf.resume_cancel(ctx, {"draft": {"slug": "abc"}})
    assert result.state == STATE_CANCELLED
    assert "abc" in result.user_message


# ---------- WorkflowStorage ----------

def test_storage_create_and_lookup(tmp_path):
    storage = WorkflowStorage(db_path=tmp_path / "wf.db")
    try:
        rec = storage.create(
            scope_key="a|b|", skill_name="plan",
            payload={"foo": 1}, ttl_seconds=60,
            state=STATE_WAITING_CONFIRMATION,
        )
        assert rec.is_waiting
        fetched = storage.get(rec.id)
        assert fetched is not None
        assert fetched.payload == {"foo": 1}
        active = storage.active_for_scope("a|b|")
        assert active is not None
        assert active.id == rec.id
    finally:
        storage.close()


def test_storage_active_for_scope_excludes_terminal(tmp_path):
    storage = WorkflowStorage(db_path=tmp_path / "wf.db")
    try:
        rec = storage.create(
            scope_key="a|b|", skill_name="plan", payload={},
            ttl_seconds=60, state=STATE_WAITING_CONFIRMATION,
        )
        storage.update(rec.id, state=STATE_COMPLETED)
        assert storage.active_for_scope("a|b|") is None
        # list_for_scope still shows history
        history = storage.list_for_scope("a|b|")
        assert len(history) == 1
        assert history[0].state == STATE_COMPLETED
    finally:
        storage.close()


def test_storage_mark_expired_waiting(tmp_path):
    storage = WorkflowStorage(db_path=tmp_path / "wf.db")
    try:
        # Create waiting row with past expiry.
        past = time.time() - 3600
        rec = storage.create(
            scope_key="s1", skill_name="plan", payload={},
            ttl_seconds=0, state=STATE_WAITING_CONFIRMATION, now=past,
        )
        expired_ids = storage.mark_expired_waiting()
        assert rec.id in expired_ids
        after = storage.get(rec.id)
        assert after.state == STATE_EXPIRED
        # Should no longer show as active.
        assert storage.active_for_scope("s1") is None
    finally:
        storage.close()


def test_storage_prune_terminal_retention(tmp_path):
    from feishu_bridge.workflows import TERMINAL_RETENTION_SECONDS
    storage = WorkflowStorage(db_path=tmp_path / "wf.db")
    try:
        # Old terminal row — should be pruned.
        old_time = time.time() - TERMINAL_RETENTION_SECONDS - 100
        rec = storage.create(
            scope_key="s", skill_name="plan", payload={},
            ttl_seconds=60, state=STATE_WAITING_CONFIRMATION, now=old_time,
        )
        storage.update(rec.id, state=STATE_COMPLETED, now=old_time)
        # Fresh terminal row — should survive.
        rec2 = storage.create(
            scope_key="s", skill_name="plan", payload={},
            ttl_seconds=60, state=STATE_WAITING_CONFIRMATION,
        )
        storage.update(rec2.id, state=STATE_COMPLETED)
        pruned = storage.prune_terminal()
        assert pruned == 1
        assert storage.get(rec.id) is None
        assert storage.get(rec2.id) is not None
    finally:
        storage.close()
