"""Bridge-managed reviewer worker calls (Phase 6.8).

This is intentionally a small orchestration layer over the active runner. It
does not create Claude Code subagents; it gives non-Claude runtimes a common
surface for bounded reviewer prompts with explicit failure reporting and shared
budget tracking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from feishu_bridge.workflows.runtime import WorkflowContext

ROLE_PLAN_REVIEWER = "plan-reviewer"
ROLE_CODE_REVIEWER = "code-reviewer"
ROLE_SECURITY_REVIEWER = "security-reviewer"

ALLOWED_REVIEWER_ROLES = frozenset({
    ROLE_PLAN_REVIEWER,
    ROLE_CODE_REVIEWER,
    ROLE_SECURITY_REVIEWER,
})


@dataclass
class AgentPoolTask:
    role: str
    prompt: str
    max_output_chars: int = 12000


@dataclass
class AgentPoolResult:
    role: str
    ok: bool
    output: str = ""
    error: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    total_cost_usd: float | None = None


@dataclass
class AgentPoolBudget:
    """Shared budget guard for a batch of reviewer calls."""

    max_calls: int = 3
    max_total_cost_usd: float | None = None
    calls_used: int = 0
    cost_used_usd: float = 0.0

    def allow_next(self) -> tuple[bool, str | None]:
        if self.calls_used >= self.max_calls:
            return False, "agent pool call budget exhausted"
        if (
            self.max_total_cost_usd is not None
            and self.cost_used_usd >= self.max_total_cost_usd
        ):
            return False, "agent pool cost budget exhausted"
        return True, None

    def record(self, result: dict[str, Any]) -> None:
        self.calls_used += 1
        cost = result.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            self.cost_used_usd += max(0.0, float(cost))


class AgentPool:
    """Run bounded reviewer prompts through the active workflow runner."""

    def __init__(self, budget: AgentPoolBudget | None = None) -> None:
        self.budget = budget or AgentPoolBudget()

    def run(
        self, ctx: WorkflowContext, tasks: Iterable[AgentPoolTask],
    ) -> list[AgentPoolResult]:
        results: list[AgentPoolResult] = []
        for task in tasks:
            if task.role not in ALLOWED_REVIEWER_ROLES:
                results.append(AgentPoolResult(
                    role=task.role,
                    ok=False,
                    error=f"unsupported reviewer role: {task.role}",
                ))
                continue

            allowed, reason = self.budget.allow_next()
            if not allowed:
                results.append(AgentPoolResult(
                    role=task.role,
                    ok=False,
                    error=reason,
                ))
                continue

            result = ctx.runner.run(
                self._wrap_prompt(task),
                session_id=None,
                resume=False,
                tag=f"agent-pool:{task.role}",
                on_output=None,
            )
            self.budget.record(result)
            text = str(result.get("result") or "")
            if len(text) > task.max_output_chars:
                text = text[:task.max_output_chars].rstrip() + "\n...[truncated]"
            if result.get("is_error"):
                results.append(AgentPoolResult(
                    role=task.role,
                    ok=False,
                    output=text,
                    error=text or "reviewer runner returned is_error=True",
                    usage=result.get("usage") or {},
                    total_cost_usd=result.get("total_cost_usd"),
                ))
                continue
            results.append(AgentPoolResult(
                role=task.role,
                ok=True,
                output=text,
                usage=result.get("usage") or {},
                total_cost_usd=result.get("total_cost_usd"),
            ))
        return results

    def _wrap_prompt(self, task: AgentPoolTask) -> str:
        return (
            f"You are the `{task.role}` reviewer for a bridge-managed workflow.\n"
            "Return concise findings first. If there are no findings, say so "
            "explicitly. Do not claim success if required evidence is missing.\n\n"
            f"{task.prompt}"
        )


__all__ = [
    "ALLOWED_REVIEWER_ROLES",
    "AgentPool",
    "AgentPoolBudget",
    "AgentPoolResult",
    "AgentPoolTask",
    "ROLE_CODE_REVIEWER",
    "ROLE_PLAN_REVIEWER",
    "ROLE_SECURITY_REVIEWER",
]
