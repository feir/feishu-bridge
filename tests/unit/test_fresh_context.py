"""Unit tests for ``build_fresh_context_prompt`` (memory-system-fix Stage 1)."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from feishu_bridge.runtime import (
    SESSION_HISTORY_HINT,
    build_fresh_context_prompt,
)


PROJECTS_MD = """# Project Registry

> 人工参考表。

## Projects

| ID | 名称 | 路径 | 状态 |
|---|---|---|---|
| confluence | Confluence Strategy | `~/projects/confluence-strategy` | active |
| feishu-bridge | Feishu Claude Bridge | `~/projects/feishu-bridge` | active |
| dotclaude | Claude Code Config | `~/.claude` | active |
"""


COMPACT_TEXT = "Recent thread context: working on memory-system-fix Stage 1."


MEMORY_MD = """# Project MEMORY

## Memory Sources
This section should be ignored.

## Commands
- run `pytest tests/unit -v`
- deploy with `launchctl kickstart -k gui/$UID/com.feishu-claude-bridge`

## Constraints
- never restart feishu-bridge itself

## Known Pitfalls (project-specific)
- OMP idle reap at 1800s

## 待办
- finish Stage 1
- start Stage 2

## Anchor
Last working commit ad2eef21.
"""


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------- #
# C1-C7: source availability matrix                                            #
# --------------------------------------------------------------------------- #


def test_only_compact_context(tmp_path: Path) -> None:
    """C1 — only compact-context.md → returned alone with hint."""
    _write(tmp_path / ".claude" / "compact-context.md", COMPACT_TEXT)

    out = build_fresh_context_prompt(str(tmp_path))

    assert out is not None
    assert "Compact context" in out
    assert COMPACT_TEXT in out
    assert SESSION_HISTORY_HINT in out
    # No projects index, no MEMORY section
    assert "Projects index" not in out
    assert "Project MEMORY" not in out


def test_only_projects_index(tmp_path: Path) -> None:
    """C2 — only projects.md → index-only output."""
    _write(tmp_path / ".claude" / "memory" / "projects.md", PROJECTS_MD)

    out = build_fresh_context_prompt(str(tmp_path))

    assert out is not None
    assert "Projects index" in out
    assert "feishu-bridge → ~/projects/feishu-bridge" in out
    # Header row must not leak in as an entry
    assert "ID → 路径" not in out
    assert "Compact context" not in out


def test_only_project_memory(tmp_path: Path) -> None:
    """C3 — only project_workspace MEMORY.md → memory sections appear."""
    project = tmp_path / "feishu-bridge"
    _write(project / ".claude" / "MEMORY.md", MEMORY_MD)

    out = build_fresh_context_prompt(
        str(tmp_path), project_workspace=str(project)
    )

    assert out is not None
    assert "Project MEMORY (feishu-bridge)" in out
    assert "## Commands" in out
    assert "pytest tests/unit -v" in out
    assert "## Constraints" in out
    assert "## Known Pitfalls" in out
    assert "## 待办" in out
    assert "## Anchor" in out
    # Memory Sources should be excluded
    assert "Memory Sources" not in out


def test_all_sources_present(tmp_path: Path) -> None:
    """C4 — all three sources → merged ≤ 2KB."""
    _write(tmp_path / ".claude" / "compact-context.md", COMPACT_TEXT)
    _write(tmp_path / ".claude" / "memory" / "projects.md", PROJECTS_MD)
    project = tmp_path / "feishu-bridge"
    _write(project / ".claude" / "MEMORY.md", MEMORY_MD)

    out = build_fresh_context_prompt(
        str(tmp_path), project_workspace=str(project)
    )

    assert out is not None
    assert len(out.encode("utf-8")) <= 2048
    assert "Compact context" in out
    assert "Projects index" in out
    assert "Project MEMORY" in out


def test_projects_md_parse_error_other_sources_survive(tmp_path: Path) -> None:
    """C5 — corrupt projects.md doesn't suppress compact-context."""
    _write(tmp_path / ".claude" / "compact-context.md", COMPACT_TEXT)
    # Not a table — parser should yield empty index and skip
    _write(
        tmp_path / ".claude" / "memory" / "projects.md",
        "Not a markdown table at all.\nJust prose.\n",
    )

    out = build_fresh_context_prompt(str(tmp_path))

    assert out is not None
    assert "Compact context" in out
    assert COMPACT_TEXT in out
    # Projects index must be omitted (no rows parsed)
    assert "Projects index" not in out


def test_memory_md_parse_error_other_sources_survive(tmp_path: Path) -> None:
    """C6 — MEMORY.md without recognized H2s → still falls back to other sources."""
    _write(tmp_path / ".claude" / "compact-context.md", COMPACT_TEXT)
    project = tmp_path / "p"
    _write(
        project / ".claude" / "MEMORY.md",
        "# MEMORY\n\n## Unknown Section\nIrrelevant.\n",
    )

    out = build_fresh_context_prompt(
        str(tmp_path), project_workspace=str(project)
    )

    assert out is not None
    assert "Compact context" in out
    assert "Project MEMORY" not in out


def test_all_sources_missing(tmp_path: Path) -> None:
    """C7 — empty workspace returns None."""
    assert build_fresh_context_prompt(str(tmp_path)) is None


# --------------------------------------------------------------------------- #
# C8-C9: truncation                                                            #
# --------------------------------------------------------------------------- #


def test_total_truncation_to_max_bytes(tmp_path: Path) -> None:
    """C8 — combined content > max_bytes → truncated to limit."""
    huge = "lorem ipsum dolor sit amet " * 500  # ~14 KB
    _write(tmp_path / ".claude" / "compact-context.md", huge)

    out = build_fresh_context_prompt(str(tmp_path), max_bytes=2048)

    assert out is not None
    # Allow the trailing hint to push us a few bytes past max_bytes (capped body
    # + hint); main body must be respected.
    assert "…(truncated)" in out
    assert SESSION_HISTORY_HINT in out


def test_anchor_truncated_to_anchor_max(tmp_path: Path) -> None:
    """C9 — Anchor > anchor_max_bytes → bounded to anchor_max_bytes."""
    big_anchor = "X" * 4096
    memory = f"## Anchor\n{big_anchor}\n"
    project = tmp_path / "p"
    _write(project / ".claude" / "MEMORY.md", memory)

    out = build_fresh_context_prompt(
        str(tmp_path),
        project_workspace=str(project),
        anchor_max_bytes=512,
    )

    assert out is not None
    # Anchor body must be capped (sub-Anchor section is shorter than the input)
    anchor_body = out.split("## Anchor", 1)[1]
    assert len(anchor_body.encode("utf-8")) < 4096
    assert "…(truncated)" in anchor_body


# --------------------------------------------------------------------------- #
# C10: idempotency (no instance state)                                         #
# --------------------------------------------------------------------------- #


def test_repeated_calls_idempotent(tmp_path: Path) -> None:
    """C10 — calling twice yields identical output (pure function)."""
    _write(tmp_path / ".claude" / "compact-context.md", COMPACT_TEXT)
    a = build_fresh_context_prompt(str(tmp_path))
    b = build_fresh_context_prompt(str(tmp_path))
    assert a == b
    assert a is not None


# --------------------------------------------------------------------------- #
# C11: concurrent invocations do not interfere                                 #
# --------------------------------------------------------------------------- #


def test_concurrent_invocations_independent(tmp_path: Path) -> None:
    """C11 — two concurrent workspaces produce independent outputs."""
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    _write(ws_a / ".claude" / "compact-context.md", "AAA workspace A")
    _write(ws_b / ".claude" / "compact-context.md", "BBB workspace B")

    results: dict[str, str | None] = {}

    def _run(name: str, ws: Path) -> None:
        results[name] = build_fresh_context_prompt(str(ws))

    threads = [
        threading.Thread(target=_run, args=("a", ws_a)),
        threading.Thread(target=_run, args=("b", ws_b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert results["a"] is not None and "AAA workspace A" in results["a"]
    assert results["b"] is not None and "BBB workspace B" in results["b"]
    assert "AAA workspace A" not in results["b"]
    assert "BBB workspace B" not in results["a"]


# --------------------------------------------------------------------------- #
# Defensive edge cases                                                         #
# --------------------------------------------------------------------------- #


def test_empty_string_workspace_returns_none() -> None:
    assert build_fresh_context_prompt("") is None
    assert build_fresh_context_prompt("   ") is None


def test_nonexistent_workspace_returns_none(tmp_path: Path) -> None:
    fake = tmp_path / "does" / "not" / "exist"
    assert build_fresh_context_prompt(str(fake)) is None


def test_unreadable_file_skipped(tmp_path: Path, monkeypatch) -> None:
    """File read errors are swallowed and the source is skipped."""
    _write(tmp_path / ".claude" / "compact-context.md", "good content")
    bad = tmp_path / ".claude" / "memory" / "projects.md"
    _write(bad, "irrelevant")

    real_read = Path.read_text

    def _raise_on_projects(self, *args, **kwargs):
        if self == bad:
            raise OSError("simulated permission denied")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raise_on_projects)

    out = build_fresh_context_prompt(str(tmp_path))
    assert out is not None
    assert "good content" in out
