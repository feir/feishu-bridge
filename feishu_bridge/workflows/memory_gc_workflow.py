"""MemoryGcWorkflow — bridge-owned /memory-gc --dry-run (Phase 6.5).

The MVP is intentionally read-only. It gathers deterministic memory stats,
loads bounded lesson excerpts, asks the runner for classification JSON, and
renders a dry-run report. It never calls route/archive/maintain scripts.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from feishu_bridge.paths import agents_home, claude_home
from feishu_bridge.workflows.runtime import (
    STATE_COMPLETED,
    STATE_FAILED,
    JsonPolicyError,
    WorkflowContext,
    WorkflowResult,
    parse_ttl_seconds,
    request_json_with_policy,
)

log = logging.getLogger("feishu-bridge")

_DEFAULT_TTL_SECONDS = 24 * 3600
_CLASSIFY_REQUIRED = ["summary", "daily", "curated", "recommendations"]
_MAX_DAILY_FILES = 8
_MAX_DAILY_CHARS_PER_FILE = 2500
_MAX_CURATED_LINES = 120
_MAX_REPORT_ITEMS = 20


class MemoryGcWorkflow:
    """Read-only memory maintenance dry-run workflow."""

    name = "memory-gc"
    version = 1

    def __init__(self, skill_dir: Path, ttl_string: str | None = "24h") -> None:
        self.skill_dir = Path(skill_dir)
        self.ttl_seconds = parse_ttl_seconds(ttl_string, _DEFAULT_TTL_SECONDS)

    def start(self, ctx: WorkflowContext, arg: str) -> WorkflowResult:
        try:
            args = shlex.split(arg or "")
        except ValueError as exc:
            return WorkflowResult(
                state=STATE_FAILED,
                user_message=f"`/memory-gc` 参数解析失败: {exc}",
                error=str(exc),
            )
        if "--dry-run" not in args:
            return WorkflowResult(
                state=STATE_FAILED,
                user_message=(
                    "`/memory-gc` 的 bridge MVP 目前只支持只读模式。"
                    "请使用 `/memory-gc --dry-run`。"
                ),
                error="write mode not implemented",
            )

        try:
            stats = self._run_stats()
        except _MemoryGcError as exc:
            return WorkflowResult(
                state=STATE_FAILED,
                user_message=f"`/memory-gc --dry-run` 统计失败: {exc}",
                error=str(exc),
            )

        if self._nothing_to_do(stats):
            return WorkflowResult(
                state=STATE_COMPLETED,
                user_message=self._render_healthy_report(stats),
                payload={"stats": stats, "classification": None},
            )

        prompt = self._build_classify_prompt(ctx, stats)
        started = time.time()
        try:
            classification = request_json_with_policy(
                runner_call=lambda p: ctx.runner.run(
                    p, session_id=ctx.session_id, resume=False,
                    tag=None, on_output=None,
                ),
                base_prompt=prompt,
                schema_required=_CLASSIFY_REQUIRED,
                log_prefix=f"memory-gc[{ctx.scope_key}]",
            )
        except JsonPolicyError as exc:
            log.warning(
                "memory-gc: JSON policy exhausted in scope %s: %s",
                ctx.scope_key, exc,
            )
            return WorkflowResult(
                state=STATE_FAILED,
                user_message=(
                    "`/memory-gc --dry-run` 分类失败：本轮未能生成有效 JSON"
                    "（已重试 3 次），未修改任何文件。"
                ),
                error=str(exc),
                payload={"stats": stats},
            )

        log.info(
            "memory-gc: dry-run ok in scope %s (duration=%.1fs daily=%s curated=%s)",
            ctx.scope_key,
            time.time() - started,
            stats.get("daily_count"),
            stats.get("curated_count"),
        )
        return WorkflowResult(
            state=STATE_COMPLETED,
            user_message=self._render_dry_run_report(stats, classification),
            payload={"stats": stats, "classification": classification},
        )

    def resume_confirm(
        self, ctx: WorkflowContext, payload: dict,
    ) -> WorkflowResult:
        return WorkflowResult(
            state=STATE_FAILED,
            user_message="`/memory-gc` 写入模式尚未实现；请继续使用 `--dry-run`。",
            error="apply not implemented",
            payload=payload,
        )

    def resume_cancel(
        self, ctx: WorkflowContext, payload: dict,
    ) -> WorkflowResult:
        return WorkflowResult(
            state=STATE_COMPLETED,
            user_message="已关闭 `/memory-gc` dry-run 结果；未修改任何文件。",
            payload=payload,
        )

    # ---- deterministic readers --------------------------------------

    def _run_stats(self) -> dict[str, Any]:
        script = self._script_path("memory-gc-stats.sh")
        if script is None:
            raise _MemoryGcError("memory-gc-stats.sh not found")
        try:
            proc = subprocess.run(
                ["bash", str(script)],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.SubprocessError as exc:
            raise _MemoryGcError(f"stats script failed: {exc}") from exc
        if proc.returncode != 0:
            raise _MemoryGcError(
                (proc.stderr or proc.stdout or "").strip()[:500]
                or f"stats script exited {proc.returncode}"
            )
        try:
            data = json.loads((proc.stdout or "").strip())
        except json.JSONDecodeError as exc:
            raise _MemoryGcError(f"stats script returned bad JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise _MemoryGcError("stats script did not return an object")
        return data

    def _script_path(self, name: str) -> Path | None:
        candidates = [
            self.skill_dir / "scripts" / name,
            claude_home() / "skills" / "memory-gc" / "scripts" / name,
        ]
        for path in candidates:
            if path.is_file():
                return path
        return None

    def _nothing_to_do(self, stats: dict[str, Any]) -> bool:
        daily_count = int(stats.get("daily_count") or 0)
        curated_count = int(stats.get("curated_count") or 0)
        return daily_count == 0 and curated_count <= 80

    def _build_classify_prompt(
        self, ctx: WorkflowContext, stats: dict[str, Any],
    ) -> str:
        daily_block = self._daily_excerpts(stats)
        curated_block = self._curated_excerpt()
        return f"""Classify memory maintenance candidates for a dry run.

Return ONLY a JSON object with these keys:
- summary: short human-readable summary
- daily: array of objects with file, action, target, reason
- curated: array of objects with match, action, reason
- recommendations: array of short strings

Allowed daily actions: KEEP, ROUTE, ABSORBED, DUPLICATE, OUTDATED, DISCARD.
Allowed curated actions: KEEP, DELETE, MERGE, UPDATE.
Do not propose file writes. This is a read-only dry-run report.

## Stats

```json
{json.dumps(stats, ensure_ascii=False, indent=2)}
```

## Daily lesson excerpts

{daily_block}

## Curated lesson excerpt

{curated_block}
"""

    def _daily_excerpts(self, stats: dict[str, Any]) -> str:
        files = stats.get("daily_files") or []
        if not isinstance(files, list) or not files:
            return "(no daily lesson files)"
        blocks: list[str] = []
        for raw in files[:_MAX_DAILY_FILES]:
            path = Path(str(raw)).expanduser()
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                blocks.append(f"### {path}\n(read failed: {exc})")
                continue
            if len(text) > _MAX_DAILY_CHARS_PER_FILE:
                text = text[:_MAX_DAILY_CHARS_PER_FILE].rstrip() + "\n...[truncated]"
            blocks.append(f"### {path}\n\n{text}")
        if len(files) > _MAX_DAILY_FILES:
            blocks.append(f"... {len(files) - _MAX_DAILY_FILES} more files omitted")
        return "\n\n".join(blocks)

    def _curated_excerpt(self) -> str:
        path = agents_home() / "rules" / "lessons.md"
        if not path.is_file():
            path = claude_home() / "rules" / "lessons.md"
        if not path.is_file():
            return "(no curated lessons file found)"
        lines: list[str] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.lstrip().startswith("- "):
                    lines.append(line)
                    if len(lines) >= _MAX_CURATED_LINES:
                        break
        except OSError as exc:
            return f"(read failed: {exc})"
        return "\n".join(lines) if lines else "(no lesson entries found)"

    # ---- rendering ---------------------------------------------------

    def _render_healthy_report(self, stats: dict[str, Any]) -> str:
        return (
            "🧹 `/memory-gc --dry-run` 完成：无需清理。\n\n"
            f"- daily lessons: `{int(stats.get('daily_count') or 0)}`\n"
            f"- curated lessons: `{int(stats.get('curated_count') or 0)}/80`\n"
            f"- sessions: `{int(stats.get('session_count') or 0)}`\n\n"
            "未修改任何文件。"
        )

    def _render_dry_run_report(
        self, stats: dict[str, Any], classification: dict[str, Any],
    ) -> str:
        lines = [
            "🧹 `/memory-gc --dry-run` 报告（未修改任何文件）",
            "",
            f"- daily lessons: `{int(stats.get('daily_count') or 0)}`",
            f"- curated lessons: `{int(stats.get('curated_count') or 0)}/80`",
            f"- sessions: `{int(stats.get('session_count') or 0)}`",
            "",
            f"Summary: {classification.get('summary') or '(empty)'}",
        ]
        daily = classification.get("daily") or []
        if isinstance(daily, list) and daily:
            lines.extend(["", "**Daily candidates**"])
            for item in daily[:_MAX_REPORT_ITEMS]:
                if not isinstance(item, dict):
                    continue
                lines.append(
                    "- "
                    f"`{item.get('action', '?')}` "
                    f"{item.get('file', '(unknown)')} -> "
                    f"{item.get('target', 'none')}: "
                    f"{item.get('reason', '')}"
                )
        curated = classification.get("curated") or []
        if isinstance(curated, list) and curated:
            lines.extend(["", "**Curated candidates**"])
            for item in curated[:_MAX_REPORT_ITEMS]:
                if not isinstance(item, dict):
                    continue
                lines.append(
                    "- "
                    f"`{item.get('action', '?')}` "
                    f"{item.get('match', '(unknown)')}: "
                    f"{item.get('reason', '')}"
                )
        recommendations = classification.get("recommendations") or []
        if isinstance(recommendations, list) and recommendations:
            lines.extend(["", "**Recommendations**"])
            for rec in recommendations[:_MAX_REPORT_ITEMS]:
                lines.append(f"- {rec}")
        return "\n".join(lines)


class _MemoryGcError(Exception):
    pass


__all__ = ["MemoryGcWorkflow"]
