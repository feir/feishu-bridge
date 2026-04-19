"""DoneWorkflow — bridge-owned /done MVP for non-Claude runners (Phase 6.7)."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from feishu_bridge.paths import project_ctx_dir, session_archive_root
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

_DEFAULT_TTL_SECONDS = 2 * 3600
_EXTRACT_REQUIRED = [
    "title",
    "activities",
    "decisions",
    "lessons",
    "open_loops",
    "noise_filtered",
]
_MAX_JOURNAL_ENTRIES = 80
_MAX_ENTRY_CHARS = 1200


class DoneWorkflow:
    """Journal-driven session archival with confirmation before writes."""

    name = "done"
    version = 1

    def __init__(self, skill_dir: Path, ttl_string: str | None = "2h") -> None:
        self.skill_dir = Path(skill_dir)
        self.ttl_seconds = parse_ttl_seconds(ttl_string, _DEFAULT_TTL_SECONDS)

    def start(self, ctx: WorkflowContext, arg: str) -> WorkflowResult:
        journal_text = self._journal_excerpt(ctx)
        if not journal_text.strip():
            return WorkflowResult(
                state=STATE_FAILED,
                user_message="`/done` 没有可归档的 bridge journal 内容。",
                error="empty journal",
            )

        prompt = self._build_extract_prompt(ctx, arg, journal_text)
        try:
            extraction = request_json_with_policy(
                runner_call=lambda p: ctx.runner.run(
                    p, session_id=ctx.session_id, resume=False,
                    tag=None, on_output=None,
                ),
                base_prompt=prompt,
                schema_required=_EXTRACT_REQUIRED,
                log_prefix=f"done[{ctx.scope_key}]",
            )
            self._validate_extraction(extraction)
        except (JsonPolicyError, _DoneError) as exc:
            return WorkflowResult(
                state=STATE_FAILED,
                user_message=(
                    "`/done` 提取失败：未生成有效归档 JSON，未修改任何文件。"
                ),
                error=str(exc),
            )

        preview = self._render_preview(extraction)
        return WorkflowResult(
            state=STATE_WAITING_CONFIRMATION,
            user_message=(
                f"📦 `/done` 归档草稿：\n\n{preview}\n\n"
                "回复 `/confirm` 写入归档，或 `/stop` 放弃。"
                f"草稿保留 {self._ttl_label()}。"
            ),
            next_expected_input="/confirm 或 /stop",
            expires_at=time.time() + self.ttl_seconds,
            payload={
                "extraction": extraction,
                "notes": arg or "",
                "workspace": str(ctx.workspace),
                "created_at": time.time(),
            },
        )

    def resume_confirm(
        self, ctx: WorkflowContext, payload: dict,
    ) -> WorkflowResult:
        extraction = payload.get("extraction")
        if not isinstance(extraction, dict):
            return WorkflowResult(
                state=STATE_FAILED,
                user_message="`/confirm` 失败：`/done` 草稿状态损坏。",
                error="payload missing extraction",
                payload=payload,
            )
        try:
            self._validate_extraction(extraction)
            artifacts = self._write_archive(ctx, extraction)
        except (OSError, _DoneError) as exc:
            return WorkflowResult(
                state=STATE_FAILED,
                user_message=f"`/done` 写入失败: {exc}",
                error=str(exc),
                payload=payload,
            )
        return WorkflowResult(
            state=STATE_COMPLETED,
            user_message=self._render_final(extraction, artifacts),
            artifacts=[str(p) for p in artifacts.values() if p],
            payload={**payload, "artifacts": {k: str(v) for k, v in artifacts.items() if v}},
        )

    def resume_cancel(
        self, ctx: WorkflowContext, payload: dict,
    ) -> WorkflowResult:
        title = (payload.get("extraction") or {}).get("title", "untitled")
        return WorkflowResult(
            state=STATE_CANCELLED,
            user_message=f"已放弃 `/done` 归档草稿 `{title}`。",
            payload=payload,
        )

    # ---- extraction --------------------------------------------------

    def _journal_excerpt(self, ctx: WorkflowContext) -> str:
        if ctx.journal is None:
            return ""
        try:
            entries = list(ctx.journal.read(ctx.bot_id, ctx.chat_id, ctx.thread_id))
        except Exception as exc:
            log.warning("done: journal read failed: %s", exc)
            return ""
        tail = entries[-_MAX_JOURNAL_ENTRIES:]
        lines: list[str] = []
        for entry in tail:
            kind = entry.get("kind", "")
            if kind in ("user_turn", "assistant_turn"):
                role = "user" if kind == "user_turn" else "assistant"
                text = str(entry.get("text") or "").strip()
                if len(text) > _MAX_ENTRY_CHARS:
                    text = text[:_MAX_ENTRY_CHARS].rstrip() + "...[truncated]"
                lines.append(f"### {role}\n{text}")
            elif kind == "workflow_event":
                lines.append(
                    "### workflow\n"
                    f"{entry.get('command', '')} -> {entry.get('decision', '')}"
                )
            elif kind == "artifact":
                lines.append(f"### artifact\n{entry.get('path', '')}")
        return "\n\n".join(lines)

    def _build_extract_prompt(
        self, ctx: WorkflowContext, notes: str, journal_text: str,
    ) -> str:
        return f"""Extract a session archive from the bridge journal.

Return ONLY JSON with required keys:
- title: short session title
- activities: array of {{project, summary, details[]}}
- decisions: array of {{decision, rationale}}
- lessons: array of {{project, category, scope, title, lesson, prevention}}
- open_loops: array of short strings or {{text, project}}
- noise_filtered: integer

Rules:
- Do not invent completed work that is not supported by journal evidence.
- Keep lessons only when they are generalizable and preventable.
- Use project slug `{ctx.workspace.name or "unscoped"}` when uncertain.
- User notes: {notes or "(none)"}

## Bridge journal

{journal_text}
"""

    def _validate_extraction(self, data: dict[str, Any]) -> None:
        for key in _EXTRACT_REQUIRED:
            if key not in data:
                raise _DoneError(f"missing {key}")
        if not str(data.get("title") or "").strip():
            raise _DoneError("empty title")
        for key in ("activities", "decisions", "lessons", "open_loops"):
            if not isinstance(data.get(key), list):
                raise _DoneError(f"{key} must be array")
        if not isinstance(data.get("noise_filtered"), int):
            raise _DoneError("noise_filtered must be integer")

    # ---- writes ------------------------------------------------------

    def _write_archive(
        self, ctx: WorkflowContext, extraction: dict[str, Any],
    ) -> dict[str, Path | None]:
        project = self._project_slug(ctx, extraction)
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M")

        session_dir = session_archive_root() / project
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / f"{date_str}.md"
        session_text = self._render_session_section(
            extraction, project=project, time_str=time_str, workspace=ctx.workspace,
        )
        with open(session_file, "a", encoding="utf-8") as fp:
            if session_file.stat().st_size > 0:
                fp.write("\n---\n")
            fp.write(session_text)

        lessons_file = self._append_lessons(extraction, date_str)
        ctx_file = self._append_ctx_timeline(ctx.workspace, extraction, date_str, time_str)
        return {
            "session": session_file,
            "lessons": lessons_file,
            "ctx": ctx_file,
        }

    def _append_lessons(
        self, extraction: dict[str, Any], date_str: str,
    ) -> Path | None:
        lessons = extraction.get("lessons") or []
        if not lessons:
            return None
        lessons_dir = session_archive_root().parent / "lessons"
        lessons_dir.mkdir(parents=True, exist_ok=True)
        path = lessons_dir / f"{date_str}.md"
        lines = []
        for lesson in lessons:
            if not isinstance(lesson, dict):
                continue
            lesson_text = str(lesson.get("lesson") or "").strip()
            prevention = str(lesson.get("prevention") or "").strip()
            if len(lesson_text) < 20 or len(prevention) < 10:
                continue
            project = str(lesson.get("project") or "-").strip()
            cat = str(lesson.get("category") or "PROCESS").strip()
            title = str(lesson.get("title") or "Untitled").strip()
            prefix = f"[{project}] " if project and project != "-" else ""
            lines.append(
                f"- {prefix}[{cat}] **{title}**："
                f"{lesson_text.rstrip('。.！!')}。防错：{prevention.rstrip('。.！!')}"
            )
        if not lines:
            return None
        with open(path, "a", encoding="utf-8") as fp:
            fp.write("\n".join(lines) + "\n")
        return path

    def _append_ctx_timeline(
        self, workspace: Path, extraction: dict[str, Any], date_str: str, time_str: str,
    ) -> Path:
        ctx_dir = project_ctx_dir(workspace)
        ctx_dir.mkdir(parents=True, exist_ok=True)
        path = ctx_dir / "session-timeline.md"
        lines = [
            f"## {date_str} {time_str} — {extraction.get('title', 'Untitled')}",
        ]
        for activity in extraction.get("activities") or []:
            if isinstance(activity, dict):
                lines.append(
                    f"- {activity.get('project', '-')}: {activity.get('summary', '')}"
                )
        open_loops = extraction.get("open_loops") or []
        if open_loops:
            lines.append("- next: " + "; ".join(self._loop_text(x) for x in open_loops[:5]))
        with open(path, "a", encoding="utf-8") as fp:
            fp.write("\n".join(lines).rstrip() + "\n\n")
        return path

    # ---- rendering ---------------------------------------------------

    def _render_preview(self, extraction: dict[str, Any]) -> str:
        title = extraction.get("title", "Untitled")
        activities = extraction.get("activities") or []
        lessons = extraction.get("lessons") or []
        open_loops = extraction.get("open_loops") or []
        lines = [
            f"**{title}**",
            f"- activities: `{len(activities)}`",
            f"- lessons: `{len(lessons)}`",
            f"- open loops: `{len(open_loops)}`",
            f"- noise filtered: `{extraction.get('noise_filtered', 0)}`",
        ]
        for activity in activities[:5]:
            if isinstance(activity, dict):
                lines.append(
                    f"- {activity.get('project', '-')}: {activity.get('summary', '')}"
                )
        return "\n".join(lines)

    def _render_session_section(
        self, extraction: dict[str, Any], *, project: str,
        time_str: str, workspace: Path,
    ) -> str:
        lines = [
            f"<!-- project={project} root={workspace} -->",
            f"## Session 归档 [{time_str}]",
            f"**主题**: {extraction.get('title', 'Untitled')}",
            "",
        ]
        activities = extraction.get("activities") or []
        if activities:
            lines.append("**产出**:")
            for activity in activities:
                if not isinstance(activity, dict):
                    continue
                lines.append(
                    f"### {activity.get('project', '-')} — {activity.get('summary', '')}"
                )
                for detail in activity.get("details") or []:
                    lines.append(f"- {detail}")
                lines.append("")
        decisions = extraction.get("decisions") or []
        if decisions:
            lines.append("**决策**:")
            for dec in decisions:
                if isinstance(dec, dict):
                    lines.append(
                        f"- **{dec.get('decision', '')}**：{dec.get('rationale', '')}"
                    )
            lines.append("")
        lessons = extraction.get("lessons") or []
        if lessons:
            lines.append("**教训**:")
            for lesson in lessons:
                if isinstance(lesson, dict):
                    cat = lesson.get("category", "PROCESS")
                    title = lesson.get("title", "")
                    text = str(lesson.get("lesson", "")).rstrip("。.！!")
                    prevention = str(lesson.get("prevention", "")).rstrip("。.！!")
                    lines.append(f"- [{cat}] **{title}**：{text}。防错：{prevention}")
            lines.append("")
        open_loops = extraction.get("open_loops") or []
        if open_loops:
            lines.append("**未关闭事项**:")
            for loop in open_loops:
                lines.append(f"- {self._loop_text(loop)}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _render_final(
        self, extraction: dict[str, Any], artifacts: dict[str, Path | None],
    ) -> str:
        lines = [
            f"**{extraction.get('title', 'Untitled')}**",
            f"- activities: {len(extraction.get('activities') or [])}",
            f"- lessons: {len(extraction.get('lessons') or [])}",
            f"- noise filtered: {extraction.get('noise_filtered', 0)}",
            "",
            "已归档：",
        ]
        for label, path in artifacts.items():
            if path:
                lines.append(f"- `{label}`: `{path}`")
        return "\n".join(lines)

    def _project_slug(self, ctx: WorkflowContext, extraction: dict[str, Any]) -> str:
        for activity in extraction.get("activities") or []:
            if isinstance(activity, dict):
                value = str(activity.get("project") or "").strip()
                if value and value not in {"-", "unscoped", "multi-project"}:
                    return self._slug(value)
        return self._slug(ctx.workspace.name or "unscoped")

    def _loop_text(self, item: Any) -> str:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            project = str(item.get("project") or "").strip()
            return f"[{project}] {text}" if project and project != "-" else text
        return str(item).strip()

    def _slug(self, value: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
        return slug or "unscoped"

    def _ttl_label(self) -> str:
        hours = max(1, int(self.ttl_seconds / 3600))
        return f"{hours}h"


class _DoneError(Exception):
    pass


__all__ = ["DoneWorkflow"]
