"""Unit tests for ``state_thread_projects`` (Stage 2 memory-system-fix)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from feishu_bridge.state_thread_projects import (
    ProjectEntry,
    ThreadProjects,
    normalize_path,
    parse_projects_registry,
)


# ── normalize_path ──────────────────────────────────────────────────────────


class TestNormalizePath:
    def test_none(self):
        assert normalize_path(None) is None

    def test_empty(self):
        assert normalize_path("") is None
        assert normalize_path("   ") is None
        assert normalize_path("``") is None
        assert normalize_path("` `") is None  # backticks + whitespace only

    def test_strip_whitespace(self, tmp_path):
        assert normalize_path(f"  {tmp_path}  ") == str(tmp_path)

    def test_strip_backticks(self, tmp_path):
        assert normalize_path(f"`{tmp_path}`") == str(tmp_path)
        assert normalize_path(f"``{tmp_path}``") == str(tmp_path)

    def test_expanduser(self):
        out = normalize_path("~/some/dir")
        assert out is not None
        assert out.startswith(os.path.expanduser("~"))
        assert "~" not in out

    def test_abspath_relative(self):
        out = normalize_path("./relative")
        assert out is not None
        assert out == os.path.abspath("./relative")

    def test_does_not_enforce_existence(self):
        """normalize_path itself never checks isdir — caller's job."""
        out = normalize_path("/definitely/not/a/real/path/xyzzy")
        assert out == "/definitely/not/a/real/path/xyzzy"


# ── parse_projects_registry ─────────────────────────────────────────────────


PROJECTS_MD_SAMPLE = """\
# Project Registry

> Reference, not runtime SoT.

## Projects

| ID | 名称 | 路径 | 状态 |
|---|---|---|---|
| confluence | Confluence Strategy | `~/projects/confluence-strategy` | active |
| feishu-bridge | Feishu Claude Bridge | `~/projects/feishu-bridge` | active |
| dotclaude | Claude Code Config | `~/.claude` | active |
| investment-dashboard | Investment Dashboard | `~/projects/investment-dashboard` | active | deploy: extra cell |
"""


class TestParseProjectsRegistry:
    def test_parses_rows(self):
        entries = parse_projects_registry(PROJECTS_MD_SAMPLE)
        ids = [e.id for e in entries]
        assert ids == ["confluence", "feishu-bridge", "dotclaude", "investment-dashboard"]

    def test_returns_raw_path(self):
        entries = parse_projects_registry(PROJECTS_MD_SAMPLE)
        e = next(x for x in entries if x.id == "feishu-bridge")
        assert e.path == "`~/projects/feishu-bridge`"
        assert e.name == "Feishu Claude Bridge"

    def test_skips_header_and_separator(self):
        entries = parse_projects_registry(PROJECTS_MD_SAMPLE)
        assert "ID" not in [e.id for e in entries]
        # separator row "|---|---|---|---|" has empty/dash-only ident — filtered
        for e in entries:
            assert set(e.id) - set("-: ") != set()

    def test_tolerates_extra_columns(self):
        entries = parse_projects_registry(PROJECTS_MD_SAMPLE)
        ids = [e.id for e in entries]
        assert "investment-dashboard" in ids

    def test_empty_input(self):
        assert parse_projects_registry("") == []
        assert parse_projects_registry(None) == []

    def test_no_table(self):
        assert parse_projects_registry("plain text without a table") == []


# ── ThreadProjects ──────────────────────────────────────────────────────────


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "thread-projects-test-bot.json"


@pytest.fixture
def real_workspace(tmp_path):
    ws = tmp_path / "fake-project"
    ws.mkdir()
    return ws


class TestThreadProjectsCRUD:
    def test_empty_store_get_returns_none(self, store_path):
        tp = ThreadProjects(store_path)
        assert tp.get("test-bot:chat:thread") is None

    def test_set_and_get_roundtrip(self, store_path, real_workspace):
        tp = ThreadProjects(store_path)
        entry = tp.set(
            "test-bot:chat-1:thread-A",
            project_id="fake",
            workspace=str(real_workspace),
        )
        assert entry["project_id"] == "fake"
        assert entry["workspace"] == str(real_workspace)
        assert entry["source"] == "explicit"
        assert entry["bound_at"]  # iso ts present

        fetched = tp.get("test-bot:chat-1:thread-A")
        assert fetched == entry

    def test_set_returns_defensive_copy(self, store_path, real_workspace):
        tp = ThreadProjects(store_path)
        entry = tp.set("t1", project_id="p", workspace=str(real_workspace))
        entry["project_id"] = "MUTATED"
        # internal state must not be affected
        assert tp.get("t1")["project_id"] == "p"

    def test_get_returns_defensive_copy(self, store_path, real_workspace):
        tp = ThreadProjects(store_path)
        tp.set("t1", project_id="p", workspace=str(real_workspace))
        view = tp.get("t1")
        view["project_id"] = "MUTATED"
        assert tp.get("t1")["project_id"] == "p"

    def test_clear_existing(self, store_path, real_workspace):
        tp = ThreadProjects(store_path)
        tp.set("t1", project_id="p", workspace=str(real_workspace))
        assert tp.clear("t1") is True
        assert tp.get("t1") is None
        assert tp.clear("t1") is False  # second clear is no-op

    def test_clear_missing(self, store_path):
        tp = ThreadProjects(store_path)
        assert tp.clear("never-bound") is False

    def test_persistence_survives_reload(self, store_path, real_workspace):
        tp1 = ThreadProjects(store_path)
        tp1.set("t1", project_id="p1", workspace=str(real_workspace))

        tp2 = ThreadProjects(store_path)
        entry = tp2.get("t1")
        assert entry is not None
        assert entry["project_id"] == "p1"
        assert entry["workspace"] == str(real_workspace)

    def test_normalizes_tilde_workspace(self, store_path, tmp_path, monkeypatch):
        # Point HOME at tmp_path so ~/relative-target resolves to a real dir
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "relative-target").mkdir()
        tp = ThreadProjects(store_path)
        entry = tp.set("t1", project_id="p", workspace="~/relative-target")
        assert entry["workspace"] == str(tmp_path / "relative-target")
        assert "~" not in entry["workspace"]

    def test_normalizes_backticks(self, store_path, real_workspace):
        tp = ThreadProjects(store_path)
        entry = tp.set("t1", project_id="p", workspace=f"`{real_workspace}`")
        assert entry["workspace"] == str(real_workspace)

    def test_rejects_non_directory(self, store_path, tmp_path):
        # file, not a dir
        f = tmp_path / "regular-file.txt"
        f.write_text("hello")
        tp = ThreadProjects(store_path)
        with pytest.raises(ValueError, match="not a directory"):
            tp.set("t1", project_id="p", workspace=str(f))

    def test_rejects_missing_path(self, store_path):
        tp = ThreadProjects(store_path)
        with pytest.raises(ValueError, match="not exist or is not a directory"):
            tp.set("t1", project_id="p", workspace="/no/such/path/xyzzy")

    def test_rejects_empty_workspace(self, store_path):
        tp = ThreadProjects(store_path)
        with pytest.raises(ValueError, match="empty or invalid"):
            tp.set("t1", project_id="p", workspace="")
        with pytest.raises(ValueError, match="empty or invalid"):
            tp.set("t1", project_id="p", workspace="   ")

    def test_rejects_empty_tag(self, store_path, real_workspace):
        tp = ThreadProjects(store_path)
        with pytest.raises(ValueError, match="tag must be a non-empty string"):
            tp.set("", project_id="p", workspace=str(real_workspace))

    def test_rejects_empty_project_id(self, store_path, real_workspace):
        tp = ThreadProjects(store_path)
        with pytest.raises(ValueError, match="project_id must be a non-empty string"):
            tp.set("t1", project_id="", workspace=str(real_workspace))

    def test_rejects_invalid_source(self, store_path, real_workspace):
        tp = ThreadProjects(store_path)
        with pytest.raises(ValueError, match="source must be one of"):
            tp.set(
                "t1",
                project_id="p",
                workspace=str(real_workspace),
                source="heuristic",  # R2: D5 — heuristic does NOT write
            )

    def test_failed_save_rolls_back_inmemory(
        self, store_path, real_workspace, monkeypatch
    ):
        tp = ThreadProjects(store_path)
        # First write succeeds
        tp.set("t1", project_id="p1", workspace=str(real_workspace))

        # Sabotage subsequent writes
        def boom(self):
            raise OSError("disk full")

        monkeypatch.setattr(ThreadProjects, "_save_locked", boom)

        # Second set must roll back the in-memory state
        with pytest.raises(OSError):
            tp.set("t1", project_id="p2", workspace=str(real_workspace))
        assert tp.get("t1")["project_id"] == "p1"

        # Setting a brand-new tag must remove it from memory on failure
        with pytest.raises(OSError):
            tp.set("t2", project_id="p", workspace=str(real_workspace))
        assert tp.get("t2") is None


class TestThreadProjectsCorruption:
    def test_corrupt_json_is_quarantined(self, store_path):
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text("{not valid json")
        tp = ThreadProjects(store_path)
        assert tp.get("anything") is None
        # corrupt sidecar should be present
        backups = list(store_path.parent.glob(store_path.stem + ".corrupt-*.json"))
        assert len(backups) == 1, [p.name for p in store_path.parent.iterdir()]

    def test_non_object_root_is_quarantined(self, store_path):
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text("[1, 2, 3]")
        tp = ThreadProjects(store_path)
        assert tp.all() == {}
        backups = list(store_path.parent.glob(store_path.stem + ".corrupt-*.json"))
        assert len(backups) == 1

    def test_garbage_rows_dropped_but_valid_kept(
        self, store_path, real_workspace
    ):
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text(
            json.dumps(
                {
                    "good:1": {
                        "project_id": "p1",
                        "workspace":  str(real_workspace),
                        "bound_at":   "2026-05-28T16:00:00+00:00",
                        "source":     "explicit",
                    },
                    "bad-missing-workspace": {"project_id": "p2"},
                    "bad-non-dict": "stringvalue",
                    # numeric key would round-trip to str via JSON, so not testable here
                }
            )
        )
        tp = ThreadProjects(store_path)
        assert tp.get("good:1") is not None
        assert tp.get("bad-missing-workspace") is None


class TestThreadProjectsConcurrency:
    def test_parallel_set_distinct_tags(self, store_path, tmp_path):
        # Make 10 distinct workspace dirs
        ws = [tmp_path / f"ws-{i}" for i in range(10)]
        for w in ws:
            w.mkdir()
        tp = ThreadProjects(store_path)

        errors: list[BaseException] = []

        def worker(i: int):
            try:
                tp.set(f"tag-{i}", project_id=f"p{i}", workspace=str(ws[i]))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        for i in range(10):
            entry = tp.get(f"tag-{i}")
            assert entry is not None
            assert entry["workspace"] == str(ws[i])

        # Reload from disk and verify all 10 still present (no torn write)
        tp2 = ThreadProjects(store_path)
        for i in range(10):
            assert tp2.get(f"tag-{i}") is not None

    def test_parallel_set_same_tag_last_wins(self, store_path, tmp_path):
        ws_a = tmp_path / "a"; ws_a.mkdir()
        ws_b = tmp_path / "b"; ws_b.mkdir()
        tp = ThreadProjects(store_path)
        errors: list[BaseException] = []

        def worker(label: str, ws: Path):
            try:
                for _ in range(20):
                    tp.set("shared", project_id=label, workspace=str(ws))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=worker, args=("a", ws_a))
        t2 = threading.Thread(target=worker, args=("b", ws_b))
        t1.start(); t2.start(); t1.join(); t2.join()

        assert errors == []
        # Final entry must be one of the two — never a hybrid / corrupted record.
        final = tp.get("shared")
        assert final is not None
        assert final["project_id"] in ("a", "b")
        assert final["workspace"] in (str(ws_a), str(ws_b))


class TestThreadProjectsAtomicWrite:
    def test_no_tmp_left_behind_on_success(self, store_path, real_workspace):
        tp = ThreadProjects(store_path)
        tp.set("t1", project_id="p", workspace=str(real_workspace))
        # tmp suffix must not linger
        leftovers = list(store_path.parent.glob(store_path.stem + ".tmp"))
        assert leftovers == []

    def test_file_perms_are_0600(self, store_path, real_workspace):
        tp = ThreadProjects(store_path)
        tp.set("t1", project_id="p", workspace=str(real_workspace))
        mode = store_path.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
