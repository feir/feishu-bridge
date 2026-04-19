"""Tests for feishu_bridge.paths (Phase 6.2 workspace policy).

Covers canonical/adapter rules for:
- $AGENTS_HOME / $CLAUDE_HOME / $FEISHU_BRIDGE_HOME env resolution
- default cwd per runner_type (Claude vs non-Claude)
- project long-term memory target `<repo>/.agents/ctx` with legacy `<repo>/.claude/ctx`
- raw session archives route to `$AGENTS_HOME/memory/sessions`, not repo ctx
- skill source resolution prefers `~/.agents/skills` over `~/.claude/skills`
- AGENTS.md is canonical for universal rules
"""

from __future__ import annotations

from pathlib import Path

import pytest

from feishu_bridge import paths


# ---------- env resolution ----------

def test_agents_home_defaults_to_home_dot_agents(monkeypatch):
    monkeypatch.delenv("AGENTS_HOME", raising=False)
    assert paths.agents_home() == Path.home() / ".agents"


def test_agents_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTS_HOME", str(tmp_path))
    assert paths.agents_home() == tmp_path


def test_claude_home_defaults_to_home_dot_claude(monkeypatch):
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    assert paths.claude_home() == Path.home() / ".claude"


def test_claude_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert paths.claude_home() == tmp_path


def test_bridge_home_defaults_to_home_dot_feishu_bridge(monkeypatch):
    monkeypatch.delenv("FEISHU_BRIDGE_HOME", raising=False)
    assert paths.bridge_home() == Path.home() / ".feishu-bridge"


def test_bridge_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("FEISHU_BRIDGE_HOME", str(tmp_path))
    assert paths.bridge_home() == tmp_path


def test_env_vars_expand_tilde(monkeypatch):
    monkeypatch.setenv("AGENTS_HOME", "~/custom-agents")
    assert paths.agents_home() == Path.home() / "custom-agents"


# ---------- default_runner_workspace ----------

def test_claude_runner_defaults_to_claude_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert paths.default_runner_workspace("claude") == tmp_path


def test_pi_runner_defaults_to_bridge_workspaces(monkeypatch, tmp_path):
    """Non-Claude runners must NOT inherit ~/.claude as default workspace."""
    monkeypatch.setenv("FEISHU_BRIDGE_HOME", str(tmp_path))
    assert paths.default_runner_workspace("pi") == tmp_path / "workspaces" / "default"


def test_codex_and_local_share_non_claude_default(monkeypatch, tmp_path):
    monkeypatch.setenv("FEISHU_BRIDGE_HOME", str(tmp_path))
    expected = tmp_path / "workspaces" / "default"
    assert paths.default_runner_workspace("codex") == expected
    assert paths.default_runner_workspace("local") == expected


def test_unknown_runner_type_uses_non_claude_default(monkeypatch, tmp_path):
    """Safe default: unknown runner_type gets bridge workspace, never claude_home."""
    monkeypatch.setenv("FEISHU_BRIDGE_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_HOME", "/should/not/be/used")
    assert paths.default_runner_workspace("mystery") == tmp_path / "workspaces" / "default"
    assert paths.default_runner_workspace("") == tmp_path / "workspaces" / "default"


def test_runner_type_case_insensitive(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert paths.default_runner_workspace("CLAUDE") == tmp_path
    assert paths.default_runner_workspace("Claude") == tmp_path


# ---------- project ctx routing ----------

def test_project_ctx_dir_is_repo_dot_agents_ctx(tmp_path):
    """Canonical target per Phase 6.2: <repo>/.agents/ctx."""
    assert paths.project_ctx_dir(tmp_path) == tmp_path / ".agents" / "ctx"


def test_legacy_project_ctx_dir_is_repo_dot_claude_ctx(tmp_path):
    """Legacy adapter remains readable during migration."""
    assert paths.legacy_project_ctx_dir(tmp_path) == tmp_path / ".claude" / "ctx"


def test_session_archive_root_lives_under_agents_home(monkeypatch, tmp_path):
    """Raw session archives go to $AGENTS_HOME/memory/sessions, not repo-local."""
    monkeypatch.setenv("AGENTS_HOME", str(tmp_path))
    assert paths.session_archive_root() == tmp_path / "memory" / "sessions"


def test_bridge_state_root_is_bridge_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FEISHU_BRIDGE_HOME", str(tmp_path))
    assert paths.bridge_state_root() == tmp_path


# ---------- is_safe_project_ctx_target ----------

def test_safe_project_ctx_accepts_canonical_agents_ctx(tmp_path):
    target = tmp_path / ".agents" / "ctx" / "architecture.md"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    assert paths.is_safe_project_ctx_target(target, tmp_path) is True


def test_safe_project_ctx_accepts_legacy_claude_ctx(tmp_path):
    target = tmp_path / ".claude" / "ctx" / "known-pitfalls.md"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    assert paths.is_safe_project_ctx_target(target, tmp_path) is True


def test_safe_project_ctx_rejects_session_archive_shape(tmp_path):
    """Raw session archives must not become repo-local project ctx."""
    # Even if someone tries to drop an archive under .agents/ctx, reject.
    bad = tmp_path / ".agents" / "ctx" / "sessions" / "2026-04-19.md"
    assert paths.is_safe_project_ctx_target(bad, tmp_path) is False


def test_safe_project_ctx_rejects_outside_repo(tmp_path, monkeypatch):
    outside = tmp_path.parent / "other" / "ctx.md"
    assert paths.is_safe_project_ctx_target(outside, tmp_path) is False


def test_safe_project_ctx_rejects_non_ctx_subdir(tmp_path):
    assert paths.is_safe_project_ctx_target(
        tmp_path / ".agents" / "memory" / "x.md", tmp_path
    ) is False
    assert paths.is_safe_project_ctx_target(
        tmp_path / "src" / "x.md", tmp_path
    ) is False


# ---------- resolve_skill_source ----------

def test_resolve_skill_prefers_agents_over_claude(monkeypatch, tmp_path):
    agents = tmp_path / "agents"
    claude = tmp_path / "claude"
    (agents / "skills" / "plan").mkdir(parents=True)
    (claude / "skills" / "plan").mkdir(parents=True)
    monkeypatch.setenv("AGENTS_HOME", str(agents))
    monkeypatch.setenv("CLAUDE_HOME", str(claude))
    assert paths.resolve_skill_source("plan") == agents / "skills" / "plan"


def test_resolve_skill_falls_back_to_claude(monkeypatch, tmp_path):
    agents = tmp_path / "agents"
    claude = tmp_path / "claude"
    agents.mkdir()
    (claude / "skills" / "save").mkdir(parents=True)
    monkeypatch.setenv("AGENTS_HOME", str(agents))
    monkeypatch.setenv("CLAUDE_HOME", str(claude))
    assert paths.resolve_skill_source("save") == claude / "skills" / "save"


def test_resolve_skill_returns_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTS_HOME", str(tmp_path / "a"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "c"))
    assert paths.resolve_skill_source("nonexistent") is None


def test_resolve_skill_strips_leading_slash(monkeypatch, tmp_path):
    agents = tmp_path / "agents"
    (agents / "skills" / "plan").mkdir(parents=True)
    monkeypatch.setenv("AGENTS_HOME", str(agents))
    assert paths.resolve_skill_source("/plan") == agents / "skills" / "plan"


def test_resolve_skill_empty_name_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTS_HOME", str(tmp_path))
    assert paths.resolve_skill_source("") is None
    assert paths.resolve_skill_source("   ") is None


# ---------- AGENTS.md canonical ----------

def test_agents_md_found_when_present(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTS_HOME", str(tmp_path))
    md = tmp_path / "AGENTS.md"
    md.write_text("---\nname: agents-home\n---\n", encoding="utf-8")
    assert paths.resolve_agents_md() == md


def test_agents_md_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTS_HOME", str(tmp_path))
    assert paths.resolve_agents_md() is None


# ---------- shipped layout sanity ----------

def test_shipped_layout_has_agents_md():
    """Sanity: repo author has seeded ~/.agents/AGENTS.md."""
    real = Path.home() / ".agents" / "AGENTS.md"
    if not real.is_file():
        pytest.skip("~/.agents/AGENTS.md not seeded in this environment")
    # Just confirm it parses as UTF-8 and contains frontmatter start.
    text = real.read_text(encoding="utf-8")
    assert text.startswith("---")
