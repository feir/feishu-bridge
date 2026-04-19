"""Tests for feishu_bridge.workflows.done_workflow (Phase 6.7)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from feishu_bridge.workflows import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_WAITING_CONFIRMATION,
    DoneWorkflow,
    WorkflowContext,
)


class FakeRunner:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def run(self, prompt: str, **kwargs: Any) -> dict:
        self.calls.append({"prompt": prompt, **kwargs})
        if not self._responses:
            raise AssertionError("FakeRunner exhausted")
        return self._responses.pop(0)


class FakeJournal:
    def __init__(self, entries: list[dict]) -> None:
        self.entries = entries

    def read(self, bot_id, chat_id, thread_id):
        return list(self.entries)


def _extraction() -> dict:
    return {
        "title": "Implement bridge done workflow",
        "activities": [{
            "project": "feishu-bridge",
            "summary": "Added runner-neutral done workflow",
            "details": ["implemented DoneWorkflow with confirm-gated writes"],
        }],
        "decisions": [{
            "decision": "Write archives under AGENTS_HOME",
            "rationale": "Avoid hardcoded Claude memory paths for Pi",
        }],
        "lessons": [{
            "project": "feishu-bridge",
            "category": "PROCESS",
            "scope": "project",
            "title": "Keep bridge writes deterministic",
            "lesson": "Workflow model output should be validated before any file write occurs",
            "prevention": "Keep confirmation and deterministic write steps separated",
        }],
        "open_loops": [{"text": "Add apply mode hardening", "project": "feishu-bridge"}],
        "noise_filtered": 1,
    }


def _json_response(payload: dict) -> dict:
    return {"result": json.dumps(payload, ensure_ascii=False), "is_error": False}


def _ctx(tmp_path: Path, runner: FakeRunner, journal: FakeJournal) -> WorkflowContext:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    return WorkflowContext(
        bot_id="bot",
        chat_id="chat",
        thread_id=None,
        sender_id="user",
        chat_type="p2p",
        message_id="msg",
        workspace=workspace,
        runner=runner,
        runner_type="pi",
        handle=None,
        journal=journal,
        session_id="sid",
        agents_home=tmp_path / "agents",
        skill_dir=tmp_path / "skill",
    )


def test_done_start_extracts_and_waits_for_confirm(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_HOME", str(tmp_path / "agents"))
    journal = FakeJournal([
        {"kind": "user_turn", "text": "please implement /done"},
        {"kind": "assistant_turn", "text": "implemented tests and workflow"},
    ])
    runner = FakeRunner([_json_response(_extraction())])
    wf = DoneWorkflow(skill_dir=tmp_path / "skill", ttl_string="2h")

    result = wf.start(_ctx(tmp_path, runner, journal), "")

    assert result.state == STATE_WAITING_CONFIRMATION
    assert "/confirm" in result.user_message
    assert result.payload["extraction"]["title"] == _extraction()["title"]
    assert "please implement /done" in runner.calls[0]["prompt"]


def test_done_confirm_writes_agents_memory_and_project_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_HOME", str(tmp_path / "agents"))
    journal = FakeJournal([{"kind": "user_turn", "text": "done"}])
    runner = FakeRunner([])
    ctx = _ctx(tmp_path, runner, journal)
    wf = DoneWorkflow(skill_dir=tmp_path / "skill", ttl_string="2h")

    result = wf.resume_confirm(ctx, {"extraction": _extraction()})

    assert result.state == STATE_COMPLETED
    artifacts = result.payload["artifacts"]
    session_file = Path(artifacts["session"])
    lessons_file = Path(artifacts["lessons"])
    ctx_file = Path(artifacts["ctx"])
    assert session_file.is_file()
    assert lessons_file.is_file()
    assert ctx_file.is_file()
    assert str(session_file).startswith(str(tmp_path / "agents" / "memory" / "sessions"))
    assert str(lessons_file).startswith(str(tmp_path / "agents" / "memory" / "lessons"))
    assert ctx_file == ctx.workspace / ".agents" / "ctx" / "session-timeline.md"
    assert "Session 归档" in session_file.read_text(encoding="utf-8")
    assert "Keep bridge writes deterministic" in lessons_file.read_text(encoding="utf-8")


def test_done_confirm_revalidates_payload_before_write(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_HOME", str(tmp_path / "agents"))
    journal = FakeJournal([{"kind": "user_turn", "text": "done"}])
    ctx = _ctx(tmp_path, FakeRunner([]), journal)
    wf = DoneWorkflow(skill_dir=tmp_path / "skill", ttl_string="2h")

    broken = dict(_extraction())
    broken["activities"] = "not an array"
    result = wf.resume_confirm(ctx, {"extraction": broken})

    assert result.state == STATE_FAILED
    assert "写入失败" in result.user_message
    assert not (tmp_path / "agents" / "memory" / "sessions").exists()


def test_done_invalid_json_fails_closed(tmp_path):
    journal = FakeJournal([{"kind": "user_turn", "text": "work happened"}])
    runner = FakeRunner([
        {"result": "not json", "is_error": False},
        {"result": "still not json", "is_error": False},
        {"result": "nope", "is_error": False},
    ])
    wf = DoneWorkflow(skill_dir=tmp_path / "skill", ttl_string="2h")
    result = wf.start(_ctx(tmp_path, runner, journal), "")
    assert result.state == STATE_FAILED
    assert "未修改任何文件" in result.user_message
    assert len(runner.calls) == 3


def test_done_empty_journal_fails(tmp_path):
    runner = FakeRunner([])
    wf = DoneWorkflow(skill_dir=tmp_path / "skill", ttl_string="2h")
    result = wf.start(_ctx(tmp_path, runner, FakeJournal([])), "")
    assert result.state == STATE_FAILED
    assert "没有可归档" in result.user_message
