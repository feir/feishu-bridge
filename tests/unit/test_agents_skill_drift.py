"""Unit tests for scripts/agents-skill-drift.py (Phase 0.2 gate).

Drive the drift script via subprocess against fixture directories. We pin
AGENTS_HOME and CLAUDE_HOME to temp dirs so tests are hermetic and do not
observe or mutate the developer's actual ~/.agents/ or ~/.claude/ state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "agents-skill-drift.py"


def _run(agents_home: Path, claude_home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "AGENTS_HOME": str(agents_home),
        "CLAUDE_HOME": str(claude_home),
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def homes(tmp_path: Path) -> tuple[Path, Path]:
    agents = tmp_path / "agents"
    claude = tmp_path / "claude"
    (agents / "skills").mkdir(parents=True)
    (agents / "agents").mkdir(parents=True)
    (agents / "rules").mkdir(parents=True)
    (agents / "bin").mkdir(parents=True)
    (claude / "skills").mkdir(parents=True)
    (claude / "agents").mkdir(parents=True)
    (claude / "rules").mkdir(parents=True)
    return agents, claude


def test_list_enumerates_known_checks():
    """--list must expose the contract of available checks so Phase-gate docs stay accurate."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--list"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    names = result.stdout.strip().splitlines()
    expected = {
        "backup_in_skills",
        "hardcoded_claude_paths",
        "duplicate_skills",
        "executable_bits",
        "agent_dual_artifact",
        "session_history_index",
        "symlinks",
        "rules_adapter",
    }
    assert expected.issubset(set(names))


def test_hardcoded_claude_paths_clean(homes: tuple[Path, Path]):
    """Canonical file with no ~/.claude/ references → OK."""
    agents, claude = homes
    skill = agents / "skills" / "demo"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo\n---\nUse AGENTS_HOME/skills/demo/scripts/foo.\n"
    )
    result = _run(agents, claude, "--check", "hardcoded_claude_paths", "--json")
    assert result.returncode == 0, result.stderr
    findings = json.loads(result.stdout)
    assert [f["severity"] for f in findings] == ["OK"]


def test_hardcoded_claude_paths_detected(homes: tuple[Path, Path]):
    """Canonical file with ~/.claude/ reference → ERROR + non-zero exit."""
    agents, claude = homes
    skill = agents / "skills" / "bad"
    skill.mkdir()
    (skill / "SKILL.md").write_text("Run `~/.claude/bin/foo` to kick off.\n")
    result = _run(agents, claude, "--check", "hardcoded_claude_paths", "--json")
    assert result.returncode == 1, result.stderr
    findings = json.loads(result.stdout)
    severities = [f["severity"] for f in findings]
    assert "ERROR" in severities
    err = next(f for f in findings if f["severity"] == "ERROR")
    assert err["details"]["file"].endswith("SKILL.md")


def test_backup_in_skills_flags_snapshot_dir(homes: tuple[Path, Path]):
    """A backup/snapshot-named dir under $CLAUDE_HOME/skills/ must fail the gate."""
    agents, claude = homes
    (claude / "skills" / ".done.snapshot.20260420").mkdir()
    result = _run(agents, claude, "--check", "backup_in_skills", "--json")
    assert result.returncode == 1, result.stderr
    findings = json.loads(result.stdout)
    assert any(
        f["severity"] == "ERROR" and ".done.snapshot" in f["details"].get("path", "")
        for f in findings
    )


def test_unknown_check_exits_2(homes: tuple[Path, Path]):
    """Requesting a non-existent check name must exit 2 (operator error)."""
    agents, claude = homes
    result = _run(agents, claude, "--check", "does_not_exist")
    assert result.returncode == 2
    assert "unknown check" in result.stderr
