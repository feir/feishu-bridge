"""Tests for feishu_bridge.workflows.memory_gc_workflow (Phase 6.5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from feishu_bridge.workflows import (
    STATE_COMPLETED,
    STATE_FAILED,
    MemoryGcWorkflow,
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


def _make_skill(tmp_path: Path, stats: dict | str) -> Path:
    skill_dir = tmp_path / "skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    payload = stats if isinstance(stats, str) else json.dumps(stats)
    script = scripts / "memory-gc-stats.sh"
    script.write_text(
        "#!/bin/bash\n"
        "cat <<'JSON'\n"
        f"{payload}\n"
        "JSON\n",
        encoding="utf-8",
    )
    return skill_dir


def _make_ctx(tmp_path: Path, skill_dir: Path, runner: FakeRunner) -> WorkflowContext:
    return WorkflowContext(
        bot_id="bot",
        chat_id="chat",
        thread_id=None,
        sender_id="user",
        chat_type="p2p",
        message_id="msg",
        workspace=tmp_path,
        runner=runner,
        runner_type="pi",
        handle=None,
        journal=None,
        session_id="sid",
        agents_home=tmp_path / "agents",
        skill_dir=skill_dir,
    )


def _json_response(payload: dict) -> dict:
    return {"result": json.dumps(payload, ensure_ascii=False), "is_error": False}


def test_memory_gc_rejects_write_mode(tmp_path):
    skill_dir = _make_skill(tmp_path, {"daily_count": 0, "curated_count": 0})
    runner = FakeRunner([])
    wf = MemoryGcWorkflow(skill_dir=skill_dir, ttl_string="24h")
    result = wf.start(_make_ctx(tmp_path, skill_dir, runner), "")
    assert result.state == STATE_FAILED
    assert "只支持只读模式" in result.user_message
    assert runner.calls == []


def test_memory_gc_bad_args_fail_without_runner(tmp_path):
    skill_dir = _make_skill(tmp_path, {"daily_count": 0, "curated_count": 0})
    runner = FakeRunner([])
    wf = MemoryGcWorkflow(skill_dir=skill_dir, ttl_string="24h")
    result = wf.start(_make_ctx(tmp_path, skill_dir, runner), '"unterminated')
    assert result.state == STATE_FAILED
    assert "参数解析失败" in result.user_message
    assert runner.calls == []


def test_memory_gc_healthy_dry_run_skips_runner(tmp_path):
    stats = {
        "daily_count": 0,
        "daily_files": [],
        "curated_count": 12,
        "session_count": 2,
        "oldest_session": "",
    }
    skill_dir = _make_skill(tmp_path, stats)
    runner = FakeRunner([])
    wf = MemoryGcWorkflow(skill_dir=skill_dir, ttl_string="24h")
    result = wf.start(_make_ctx(tmp_path, skill_dir, runner), "--dry-run")
    assert result.state == STATE_COMPLETED
    assert "无需清理" in result.user_message
    assert "未修改任何文件" in result.user_message
    assert runner.calls == []


def test_memory_gc_dry_run_classifies_daily_files(tmp_path, monkeypatch):
    daily = tmp_path / "lesson.md"
    daily.write_text("- [feishu-bridge] learned thing\n", encoding="utf-8")
    stats = {
        "daily_count": 1,
        "daily_files": [str(daily)],
        "curated_count": 81,
        "session_count": 5,
        "oldest_session": "2026-03-01.md",
    }
    skill_dir = _make_skill(tmp_path, stats)
    agents_home = tmp_path / "agents"
    rules = agents_home / "rules" / "lessons.md"
    rules.parent.mkdir(parents=True)
    rules.write_text("- [TOOL] **Old thing**: keep it\n", encoding="utf-8")
    monkeypatch.setenv("AGENTS_HOME", str(agents_home))

    classification = {
        "summary": "one daily file can route to project ctx",
        "daily": [{
            "file": str(daily),
            "action": "ROUTE",
            "target": "ctx/feishu-bridge",
            "reason": "project-tagged lesson",
        }],
        "curated": [{
            "match": "Old thing",
            "action": "KEEP",
            "reason": "still relevant",
        }],
        "recommendations": ["run apply mode later"],
    }
    runner = FakeRunner([_json_response(classification)])
    wf = MemoryGcWorkflow(skill_dir=skill_dir, ttl_string="24h")
    result = wf.start(_make_ctx(tmp_path, skill_dir, runner), "--dry-run")

    assert result.state == STATE_COMPLETED
    assert "ROUTE" in result.user_message
    assert "ctx/feishu-bridge" in result.user_message
    assert "Old thing" in runner.calls[0]["prompt"]
    assert str(daily) in runner.calls[0]["prompt"]
    assert result.payload["classification"]["summary"] == classification["summary"]


def test_memory_gc_json_failure_fails_closed(tmp_path):
    stats = {
        "daily_count": 1,
        "daily_files": [],
        "curated_count": 81,
        "session_count": 0,
        "oldest_session": "",
    }
    skill_dir = _make_skill(tmp_path, stats)
    runner = FakeRunner([
        {"result": "not json", "is_error": False},
        {"result": "still not json", "is_error": False},
        {"result": "nope", "is_error": False},
    ])
    wf = MemoryGcWorkflow(skill_dir=skill_dir, ttl_string="24h")
    result = wf.start(_make_ctx(tmp_path, skill_dir, runner), "--dry-run")
    assert result.state == STATE_FAILED
    assert "未修改任何文件" in result.user_message
    assert len(runner.calls) == 3


def test_memory_gc_bad_stats_json_fails(tmp_path):
    skill_dir = _make_skill(tmp_path, "{not json")
    runner = FakeRunner([])
    wf = MemoryGcWorkflow(skill_dir=skill_dir, ttl_string="24h")
    result = wf.start(_make_ctx(tmp_path, skill_dir, runner), "--dry-run")
    assert result.state == STATE_FAILED
    assert "统计失败" in result.user_message
