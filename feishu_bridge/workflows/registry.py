"""Workflow command registry for runner-neutral dispatch.

Parses SKILL.md frontmatter + workflow.yaml from $AGENTS_HOME/skills/<name>/ and a
Claude-native fallback list from $AGENTS_HOME/adapters/bridge/command-registry.yaml
to build a CommandPolicy. CommandPolicy decides routing per (command, runner_type):

    DECISION_BRIDGE_WORKFLOW — bridge owns execution (Pi/Codex for migrated skills)
    DECISION_CLAUDE_NATIVE   — pass the slash command through to the runner as text
                               (Claude Code then runs its native skill)
    DECISION_UNSUPPORTED     — return an explicit rejection message

Frontmatter schema (extra keys are ignored by Claude Code):
    name: <skill name>
    triggers: [ "/<cmd>", ... ]         (optional, defaults to [f"/{name}"])
    runners:                            (optional; defaults to unsupported)
      claude: native | bridge_workflow | unsupported
      pi: native | bridge_workflow | unsupported
      codex: ...
      local: ...
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover — yaml is a declared dep
    yaml = None

from feishu_bridge.paths import AGENTS_HOME_ENV, agents_home as resolve_agents_home

log = logging.getLogger("feishu-bridge")


DECISION_BRIDGE_WORKFLOW = "bridge_workflow"
DECISION_CLAUDE_NATIVE = "claude_native"
DECISION_UNSUPPORTED = "unsupported"

INTERCEPT_AUTO = "auto"
INTERCEPT_ALWAYS = "always"
INTERCEPT_NEVER = "never"

_VALID_INTERCEPT_MODES = (INTERCEPT_AUTO, INTERCEPT_ALWAYS, INTERCEPT_NEVER)

_RUNNER_SUPPORT_NATIVE = "native"
_RUNNER_SUPPORT_BRIDGE = "bridge_workflow"
_RUNNER_SUPPORT_UNSUPPORTED = "unsupported"

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)


@dataclass
class SkillMetadata:
    """Parsed skill metadata from SKILL.md frontmatter + workflow.yaml."""

    name: str
    triggers: list[str]
    description: str
    runners: dict[str, str]
    ttl: str | None = None
    workflow_version: int = 1
    source_dir: Path | None = None


@dataclass
class RunnerCommandDecision:
    """Routing decision for (command, runner_type)."""

    decision: str
    skill: SkillMetadata | None
    reason: str


@dataclass
class CommandPolicy:
    """Resolves (command, runner_type) to a routing decision.

    Built by WorkflowRegistry.load(). Safe to construct empty for tests.
    """

    # Keyed by normalized command (lowercased, no leading slash). A skill registers
    # under each of its triggers so /done, 结束会话, 归档 all resolve to the same
    # SkillMetadata. Kept separate from claude_native_only so tests can inspect.
    skills: dict[str, SkillMetadata] = field(default_factory=dict)
    claude_native_only: dict[str, str] = field(default_factory=dict)
    intercept_mode: str = INTERCEPT_AUTO

    def known_skill_commands(self) -> list[str]:
        """Return workflow skill names only (not triggers, not claude-native)."""
        names = {md.name for md in self.skills.values()}
        return sorted(names)

    def known_claude_native_commands(self) -> list[str]:
        return sorted(self.claude_native_only)

    def resolve(self, command: str, runner_type: str) -> RunnerCommandDecision:
        """Decide how to route a slash command for a given runner.

        `command` may include a leading slash or not. `runner_type` is typically
        one of 'claude', 'pi', 'codex', 'local' and is lowercased.
        """
        cmd = _normalize_command(command)
        runner = (runner_type or "").lower()

        # 1. Workflow skill (bridge-aware)
        skill = self.skills.get(cmd)
        if skill is not None:
            return self._resolve_skill(skill, runner)

        # 2. Claude-native-only fallback list (skills not yet migrated to bridge)
        if cmd in self.claude_native_only:
            if runner == "claude":
                return RunnerCommandDecision(
                    DECISION_CLAUDE_NATIVE,
                    None,
                    f"/{cmd} is Claude-native; pass through to ClaudeRunner",
                )
            return RunnerCommandDecision(
                DECISION_UNSUPPORTED,
                None,
                f"/{cmd} is a Claude-native skill; runner={runner!r} cannot execute it",
            )

        # 3. Unknown slash command — not a registered skill. Defer to runner.
        return RunnerCommandDecision(
            DECISION_CLAUDE_NATIVE,
            None,
            "unknown command; defer to runner",
        )

    def _resolve_skill(
        self, skill: SkillMetadata, runner: str
    ) -> RunnerCommandDecision:
        if self.intercept_mode == INTERCEPT_NEVER:
            return RunnerCommandDecision(
                DECISION_CLAUDE_NATIVE,
                skill,
                "workflow.intercept=never; pass through",
            )

        runner_support = skill.runners.get(runner, _RUNNER_SUPPORT_UNSUPPORTED)

        if runner_support == _RUNNER_SUPPORT_NATIVE:
            if self.intercept_mode == INTERCEPT_ALWAYS:
                return RunnerCommandDecision(
                    DECISION_BRIDGE_WORKFLOW,
                    skill,
                    "workflow.intercept=always overrides native",
                )
            return RunnerCommandDecision(
                DECISION_CLAUDE_NATIVE,
                skill,
                f"skill runners[{runner}]=native; pass through",
            )

        if runner_support == _RUNNER_SUPPORT_BRIDGE:
            return RunnerCommandDecision(
                DECISION_BRIDGE_WORKFLOW,
                skill,
                f"skill runners[{runner}]=bridge_workflow",
            )

        return RunnerCommandDecision(
            DECISION_UNSUPPORTED,
            skill,
            f"skill runners[{runner}]={runner_support!r}",
        )


class WorkflowRegistry:
    """Loads skill metadata from ~/.agents and produces a CommandPolicy."""

    def __init__(
        self,
        agents_home: Path | None = None,
        intercept_mode: str = INTERCEPT_AUTO,
    ) -> None:
        self.agents_home = agents_home or resolve_agents_home()
        if intercept_mode not in _VALID_INTERCEPT_MODES:
            raise ValueError(
                f"intercept_mode must be one of {_VALID_INTERCEPT_MODES}, "
                f"got {intercept_mode!r}"
            )
        self.intercept_mode = intercept_mode

    def load(self) -> CommandPolicy:
        policy = CommandPolicy(intercept_mode=self.intercept_mode)
        self._load_skills(policy)
        self._load_claude_native_fallback(policy)
        return policy

    def _load_skills(self, policy: CommandPolicy) -> None:
        skills_dir = self.agents_home / "skills"
        if not skills_dir.is_dir():
            return
        for sub in sorted(skills_dir.iterdir()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            md = self._parse_skill_dir(sub)
            if md is None:
                continue
            # Register under canonical name and every trigger alias.
            keys = {md.name}
            for trigger in md.triggers:
                normalized = _normalize_command(trigger)
                if normalized:
                    keys.add(normalized)
            for key in keys:
                policy.skills[key] = md

    def _parse_skill_dir(self, path: Path) -> SkillMetadata | None:
        skill_md = path / "SKILL.md"
        if not skill_md.is_file():
            return None
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("workflow registry: read %s failed: %s", skill_md, exc)
            return None

        frontmatter = _parse_frontmatter(text)
        if frontmatter is None:
            log.debug("workflow registry: %s has no frontmatter", skill_md)
            return None

        name = str(frontmatter.get("name") or path.name).strip()
        if not name:
            return None

        triggers = _coerce_triggers(frontmatter.get("triggers"), name)
        runners = _coerce_runners(frontmatter.get("runners"))
        description = str(frontmatter.get("description") or "").strip()

        ttl, workflow_version = self._read_workflow_yaml(path)

        return SkillMetadata(
            name=name,
            triggers=triggers,
            description=description,
            runners=runners,
            ttl=ttl,
            workflow_version=workflow_version,
            source_dir=path,
        )

    def _read_workflow_yaml(self, path: Path) -> tuple[str | None, int]:
        workflow_yaml = path / "workflow.yaml"
        if not workflow_yaml.is_file() or yaml is None:
            return None, 1
        try:
            data = yaml.safe_load(workflow_yaml.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            log.warning(
                "workflow registry: parse %s failed: %s", workflow_yaml, exc
            )
            return None, 1
        ttl = data.get("ttl")
        ttl = str(ttl).strip() if ttl is not None else None
        version = data.get("version", 1)
        try:
            version = int(version)
        except (TypeError, ValueError):
            version = 1
        return ttl, version

    def _load_claude_native_fallback(self, policy: CommandPolicy) -> None:
        registry_path = (
            self.agents_home / "adapters" / "bridge" / "command-registry.yaml"
        )
        if not registry_path.is_file() or yaml is None:
            return
        try:
            data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            log.warning(
                "workflow registry: parse %s failed: %s", registry_path, exc
            )
            return
        entries = data.get("claude_native_skills") or []
        if not isinstance(entries, list):
            return
        for entry in entries:
            if isinstance(entry, dict) and "name" in entry:
                key = _normalize_command(entry["name"])
                if key:
                    policy.claude_native_only[key] = str(
                        entry.get("description", "")
                    ).strip()
            elif isinstance(entry, str):
                key = _normalize_command(entry)
                if key:
                    policy.claude_native_only[key] = ""


def _normalize_command(value: str) -> str:
    if value is None:
        return ""
    v = str(value).strip().lower()
    if v.startswith("/"):
        v = v[1:]
    return v


def _coerce_triggers(raw: Any, fallback_name: str) -> list[str]:
    if raw is None:
        return [f"/{fallback_name}"]
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return [f"/{fallback_name}"]
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    if not out:
        return [f"/{fallback_name}"]
    return out


def _coerce_runners(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        if not isinstance(value, str):
            continue
        v = value.strip().lower()
        if v in (
            _RUNNER_SUPPORT_NATIVE,
            _RUNNER_SUPPORT_BRIDGE,
            _RUNNER_SUPPORT_UNSUPPORTED,
        ):
            out[key.strip().lower()] = v
    return out


def _parse_frontmatter(text: str) -> dict[str, Any] | None:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    if yaml is None:
        return None
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None
