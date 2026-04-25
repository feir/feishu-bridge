#!/usr/bin/env python3
"""Agents canonical migration drift detection.

Read-only checks that report divergence between canonical (`~/.agents/`) and
Claude adapter (`~/.claude/`) layouts. Intended for CI + manual verification.

Each check returns a list of Findings with severity:
  - OK    — no drift
  - INFO  — expected state mid-migration, nothing to fix
  - WARN  — drift that requires attention but not blocking
  - ERROR — drift that blocks a phase gate

Exit code:
  0 — no ERROR findings
  1 — at least one ERROR finding
  2 — script failure (unhandled exception)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable

HOME = Path.home()
AGENTS_HOME = Path(os.environ.get("AGENTS_HOME", HOME / ".agents"))
CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME", HOME / ".claude"))

SEVERITY_RANK = {"OK": 0, "INFO": 1, "WARN": 2, "ERROR": 3}


@dataclass
class Finding:
    check: str
    severity: str
    message: str
    details: dict = field(default_factory=dict)


def _iter_dirs(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    return sorted(p for p in path.iterdir() if p.is_dir() and not p.name.startswith("."))


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- Checks ----------

def check_backup_in_skills() -> list[Finding]:
    """Backup dirs must not live under $CLAUDE_HOME/skills/ (skill loader scans all
    subdirs including dotfile-prefixed)."""
    findings: list[Finding] = []
    skills = CLAUDE_HOME / "skills"
    if not skills.is_dir():
        return findings
    for entry in skills.iterdir():
        name = entry.name
        if re.search(r"(snapshot|backup|\.bak|migration)", name, re.IGNORECASE):
            findings.append(Finding(
                check="backup_in_skills",
                severity="ERROR",
                message=f"Backup/snapshot path under $CLAUDE_HOME/skills/ will pollute slash registry",
                details={"path": str(entry)},
            ))
    if not findings:
        findings.append(Finding("backup_in_skills", "OK", "No backup artefacts under skills/"))
    return findings


def check_hardcoded_claude_paths() -> list[Finding]:
    """Canonical files should not hardcode `~/.claude/` paths (breaks Pi runtime)."""
    findings: list[Finding] = []
    roots = [AGENTS_HOME / "skills", AGENTS_HOME / "agents", AGENTS_HOME / "rules"]
    pattern = re.compile(r"~/\.claude/|\$CLAUDE_HOME|/\.claude/bin/")
    hits: list[tuple[Path, int, str]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix in {".pyc"} or p.name == ".DS_Store":
                continue
            try:
                for lineno, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                    if pattern.search(line):
                        hits.append((p, lineno, line.strip()[:120]))
            except (OSError, UnicodeDecodeError):
                continue
    if hits:
        for p, ln, snippet in hits[:20]:
            findings.append(Finding(
                check="hardcoded_claude_paths",
                severity="ERROR",
                message=f"Canonical file references ~/.claude/",
                details={"file": str(p), "line": ln, "snippet": snippet},
            ))
        if len(hits) > 20:
            findings.append(Finding(
                check="hardcoded_claude_paths",
                severity="INFO",
                message=f"... and {len(hits)-20} more hits (showing 20)",
            ))
    else:
        findings.append(Finding("hardcoded_claude_paths", "OK", "No hardcoded ~/.claude/ in canonical files"))
    return findings


def check_duplicate_skills() -> list[Finding]:
    """Skills existing in both $CLAUDE_HOME/skills/<name>/ and $AGENTS_HOME/skills/<name>/
    as independent dirs (not symlinks) are drift (mid-migration this is expected but
    past Phase 1 completion it's an error)."""
    findings: list[Finding] = []
    claude_skills = {p.name for p in _iter_dirs(CLAUDE_HOME / "skills")}
    agents_skills = {p.name for p in _iter_dirs(AGENTS_HOME / "skills")}
    shared = claude_skills & agents_skills
    for name in sorted(shared):
        cp = CLAUDE_HOME / "skills" / name
        if cp.is_symlink():
            continue
        findings.append(Finding(
            check="duplicate_skills",
            severity="WARN",
            message=f"Skill '{name}' exists in both homes as independent dir (not symlink)",
            details={"claude_path": str(cp), "agents_path": str(AGENTS_HOME/"skills"/name)},
        ))
    if not findings:
        findings.append(Finding("duplicate_skills", "OK", "No duplicate independent skill dirs"))
    return findings


def check_executable_bits() -> list[Finding]:
    """scripts/*.sh and scripts/*.py marked as entry points should preserve exec bit
    across canonical/adapter copies. We inspect canonical only (authoritative)."""
    findings: list[Finding] = []
    roots = [AGENTS_HOME / "skills", AGENTS_HOME / "bin"]
    # scripts that are explicitly entry points (per inventory)
    known_entry_points = {
        "spec-resolve.py", "spec-write.py",
        "memory-anchor-sync.sh", "session-done-apply.sh", "session-done-commit.sh",
        "session-done-format.py", "spec-archive-validate.py", "spec-archive.sh",
        "spec-check-write.py", "stale-ctx-check.sh",
        "memory-gc-archive.sh", "memory-gc-maintain.sh", "memory-gc-route.sh",
        "memory-gc-stats.sh",
    }
    checked = 0
    missing_bit: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.name not in known_entry_points:
                continue
            checked += 1
            if not (p.stat().st_mode & 0o111):
                missing_bit.append(p)
    if missing_bit:
        for p in missing_bit:
            findings.append(Finding(
                check="executable_bits",
                severity="ERROR",
                message=f"Entry-point script missing exec bit",
                details={"path": str(p)},
            ))
    else:
        findings.append(Finding(
            check="executable_bits", severity="OK",
            message=f"All {checked} canonical entry-point scripts have exec bit",
        ))
    return findings


def check_agent_dual_artifact() -> list[Finding]:
    """For B-class agents migrated to canonical, the Claude adapter's body (minus
    frontmatter) should hash-match the canonical prompt.md. Skip if canonical
    doesn't exist yet (pre-Phase 2.5)."""
    findings: list[Finding] = []
    canonical_root = AGENTS_HOME / "agents"
    if not canonical_root.is_dir():
        findings.append(Finding("agent_dual_artifact", "INFO", "Canonical agents/ not yet populated (pre-Phase 2.5)"))
        return findings
    for agent_dir in _iter_dirs(canonical_root):
        prompt = agent_dir / "prompt.md"
        adapter = CLAUDE_HOME / "agents" / f"{agent_dir.name}.md"
        if not prompt.is_file():
            findings.append(Finding(
                "agent_dual_artifact", "ERROR",
                f"Canonical agent missing prompt.md",
                {"canonical": str(agent_dir)},
            ))
            continue
        if not adapter.is_file():
            findings.append(Finding(
                "agent_dual_artifact", "ERROR",
                f"Claude adapter missing for canonical agent",
                {"canonical": str(prompt), "expected_adapter": str(adapter)},
            ))
            continue
        # strip frontmatter from adapter, compare with canonical prompt
        adapter_text = adapter.read_text()
        if adapter_text.startswith("---"):
            _, _, body = adapter_text.partition("---")[2].partition("---")
            adapter_body = body.lstrip("\n")
        else:
            adapter_body = adapter_text
        prompt_body = prompt.read_text()
        if hashlib.sha256(adapter_body.encode()).hexdigest() != hashlib.sha256(prompt_body.encode()).hexdigest():
            findings.append(Finding(
                "agent_dual_artifact", "ERROR",
                f"Agent '{agent_dir.name}' body drift between canonical prompt.md and Claude adapter",
                {"canonical": str(prompt), "adapter": str(adapter)},
            ))
    if not any(f.severity == "ERROR" for f in findings):
        findings.append(Finding("agent_dual_artifact", "OK", "All canonical agents have matching Claude adapter body"))
    return findings


def check_session_history_index_root() -> list[Finding]:
    """session-history index entries should have a 'root' field with values in
    {'agents','claude'} post Phase 2.3. Pre-phase: index may not exist → INFO."""
    findings: list[Finding] = []
    # Prefer AGENTS_HOME sessions path (canonical post-migration)
    candidates = [
        AGENTS_HOME / "memory/sessions/index.jsonl",
        CLAUDE_HOME / "projects/-Users-feir--claude/memory/sessions/index.jsonl",
    ]
    found = None
    for c in candidates:
        if c.is_file():
            found = c
            break
    if not found:
        findings.append(Finding("session_history_index", "INFO", "No session-history index found (not yet built)"))
        return findings
    root_values = set()
    entries = 0
    with found.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            entries += 1
            root_values.add(e.get("root", "<missing>"))
    unknown = root_values - {"agents", "claude"}
    if "<missing>" in unknown and entries > 0:
        findings.append(Finding(
            "session_history_index", "WARN",
            "Some index entries lack 'root' field (legacy pre-P2.3 entries)",
            {"index": str(found), "entries": entries},
        ))
    elif unknown:
        findings.append(Finding(
            "session_history_index", "ERROR",
            "Index has unexpected root values",
            {"values": sorted(unknown)},
        ))
    else:
        findings.append(Finding(
            "session_history_index", "OK",
            f"Index has consistent root labels ({entries} entries, roots={sorted(root_values)})",
        ))
    return findings


def check_symlink_loops() -> list[Finding]:
    """Report symlinks whose target does not exist, or that loop. Covers skills
    and agents dirs in both homes."""
    findings: list[Finding] = []
    roots = [CLAUDE_HOME/"skills", CLAUDE_HOME/"agents", AGENTS_HOME/"skills", AGENTS_HOME/"agents"]
    bad: list[tuple[Path, str]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_symlink():
                continue
            try:
                p.resolve(strict=True)
            except FileNotFoundError:
                bad.append((p, "dangling"))
            except RuntimeError:
                bad.append((p, "loop"))
            except Exception as e:
                bad.append((p, f"error:{e}"))
    if bad:
        for p, reason in bad:
            findings.append(Finding(
                "symlinks", "ERROR",
                f"Broken symlink ({reason})",
                {"path": str(p), "target": os.readlink(p) if p.is_symlink() else None},
            ))
    else:
        findings.append(Finding("symlinks", "OK", "No dangling or looped symlinks"))
    return findings


def check_rules_adapter_drift() -> list[Finding]:
    """Post Phase 2.6, `~/.agents/rules/*.md` should be canonical and `~/.claude/rules/`
    should either be a symlink or contain only Claude-specific rules. Pre-P2.6, one
    side being empty while the other is populated is expected (INFO)."""
    findings: list[Finding] = []
    agents_rules = AGENTS_HOME / "rules"
    claude_rules = CLAUDE_HOME / "rules"
    a_files = {p.name for p in agents_rules.glob("*.md")} if agents_rules.is_dir() else set()
    c_files = {p.name for p in claude_rules.glob("*.md")} if claude_rules.is_dir() else set()
    if not a_files and c_files:
        findings.append(Finding(
            "rules_adapter", "INFO",
            f"Canonical rules/ empty, {len(c_files)} files in Claude rules/ (pre-P2.6)",
        ))
        return findings
    if a_files and c_files:
        both = a_files & c_files
        # If both sides have same filename, they should hash-match
        for name in sorted(both):
            if (agents_rules/name).is_file() and (claude_rules/name).is_file():
                if _file_sha256(agents_rules/name) != _file_sha256(claude_rules/name):
                    findings.append(Finding(
                        "rules_adapter", "ERROR",
                        f"Rule file diverges between homes: {name}",
                        {"agents": str(agents_rules/name), "claude": str(claude_rules/name)},
                    ))
    if not any(f.severity == "ERROR" for f in findings):
        findings.append(Finding("rules_adapter", "OK", "Rules homes consistent"))
    return findings


CHECKS: dict[str, Callable[[], list[Finding]]] = {
    "backup_in_skills": check_backup_in_skills,
    "hardcoded_claude_paths": check_hardcoded_claude_paths,
    "duplicate_skills": check_duplicate_skills,
    "executable_bits": check_executable_bits,
    "agent_dual_artifact": check_agent_dual_artifact,
    "session_history_index": check_session_history_index_root,
    "symlinks": check_symlink_loops,
    "rules_adapter": check_rules_adapter_drift,
}


def run(selected: list[str] | None, json_out: bool) -> int:
    all_findings: list[Finding] = []
    names = selected or list(CHECKS.keys())
    for name in names:
        if name not in CHECKS:
            print(f"ERROR: unknown check '{name}'", file=sys.stderr)
            return 2
        all_findings.extend(CHECKS[name]())

    if json_out:
        print(json.dumps([asdict(f) for f in all_findings], indent=2))
    else:
        # Grouped text output
        by_check: dict[str, list[Finding]] = {}
        for f in all_findings:
            by_check.setdefault(f.check, []).append(f)
        worst = 0
        for check_name, findings in by_check.items():
            sev = max((SEVERITY_RANK[f.severity] for f in findings), default=0)
            worst = max(worst, sev)
            sev_label = {0:"OK", 1:"INFO", 2:"WARN", 3:"ERROR"}[sev]
            print(f"[{sev_label:5s}] {check_name}")
            for f in findings:
                if f.severity == "OK" and len(findings) == 1:
                    print(f"         {f.message}")
                else:
                    print(f"    [{f.severity}] {f.message}")
                    for k, v in f.details.items():
                        print(f"           {k}: {v}")
        print()
        print(f"Agents: {AGENTS_HOME}")
        print(f"Claude: {CLAUDE_HOME}")

    has_error = any(f.severity == "ERROR" for f in all_findings)
    return 1 if has_error else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="append", help="Run only the named check (repeatable)")
    ap.add_argument("--json", action="store_true", help="Output findings as JSON")
    ap.add_argument("--list", action="store_true", help="List check names and exit")
    args = ap.parse_args()
    if args.list:
        for name in CHECKS:
            print(name)
        return 0
    return run(args.check, args.json)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(f"drift script crashed: {exc}", file=sys.stderr)
        sys.exit(2)
