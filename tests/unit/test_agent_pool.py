"""Tests for bridge-managed AgentPool (Phase 6.8)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from feishu_bridge.workflows import (
    ROLE_CODE_REVIEWER,
    ROLE_PLAN_REVIEWER,
    AgentPool,
    AgentPoolBudget,
    AgentPoolTask,
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


def _ctx(tmp_path: Path, runner: FakeRunner) -> WorkflowContext:
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
        skill_dir=tmp_path / "skill",
    )


def test_agent_pool_runs_supported_reviewers(tmp_path):
    runner = FakeRunner([
        {"result": "no findings", "is_error": False, "total_cost_usd": 0.01},
        {"result": "one issue", "is_error": False, "total_cost_usd": 0.02},
    ])
    pool = AgentPool(AgentPoolBudget(max_calls=3, max_total_cost_usd=1.0))
    results = pool.run(_ctx(tmp_path, runner), [
        AgentPoolTask(role=ROLE_PLAN_REVIEWER, prompt="review plan"),
        AgentPoolTask(role=ROLE_CODE_REVIEWER, prompt="review code"),
    ])

    assert [r.ok for r in results] == [True, True]
    assert results[0].output == "no findings"
    assert "plan-reviewer" in runner.calls[0]["prompt"]
    assert runner.calls[0]["tag"] == "agent-pool:plan-reviewer"
    assert pool.budget.calls_used == 2
    assert pool.budget.cost_used_usd == 0.03


def test_agent_pool_runner_error_is_visible(tmp_path):
    runner = FakeRunner([
        {"result": "rate limited", "is_error": True},
    ])
    results = AgentPool().run(_ctx(tmp_path, runner), [
        AgentPoolTask(role=ROLE_CODE_REVIEWER, prompt="review"),
    ])

    assert results[0].ok is False
    assert results[0].error == "rate limited"


def test_agent_pool_rejects_unsupported_role_without_runner_call(tmp_path):
    runner = FakeRunner([])
    results = AgentPool().run(_ctx(tmp_path, runner), [
        AgentPoolTask(role="random-reviewer", prompt="review"),
    ])

    assert results[0].ok is False
    assert "unsupported" in results[0].error
    assert runner.calls == []


def test_agent_pool_budget_exhaustion_is_failure_result(tmp_path):
    runner = FakeRunner([
        {"result": "first", "is_error": False},
    ])
    pool = AgentPool(AgentPoolBudget(max_calls=1))
    results = pool.run(_ctx(tmp_path, runner), [
        AgentPoolTask(role=ROLE_PLAN_REVIEWER, prompt="a"),
        AgentPoolTask(role=ROLE_CODE_REVIEWER, prompt="b"),
    ])

    assert results[0].ok is True
    assert results[1].ok is False
    assert "budget exhausted" in results[1].error
    assert len(runner.calls) == 1
