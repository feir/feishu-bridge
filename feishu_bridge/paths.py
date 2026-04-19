"""Workspace and memory routing policy (Phase 6.2 of pi-runner change).

Centralizes the canonical/adapter rules the bridge uses to decide where files
live. Callers consult these helpers to pick canonical targets, fall back to
legacy adapters, or refuse unsafe writes.

Policy summary:
    - ``~/.agents``           canonical universal skills/rules/memory
    - ``~/.claude``           Claude Code home; for non-Claude runners it is a
                              legacy adapter, not the long-term default cwd
    - ``~/.feishu-bridge``    bridge runtime state (journals, workflow DB,
                              neutral workspaces)
    - ``<repo>/.agents/ctx``  canonical project long-term memory
    - ``<repo>/.claude/ctx``  legacy adapter, readable during migration
    - session archives always live under ``$AGENTS_HOME/memory/sessions``,
      never inside a repo's project ctx

Env vars (overrides, all optional):
    AGENTS_HOME, CLAUDE_HOME, FEISHU_BRIDGE_HOME

This module only resolves paths; it never mutates the filesystem.
"""

from __future__ import annotations

import os
from pathlib import Path

AGENTS_HOME_ENV = "AGENTS_HOME"
CLAUDE_HOME_ENV = "CLAUDE_HOME"
BRIDGE_HOME_ENV = "FEISHU_BRIDGE_HOME"

_DEFAULT_AGENTS_HOME = Path.home() / ".agents"
_DEFAULT_CLAUDE_HOME = Path.home() / ".claude"
_DEFAULT_BRIDGE_HOME = Path.home() / ".feishu-bridge"

_CLAUDE_RUNNER_TYPES = frozenset({"claude"})


def _resolve_env_path(env_var: str, default: Path) -> Path:
    raw = os.environ.get(env_var)
    if raw:
        return Path(raw).expanduser()
    return default


def agents_home() -> Path:
    return _resolve_env_path(AGENTS_HOME_ENV, _DEFAULT_AGENTS_HOME)


def claude_home() -> Path:
    return _resolve_env_path(CLAUDE_HOME_ENV, _DEFAULT_CLAUDE_HOME)


def bridge_home() -> Path:
    return _resolve_env_path(BRIDGE_HOME_ENV, _DEFAULT_BRIDGE_HOME)


def default_runner_workspace(runner_type: str) -> Path:
    """Default cwd for a runner profile that didn't specify ``workspace``.

    ClaudeRunner keeps ``~/.claude`` as its default so Claude Code can locate
    ``CLAUDE.md``, skills, and hooks. Pi/Codex/local/unknown runners must not
    inherit ``~/.claude`` — they get a neutral bridge-managed workspace at
    ``$FEISHU_BRIDGE_HOME/workspaces/default``.
    """
    rt = (runner_type or "").strip().lower()
    if rt in _CLAUDE_RUNNER_TYPES:
        return claude_home()
    return bridge_home() / "workspaces" / "default"


def project_ctx_dir(repo_root: Path | str) -> Path:
    """Canonical project long-term memory target: ``<repo>/.agents/ctx``."""
    return Path(repo_root) / ".agents" / "ctx"


def legacy_project_ctx_dir(repo_root: Path | str) -> Path:
    """Legacy Claude adapter: ``<repo>/.claude/ctx``. Readable during migration."""
    return Path(repo_root) / ".claude" / "ctx"


def session_archive_root() -> Path:
    """Raw session archives live at ``$AGENTS_HOME/memory/sessions``.

    These are local memory — they must not be written into repo project ctx.
    """
    return agents_home() / "memory" / "sessions"


def bridge_state_root() -> Path:
    """Runtime state root: journals, workflow DB, neutral workspaces."""
    return bridge_home()


def resolve_skill_source(name: str) -> Path | None:
    """Locate a skill directory, preferring ``agents_home`` over ``claude_home``.

    ``name`` may include a leading slash. Returns ``None`` if neither root
    contains the skill.
    """
    if not name:
        return None
    key = name.strip().lstrip("/")
    if not key:
        return None
    for root in (agents_home(), claude_home()):
        candidate = root / "skills" / key
        if candidate.is_dir():
            return candidate
    return None


def resolve_agents_md() -> Path | None:
    """Canonical universal-rules doc: ``$AGENTS_HOME/AGENTS.md``.

    Returns ``None`` if not present so callers can fall back to ``CLAUDE.md``
    during the migration window.
    """
    path = agents_home() / "AGENTS.md"
    return path if path.is_file() else None


def is_safe_project_ctx_target(
    path: Path | str, repo_root: Path | str
) -> bool:
    """Return True iff ``path`` is a permitted project-ctx write target.

    A safe target must live directly under ``<repo>/.agents/ctx/`` or the
    legacy ``<repo>/.claude/ctx/``. Paths shaped like session archives
    (containing ``sessions`` or ``archive`` segments) are rejected so raw
    journals can't leak into repo-local project context.
    """
    p = Path(path)
    r = Path(repo_root)
    try:
        rel = p.resolve().relative_to(r.resolve())
    except (ValueError, OSError):
        # On resolve failures (missing parents in tmp_path), fall back to
        # lexical relative_to.
        try:
            rel = p.relative_to(r)
        except ValueError:
            return False
    parts = rel.parts
    if len(parts) < 3:
        return False
    ns, subdir = parts[0], parts[1]
    if ns not in (".agents", ".claude"):
        return False
    if subdir != "ctx":
        return False
    if any(seg in {"sessions", "archive"} for seg in parts):
        return False
    return True


__all__ = [
    "AGENTS_HOME_ENV",
    "CLAUDE_HOME_ENV",
    "BRIDGE_HOME_ENV",
    "agents_home",
    "claude_home",
    "bridge_home",
    "default_runner_workspace",
    "project_ctx_dir",
    "legacy_project_ctx_dir",
    "session_archive_root",
    "bridge_state_root",
    "resolve_skill_source",
    "resolve_agents_md",
    "is_safe_project_ctx_target",
]
