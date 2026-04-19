"""Tests for feishu_bridge.workflows.registry (Phase 6.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from feishu_bridge.workflows import (
    CommandPolicy,
    DECISION_BRIDGE_WORKFLOW,
    DECISION_CLAUDE_NATIVE,
    DECISION_UNSUPPORTED,
    INTERCEPT_ALWAYS,
    INTERCEPT_AUTO,
    INTERCEPT_NEVER,
    WorkflowRegistry,
)


# ---------- fixtures ----------

def _write_skill(
    root: Path,
    name: str,
    *,
    triggers: list[str] | None = None,
    runners: dict[str, str] | None = None,
    ttl: str | None = None,
    description: str = "",
) -> Path:
    """Create a minimal ~/.agents/skills/<name>/{SKILL.md,workflow.yaml}."""
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    # YAML list rendering
    trigger_block = ""
    if triggers is not None:
        lines = "\n".join(f"  - {t}" for t in triggers)
        trigger_block = f"triggers:\n{lines}\n"

    runner_block = ""
    if runners is not None:
        lines = "\n".join(f"  {k}: {v}" for k, v in runners.items())
        runner_block = f"runners:\n{lines}\n"

    frontmatter = (
        f"---\n"
        f"name: {name}\n"
        f"description: {description or name + ' skill'}\n"
        f"{trigger_block}"
        f"{runner_block}"
        f"---\n"
        f"\n# {name}\n\nBody content.\n"
    )
    (skill_dir / "SKILL.md").write_text(frontmatter, encoding="utf-8")

    wf_lines = [f"name: {name}", "version: 1"]
    if ttl:
        wf_lines.append(f"ttl: {ttl}")
    (skill_dir / "workflow.yaml").write_text("\n".join(wf_lines) + "\n", encoding="utf-8")

    return skill_dir


def _write_native_registry(root: Path, entries: list[dict]) -> Path:
    adapters = root / "adapters" / "bridge"
    adapters.mkdir(parents=True, exist_ok=True)
    path = adapters / "command-registry.yaml"
    yaml_lines = ["version: 1", "claude_native_skills:"]
    for e in entries:
        yaml_lines.append(f"  - name: {e['name']}")
        if "description" in e:
            yaml_lines.append(f"    description: {e['description']}")
    path.write_text("\n".join(yaml_lines) + "\n", encoding="utf-8")
    return path


# ---------- scaffold & parse ----------

def test_load_empty_agents_home(tmp_path: Path):
    """Missing skills/ directory yields empty policy (no crash)."""
    policy = WorkflowRegistry(agents_home=tmp_path).load()
    assert policy.skills == {}
    assert policy.claude_native_only == {}
    assert policy.intercept_mode == INTERCEPT_AUTO


def test_parse_skill_frontmatter_and_ttl(tmp_path: Path):
    _write_skill(
        tmp_path, "plan",
        triggers=["/plan"],
        runners={"claude": "native", "pi": "bridge_workflow"},
        ttl="7d",
        description="Plan skill",
    )
    policy = WorkflowRegistry(agents_home=tmp_path).load()
    md = policy.skills["plan"]
    assert md.name == "plan"
    assert md.triggers == ["/plan"]
    assert md.runners == {"claude": "native", "pi": "bridge_workflow"}
    assert md.ttl == "7d"
    assert md.description == "Plan skill"
    assert md.workflow_version == 1


def test_trigger_aliases_share_metadata(tmp_path: Path):
    """/done, 结束会话, 归档 all resolve to the same SkillMetadata."""
    _write_skill(
        tmp_path, "done",
        triggers=["/done", "结束会话", "归档"],
        runners={"claude": "native", "pi": "bridge_workflow"},
        ttl="2h",
    )
    policy = WorkflowRegistry(agents_home=tmp_path).load()
    assert policy.skills["done"] is policy.skills["结束会话"]
    assert policy.skills["done"] is policy.skills["归档"]


def test_known_skill_commands_lists_canonical_only(tmp_path: Path):
    _write_skill(tmp_path, "plan", triggers=["/plan"], runners={"claude": "native"})
    _write_skill(tmp_path, "done", triggers=["/done", "归档"], runners={"claude": "native"})
    policy = WorkflowRegistry(agents_home=tmp_path).load()
    assert policy.known_skill_commands() == ["done", "plan"]


# ---------- resolve() — three decisions ----------

def test_resolve_native_claude_passes_through(tmp_path: Path):
    _write_skill(tmp_path, "plan", runners={"claude": "native", "pi": "bridge_workflow"})
    policy = WorkflowRegistry(agents_home=tmp_path).load()
    dec = policy.resolve("/plan", "claude")
    assert dec.decision == DECISION_CLAUDE_NATIVE
    assert dec.skill is not None
    assert dec.skill.name == "plan"


def test_resolve_bridge_workflow_for_pi(tmp_path: Path):
    _write_skill(tmp_path, "plan", runners={"claude": "native", "pi": "bridge_workflow"})
    policy = WorkflowRegistry(agents_home=tmp_path).load()
    dec = policy.resolve("/plan", "pi")
    assert dec.decision == DECISION_BRIDGE_WORKFLOW
    assert dec.skill.name == "plan"


def test_resolve_unsupported_explicit_status(tmp_path: Path):
    _write_skill(
        tmp_path, "memory-gc",
        runners={"claude": "native", "pi": "bridge_workflow", "mystery": "unsupported"},
    )
    policy = WorkflowRegistry(agents_home=tmp_path).load()
    dec = policy.resolve("/memory-gc", "mystery")
    assert dec.decision == DECISION_UNSUPPORTED


def test_resolve_missing_runner_defaults_unsupported(tmp_path: Path):
    """Runner key absent from SKILL.md runners map → unsupported."""
    _write_skill(tmp_path, "plan", runners={"claude": "native"})
    policy = WorkflowRegistry(agents_home=tmp_path).load()
    dec = policy.resolve("/plan", "codex")  # codex not listed
    assert dec.decision == DECISION_UNSUPPORTED


# ---------- intercept_mode ----------

def test_intercept_always_overrides_native(tmp_path: Path):
    _write_skill(tmp_path, "plan", runners={"claude": "native"})
    policy = WorkflowRegistry(
        agents_home=tmp_path, intercept_mode=INTERCEPT_ALWAYS,
    ).load()
    dec = policy.resolve("/plan", "claude")
    assert dec.decision == DECISION_BRIDGE_WORKFLOW


def test_intercept_never_forces_passthrough(tmp_path: Path):
    _write_skill(tmp_path, "plan", runners={"claude": "native", "pi": "bridge_workflow"})
    policy = WorkflowRegistry(
        agents_home=tmp_path, intercept_mode=INTERCEPT_NEVER,
    ).load()
    dec = policy.resolve("/plan", "pi")
    assert dec.decision == DECISION_CLAUDE_NATIVE


def test_invalid_intercept_mode_raises():
    with pytest.raises(ValueError):
        WorkflowRegistry(intercept_mode="bogus")


# ---------- Claude-native fallback ----------

def test_claude_native_fallback_for_claude(tmp_path: Path):
    _write_native_registry(tmp_path, [
        {"name": "save", "description": "Save to Obsidian"},
        {"name": "research"},
    ])
    policy = WorkflowRegistry(agents_home=tmp_path).load()
    dec = policy.resolve("/save", "claude")
    assert dec.decision == DECISION_CLAUDE_NATIVE
    assert dec.skill is None  # fallback has no SkillMetadata


def test_claude_native_fallback_rejects_pi(tmp_path: Path):
    _write_native_registry(tmp_path, [{"name": "save", "description": "claude-only"}])
    policy = WorkflowRegistry(agents_home=tmp_path).load()
    dec = policy.resolve("/save", "pi")
    assert dec.decision == DECISION_UNSUPPORTED


def test_known_claude_native_commands(tmp_path: Path):
    _write_native_registry(tmp_path, [
        {"name": "save"}, {"name": "research"}, {"name": "wiki"},
    ])
    policy = WorkflowRegistry(agents_home=tmp_path).load()
    assert policy.known_claude_native_commands() == ["research", "save", "wiki"]


# ---------- unknown / normalization ----------

def test_unknown_command_defers_to_runner(tmp_path: Path):
    policy = WorkflowRegistry(agents_home=tmp_path).load()
    dec = policy.resolve("/nonexistent", "claude")
    assert dec.decision == DECISION_CLAUDE_NATIVE  # runner decides
    assert dec.skill is None


def test_resolve_normalizes_command(tmp_path: Path):
    _write_skill(tmp_path, "plan", runners={"claude": "native", "pi": "bridge_workflow"})
    policy = WorkflowRegistry(agents_home=tmp_path).load()
    # Case-insensitive, leading slash optional
    assert policy.resolve("/PLAN", "pi").decision == DECISION_BRIDGE_WORKFLOW
    assert policy.resolve("plan", "pi").decision == DECISION_BRIDGE_WORKFLOW


def test_skill_without_frontmatter_skipped(tmp_path: Path):
    """SKILL.md without YAML frontmatter is ignored, not raised."""
    bad = tmp_path / "skills" / "broken"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("# just a body\n", encoding="utf-8")
    policy = WorkflowRegistry(agents_home=tmp_path).load()
    assert "broken" not in policy.skills


# ---------- Phase 6.1 baseline scaffold present ----------

def test_shipped_scaffold_loads(monkeypatch, tmp_path: Path):
    """Sanity: the repo's ~/.agents layout parses without error.

    Uses the real AGENTS_HOME on disk. Skipped if the user has not yet
    seeded ~/.agents/skills/plan (e.g. CI without this workflow).
    """
    real = Path.home() / ".agents"
    if not (real / "skills" / "plan" / "SKILL.md").exists():
        pytest.skip("~/.agents/skills/plan not seeded in this environment")
    policy = WorkflowRegistry(agents_home=real).load()
    assert "plan" in policy.skills
    plan = policy.skills["plan"]
    # Per Phase 6.1 design: claude native, pi/codex bridge_workflow.
    assert plan.runners.get("claude") == "native"
    assert plan.runners.get("pi") == "bridge_workflow"
