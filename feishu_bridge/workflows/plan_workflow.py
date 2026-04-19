"""PlanWorkflow — bridge-owned /plan for non-Claude runners (Phase 6.4).

State machine:
    /plan <goal> ──draft──▶ waiting_confirmation ──/confirm──▶ completed
                                     │                             │
                                     ├──/stop──▶ cancelled           │
                                     └──TTL──▶ expired               ▼
                                                                persist ok

The workflow asks the runner for a JSON plan draft using the 3-strike JSON
reliability policy, stores the validated draft in workflow storage, and waits
for a deterministic `/confirm` (or `/stop`) signal from the same scope. On
confirm, `scripts/spec-write.py` is invoked to render proposal.md + tasks.md
under `.specs/changes/<slug>/`.

Deliberately NOT in MVP:
- Re-draft on arbitrary user text (design doc §Phase 1 explicitly limits to
  deterministic confirmation).
- Fallback runner on JSON exhaustion (design doc §JSON Reliability Policy
  lists this as optional; disabled by default for MVP).
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from feishu_bridge.workflows.runtime import (
    STATE_CANCELLED,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_WAITING_CONFIRMATION,
    JsonPolicyError,
    WorkflowContext,
    WorkflowResult,
    parse_ttl_seconds,
    request_json_with_policy,
)

log = logging.getLogger("feishu-bridge")

# How many recent user/assistant turn entries to fold into the draft prompt
# for context. Keeps prompt size bounded; journal kind filter excludes
# artifact/workflow_event noise.
JOURNAL_CONTEXT_MAX_ENTRIES = 6

# Schema required-fields for 3-strike JSON validation. `tasks` is array-typed
# so it passes the non-empty-string check in runtime._validate_shape; deeper
# shape is enforced by spec-write.py before files touch disk.
_DRAFT_REQUIRED = ["slug", "title", "branch", "scope", "why", "what", "not", "risks", "tasks"]

# Default TTL if workflow.yaml is missing or unparseable: 7 days per design §Phase 1.
_DEFAULT_TTL_SECONDS = 7 * 86400

_DRAFT_PREVIEW_MAX_CHARS = 1500


class PlanWorkflow:
    """Stateless workflow implementation. All state lives in WorkflowStorage.

    Instances hold configuration (skill_dir, ttl). They are safe to reuse
    across invocations since per-call state comes through WorkflowContext +
    stored payloads.
    """

    name = "plan"
    version = 1

    def __init__(
        self, skill_dir: Path, ttl_string: str | None = "7d",
    ) -> None:
        self.skill_dir = Path(skill_dir)
        self.ttl_seconds = parse_ttl_seconds(ttl_string, _DEFAULT_TTL_SECONDS)

    # ---- state entrypoints -------------------------------------------

    def start(self, ctx: WorkflowContext, arg: str) -> WorkflowResult:
        """Draft a plan from the user's goal text. Wait for /confirm."""
        goal = (arg or "").strip()
        if not goal:
            return WorkflowResult(
                state=STATE_FAILED,
                user_message="`/plan <goal>` 需要提供目标描述。",
                error="empty arg",
            )

        try:
            specs_info = self._resolve_specs(ctx.workspace)
        except _PlanError as exc:
            return WorkflowResult(
                state=STATE_FAILED,
                user_message=f"`/plan` 无法解析工作区: {exc}",
                error=str(exc),
            )
        if not specs_info["slots_remaining"]:
            names = specs_info.get("changes") or []
            return WorkflowResult(
                state=STATE_FAILED,
                user_message=(
                    "`.specs/changes/` 已达 3 个 active change 上限："
                    f"{', '.join(names)}。请先归档或放弃一个再执行 /plan。"
                ),
                error="max changes reached",
                payload={"specs_info": specs_info},
            )

        draft_prompt = self._build_draft_prompt(ctx, goal, specs_info)
        start_ts = time.time()
        try:
            draft = request_json_with_policy(
                runner_call=lambda p: ctx.runner.run(
                    p, session_id=ctx.session_id, resume=False,
                    tag=None, on_output=None,
                ),
                base_prompt=draft_prompt,
                schema_required=_DRAFT_REQUIRED,
                log_prefix=f"plan[{ctx.scope_key}]",
            )
        except JsonPolicyError as exc:
            log.warning(
                "plan: JSON policy exhausted in scope %s: %s",
                ctx.scope_key, exc,
            )
            return WorkflowResult(
                state=STATE_FAILED,
                user_message=(
                    "`/plan` 草拟失败：本轮未能生成有效 JSON 计划（已重试 3 次）。"
                    "请稍后重试或缩短目标描述。"
                ),
                error=str(exc),
            )
        duration = time.time() - start_ts
        log.info(
            "plan: draft ok in scope %s (duration=%.1fs slug=%s)",
            ctx.scope_key, duration, draft.get("slug"),
        )

        # Persist draft payload and wait for /confirm. The payload holds
        # everything needed to render files later without another LLM call.
        draft_payload = {
            "draft": draft,
            "goal": goal,
            "specs_root": specs_info["specs_root"],
            "start_sha": self._head_sha(ctx.workspace),
            "created_at": time.time(),
        }
        preview = self._render_preview(draft)
        message = (
            f"📝 `/plan` 草稿（slug: `{draft['slug']}`，分支: `{draft['branch']}`，"
            f"范围: `{draft['scope']}`）：\n\n{preview}\n\n"
            f"回复 `/confirm` 以写入 `.specs/changes/{draft['slug']}/`，"
            f"或 `/stop` 放弃。草稿保留 {self._ttl_label()}。"
        )
        return WorkflowResult(
            state=STATE_WAITING_CONFIRMATION,
            user_message=message,
            next_expected_input="/confirm 或 /stop",
            expires_at=time.time() + self.ttl_seconds,
            payload=draft_payload,
        )

    def resume_confirm(
        self, ctx: WorkflowContext, payload: dict,
    ) -> WorkflowResult:
        """User typed /confirm — persist the draft to disk."""
        draft = payload.get("draft") or {}
        specs_root = payload.get("specs_root")
        start_sha = payload.get("start_sha") or self._head_sha(ctx.workspace)
        if not draft or not specs_root:
            return WorkflowResult(
                state=STATE_FAILED,
                user_message="`/confirm` 失败：草稿状态损坏，请重新 `/plan`。",
                error="payload missing draft or specs_root",
            )
        try:
            artifacts = self._write_artifacts(
                draft=draft, specs_root=specs_root, start_sha=start_sha,
            )
        except _PlanError as exc:
            return WorkflowResult(
                state=STATE_FAILED,
                user_message=f"写入失败: {exc}",
                error=str(exc),
                payload=payload,
            )
        message = (
            f"✅ 已写入:\n"
            f"- `{artifacts['proposal']}`\n"
            f"- `{artifacts['tasks']}`\n\n"
            f"下一步：根据 `tasks.md` 实施，结束时运行 `/done`。"
        )
        return WorkflowResult(
            state=STATE_COMPLETED,
            user_message=message,
            artifacts=[artifacts["proposal"], artifacts["tasks"]],
            payload={**payload, "artifacts": artifacts},
        )

    def resume_cancel(
        self, ctx: WorkflowContext, payload: dict,
    ) -> WorkflowResult:
        """User typed /stop — drop the draft without writing files."""
        slug = (payload.get("draft") or {}).get("slug", "?")
        return WorkflowResult(
            state=STATE_CANCELLED,
            user_message=f"已放弃 `/plan` 草稿 `{slug}`。",
            payload=payload,
        )

    # ---- helpers -----------------------------------------------------

    def _resolve_specs(self, workspace: Path) -> dict:
        script = self.skill_dir / "scripts" / "spec-resolve.py"
        if not script.is_file():
            raise _PlanError(f"spec-resolve script missing: {script}")
        try:
            r = subprocess.run(
                [sys.executable, str(script), "--repo", str(workspace)],
                capture_output=True, text=True, timeout=10,
            )
        except subprocess.SubprocessError as exc:
            raise _PlanError(f"spec-resolve call failed: {exc}") from exc
        if r.returncode != 0:
            raise _PlanError(
                f"spec-resolve non-zero exit {r.returncode}: "
                f"{(r.stderr or r.stdout).strip()[:300]}"
            )
        try:
            return json.loads(r.stdout.strip())
        except json.JSONDecodeError as exc:
            raise _PlanError(f"spec-resolve bad JSON: {exc}") from exc

    def _write_artifacts(
        self, *, draft: dict, specs_root: str, start_sha: str,
    ) -> dict:
        script = self.skill_dir / "scripts" / "spec-write.py"
        if not script.is_file():
            raise _PlanError(f"spec-write script missing: {script}")
        payload_json = json.dumps(draft, ensure_ascii=False)
        try:
            r = subprocess.run(
                [
                    sys.executable, str(script),
                    "--specs-root", specs_root,
                    "--start-sha", start_sha or "none",
                    "--payload-file", "-",
                ],
                input=payload_json, capture_output=True, text=True, timeout=10,
            )
        except subprocess.SubprocessError as exc:
            raise _PlanError(f"spec-write call failed: {exc}") from exc
        output_text = (r.stdout or "").strip()
        try:
            parsed = json.loads(output_text) if output_text else {}
        except json.JSONDecodeError:
            parsed = {}
        if r.returncode != 0 or not parsed.get("ok"):
            err = parsed.get("error") or (r.stderr or r.stdout or "").strip()
            raise _PlanError(err[:500] or "spec-write returned error")
        return parsed

    def _build_draft_prompt(
        self, ctx: WorkflowContext, goal: str, specs_info: dict,
    ) -> str:
        prompt_file = self.skill_dir / "prompts" / "draft.md"
        try:
            base = prompt_file.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("plan: draft prompt missing (%s); using inline fallback", exc)
            base = (
                "Draft a JSON plan with fields: slug, title, branch, scope, "
                "why, what, not, risks, tasks[{phase, items[]}]."
            )

        context_block = self._build_journal_context(ctx)
        specs_hint = self._format_specs_hint(specs_info)

        return (
            f"{base}\n\n"
            f"---\n\n"
            f"## User goal\n\n{goal}\n\n"
            f"## Recent conversation context\n\n{context_block}\n\n"
            f"## Repository state\n\n{specs_hint}\n"
        )

    def _build_journal_context(self, ctx: WorkflowContext) -> str:
        if ctx.journal is None:
            return "(no journal available)"
        try:
            entries = list(ctx.journal.read(
                ctx.bot_id, ctx.chat_id, ctx.thread_id,
            ))
        except Exception as exc:  # journal must never break workflow start
            log.debug("plan: journal read failed: %s", exc)
            return "(journal read failed)"
        turns = [
            e for e in entries
            if e.get("kind") in ("user_turn", "assistant_turn")
        ]
        if not turns:
            return "(no prior turns in this scope)"
        tail = turns[-JOURNAL_CONTEXT_MAX_ENTRIES:]
        lines: list[str] = []
        for e in tail:
            kind = "user" if e.get("kind") == "user_turn" else "assistant"
            text = (e.get("text") or "").strip()
            if not text:
                continue
            if len(text) > 400:
                text = text[:400].rstrip() + "…"
            lines.append(f"**{kind}**: {text}")
        return "\n\n".join(lines) if lines else "(no usable turns)"

    def _format_specs_hint(self, specs_info: dict) -> str:
        changes = specs_info.get("changes") or []
        if not changes:
            return (
                f"`.specs/` root: `{specs_info.get('specs_root')}` "
                f"(无 active change)"
            )
        return (
            f"`.specs/` root: `{specs_info.get('specs_root')}`\n"
            f"Current active changes: {', '.join(changes)}\n"
            f"Current branch: `{specs_info.get('current_branch') or '(none)'}`\n"
            f"Slots remaining: {specs_info.get('slots_remaining', 0)}/3"
        )

    def _render_preview(self, draft: dict) -> str:
        pretty = json.dumps(draft, ensure_ascii=False, indent=2)
        if len(pretty) > _DRAFT_PREVIEW_MAX_CHARS:
            pretty = pretty[:_DRAFT_PREVIEW_MAX_CHARS].rstrip() + "\n… (truncated)"
        return f"```json\n{pretty}\n```"

    def _head_sha(self, workspace: Path) -> str:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(workspace),
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                return r.stdout.strip() or "none"
        except (subprocess.SubprocessError, OSError):
            pass
        return "none"

    def _ttl_label(self) -> str:
        s = self.ttl_seconds
        if s % 86400 == 0:
            return f"{s // 86400} 天"
        if s % 3600 == 0:
            return f"{s // 3600} 小时"
        if s % 60 == 0:
            return f"{s // 60} 分钟"
        return f"{s} 秒"


class _PlanError(Exception):
    """Internal: unify subprocess / script errors into a single exception."""


__all__ = ["PlanWorkflow"]
