"""Workflow runtime contract (Phase 6.4 of pi-runner change).

Defines the base Workflow class, WorkflowContext and WorkflowResult dataclasses,
and the JSON reliability policy shared by all bridge-owned workflows.

Per design doc `command-runtime-plan.md` §JSON Reliability Policy:
    1. Strict JSON request.
    2. Retry with validation error feedback.
    3. Fenced ```json block extraction.
    4. Fail closed — no file mutation.

The runtime is Workflow-agnostic: PlanWorkflow (Phase 6.4), MemoryGcWorkflow
(6.5), DoneWorkflow (6.7) all plug in via the same base class.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

log = logging.getLogger("feishu-bridge")


# ---- state enum -------------------------------------------------------

STATE_DRAFT = "draft"
STATE_WAITING_CONFIRMATION = "waiting_confirmation"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_EXPIRED = "expired"
STATE_CANCELLED = "cancelled"

ALL_STATES = frozenset({
    STATE_DRAFT,
    STATE_WAITING_CONFIRMATION,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_EXPIRED,
    STATE_CANCELLED,
})

TERMINAL_STATES = frozenset({
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_EXPIRED,
    STATE_CANCELLED,
})


# ---- TTL parsing ------------------------------------------------------

_TTL_RE = re.compile(r"^\s*(\d+)\s*([smhdw]?)\s*$", re.IGNORECASE)
_TTL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "": 1}


def parse_ttl_seconds(ttl: str | None, default_seconds: int) -> int:
    """Parse a ttl string like `7d`, `24h`, `300s`. Falls back on parse error."""
    if not ttl:
        return default_seconds
    match = _TTL_RE.match(str(ttl))
    if not match:
        return default_seconds
    qty, unit = match.group(1), (match.group(2) or "").lower()
    try:
        return int(qty) * _TTL_UNITS[unit]
    except (KeyError, ValueError):
        return default_seconds


# ---- dataclasses ------------------------------------------------------

@dataclass
class WorkflowContext:
    """Per-invocation runtime context supplied by the bridge command handler.

    Fields are populated from the incoming message item + bot state. Workflows
    should treat this as read-only and pass it through to nested helpers.
    """

    bot_id: str
    chat_id: str
    thread_id: str | None
    sender_id: str
    chat_type: str
    message_id: str | None
    workspace: Path
    runner: Any  # BaseRunner; typed Any to avoid cyclic import at module load
    runner_type: str
    handle: Any  # ResponseHandle
    journal: Any  # SessionJournal | None
    session_id: str | None
    agents_home: Path
    skill_dir: Path  # ~/.agents/skills/<name>/
    now: float = field(default_factory=time.time)

    @property
    def scope_key(self) -> str:
        return f"{self.bot_id}|{self.chat_id}|{self.thread_id or ''}"


@dataclass
class WorkflowResult:
    """Outcome of a single Workflow.start() or Workflow.resume() call."""

    state: str
    user_message: str = ""
    next_expected_input: str | None = None
    expires_at: float | None = None
    artifacts: list[str] = field(default_factory=list)
    error: str | None = None
    payload: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in ALL_STATES:
            raise ValueError(
                f"invalid workflow state {self.state!r}; expected one of {sorted(ALL_STATES)}"
            )


class Workflow(Protocol):
    """Structural contract; PlanWorkflow implements via duck typing."""

    name: str
    version: int

    def start(self, ctx: WorkflowContext, arg: str) -> WorkflowResult: ...
    def resume(
        self, ctx: WorkflowContext, user_text: str, payload: dict,
    ) -> WorkflowResult: ...


# ---- JSON reliability policy -----------------------------------------

_FENCED_JSON_RE = re.compile(
    r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE,
)


class JsonPolicyError(Exception):
    """Raised when all 3 strikes of the JSON policy have been exhausted."""

    def __init__(self, message: str, last_output: str = "") -> None:
        super().__init__(message)
        self.last_output = last_output


def _extract_json_object(text: str) -> dict | None:
    """Try to parse `text` as JSON, or extract first ```json block."""
    if not text:
        return None
    stripped = text.strip()
    # Direct parse
    try:
        data = json.loads(stripped)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    # Fenced-block fallback
    match = _FENCED_JSON_RE.search(text)
    if match:
        try:
            data = json.loads(match.group(1))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _validate_shape(
    obj: dict, schema_required: list[str],
) -> list[str]:
    """Return list of missing/invalid field names; empty list = valid."""
    missing: list[str] = []
    for key in schema_required:
        value = obj.get(key)
        if value is None:
            missing.append(key)
            continue
        if isinstance(value, str) and not value.strip():
            missing.append(key)
    return missing


def request_json_with_policy(
    *,
    runner_call: Callable[[str], dict],
    base_prompt: str,
    schema_required: list[str],
    max_attempts: int = 3,
    log_prefix: str = "workflow",
) -> dict:
    """Run the 3-strike JSON policy against a callable that wraps runner.run().

    `runner_call(prompt_text)` must return a dict with keys `result` (str) and
    `is_error` (bool). It is re-invoked on each retry with a different prompt.

    Returns the validated JSON dict on success. Raises JsonPolicyError on
    exhaustion — caller must fail closed without mutation.
    """
    last_output = ""
    last_error = "no attempt made"
    for attempt in range(1, max_attempts + 1):
        if attempt == 1:
            prompt = base_prompt
        elif attempt == 2:
            prompt = (
                f"{base_prompt}\n\n"
                f"Your previous reply did not parse as valid JSON matching the "
                f"schema. Error: {last_error}\n"
                f"Reply ONLY with the corrected JSON object — no prose, no "
                f"markdown fences."
            )
        else:
            prompt = (
                f"{base_prompt}\n\n"
                f"Your previous replies were invalid. Last error: {last_error}\n"
                f"You MAY wrap the JSON in a ```json ... ``` fenced block. "
                f"Reply with nothing else."
            )

        result = runner_call(prompt)
        text = str(result.get("result", "") or "")
        last_output = text
        if result.get("is_error"):
            last_error = (text[:500] or "runner returned is_error=True").strip()
            log.warning(
                "%s: JSON attempt %d runner error: %s",
                log_prefix, attempt, last_error[:200],
            )
            continue

        obj = _extract_json_object(text)
        if obj is None:
            last_error = "response could not be parsed as JSON object"
            log.warning(
                "%s: JSON attempt %d parse failed; first 200 chars: %r",
                log_prefix, attempt, text[:200],
            )
            continue

        missing = _validate_shape(obj, schema_required)
        if missing:
            last_error = f"missing/empty required fields: {', '.join(missing)}"
            log.warning(
                "%s: JSON attempt %d shape invalid: %s",
                log_prefix, attempt, last_error,
            )
            continue

        return obj

    raise JsonPolicyError(
        f"3-strike JSON policy exhausted: {last_error}",
        last_output=last_output,
    )


__all__ = [
    "STATE_DRAFT",
    "STATE_WAITING_CONFIRMATION",
    "STATE_COMPLETED",
    "STATE_FAILED",
    "STATE_EXPIRED",
    "STATE_CANCELLED",
    "ALL_STATES",
    "TERMINAL_STATES",
    "WorkflowContext",
    "WorkflowResult",
    "Workflow",
    "JsonPolicyError",
    "parse_ttl_seconds",
    "request_json_with_policy",
]
