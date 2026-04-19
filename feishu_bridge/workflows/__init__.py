"""Runner-neutral workflow registry and command policy.

Phase 6.1 (pi-runner change) introduces this package to let non-Claude runners
share core workflows (/plan, /done, /memory-gc) via bridge-owned control flow.
PlanWorkflow (Phase 6.4) is the first concrete implementation; MemoryGcWorkflow
(6.5) and DoneWorkflow (6.7) plug in via the same runtime contract.
"""

from feishu_bridge.workflows.agent_pool import (
    ALLOWED_REVIEWER_ROLES,
    ROLE_CODE_REVIEWER,
    ROLE_PLAN_REVIEWER,
    ROLE_SECURITY_REVIEWER,
    AgentPool,
    AgentPoolBudget,
    AgentPoolResult,
    AgentPoolTask,
)
from feishu_bridge.workflows.done_workflow import DoneWorkflow
from feishu_bridge.workflows.memory_gc_workflow import MemoryGcWorkflow
from feishu_bridge.workflows.plan_workflow import PlanWorkflow
from feishu_bridge.workflows.registry import (
    AGENTS_HOME_ENV,
    CommandPolicy,
    DECISION_BRIDGE_WORKFLOW,
    DECISION_CLAUDE_NATIVE,
    DECISION_UNSUPPORTED,
    INTERCEPT_ALWAYS,
    INTERCEPT_AUTO,
    INTERCEPT_NEVER,
    RunnerCommandDecision,
    SkillMetadata,
    WorkflowRegistry,
    resolve_agents_home,
)
from feishu_bridge.workflows.runtime import (
    ALL_STATES,
    STATE_CANCELLED,
    STATE_COMPLETED,
    STATE_DRAFT,
    STATE_EXPIRED,
    STATE_FAILED,
    STATE_WAITING_CONFIRMATION,
    TERMINAL_STATES,
    JsonPolicyError,
    WorkflowContext,
    WorkflowResult,
    parse_ttl_seconds,
    request_json_with_policy,
)
from feishu_bridge.workflows.storage import (
    TERMINAL_RETENTION_SECONDS,
    WorkflowRecord,
    WorkflowStorage,
)

__all__ = [
    "AGENTS_HOME_ENV",
    "ALL_STATES",
    "ALLOWED_REVIEWER_ROLES",
    "AgentPool",
    "AgentPoolBudget",
    "AgentPoolResult",
    "AgentPoolTask",
    "CommandPolicy",
    "DECISION_BRIDGE_WORKFLOW",
    "DECISION_CLAUDE_NATIVE",
    "DECISION_UNSUPPORTED",
    "DoneWorkflow",
    "INTERCEPT_ALWAYS",
    "INTERCEPT_AUTO",
    "INTERCEPT_NEVER",
    "JsonPolicyError",
    "MemoryGcWorkflow",
    "PlanWorkflow",
    "ROLE_CODE_REVIEWER",
    "ROLE_PLAN_REVIEWER",
    "ROLE_SECURITY_REVIEWER",
    "RunnerCommandDecision",
    "STATE_CANCELLED",
    "STATE_COMPLETED",
    "STATE_DRAFT",
    "STATE_EXPIRED",
    "STATE_FAILED",
    "STATE_WAITING_CONFIRMATION",
    "SkillMetadata",
    "TERMINAL_RETENTION_SECONDS",
    "TERMINAL_STATES",
    "WorkflowContext",
    "WorkflowRecord",
    "WorkflowRegistry",
    "WorkflowResult",
    "WorkflowStorage",
    "parse_ttl_seconds",
    "request_json_with_policy",
    "resolve_agents_home",
]
