"""Unit tests for Stage 2 worker helpers + command session-clear semantics.

Covers Codex R3-M5 gap: prior tests verified only the pure
``state_thread_projects`` / ``project_detector`` modules; the worker-level
suffix / log / session-clear behavior had no direct coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from feishu_bridge.project_detector import ProjectMatch
from feishu_bridge.state_thread_projects import ThreadProjects
from feishu_bridge.worker import (
    _build_project_suffix,
    _footer_project_label,
    _log_heuristic_event,
)


# ── _footer_project_label ──────────────────────────────────────────────────


class TestFooterProjectLabel:
    def test_bound_uses_project_id(self):
        bound = {"project_id": "feishu-bridge",
                 "workspace": "/Users/x/projects/feishu-bridge"}
        # project_id wins even if it differs from the workspace basename
        assert _footer_project_label(bound, bound["workspace"]) == "feishu-bridge"

    def test_unbound_uses_workspace_basename(self):
        assert _footer_project_label(None, "/Users/x/.claude") == ".claude"

    def test_unbound_strips_trailing_slash(self):
        assert _footer_project_label(None, "/Users/x/projects/foo/") == "foo"

    def test_unbound_empty_workspace_yields_empty(self):
        # Empty/blank workspace → "" so the footer builder omits the segment
        assert _footer_project_label(None, "") == ""


# ── _build_project_suffix ──────────────────────────────────────────────────


class TestBuildProjectSuffix:
    def test_high_confidence_match(self):
        m = ProjectMatch(project_id="feishu-bridge", confidence="high",
                         matched_via="id_token")
        suf = _build_project_suffix(m, "feishu-bridge 怎么发版")
        assert suf is not None
        assert "feishu-bridge" in suf
        assert "确认绑定" in suf
        assert "`/project feishu-bridge`" in suf

    def test_medium_confidence_match(self):
        m = ProjectMatch(project_id="news", confidence="medium", matched_via="name")
        suf = _build_project_suffix(m, "News Briefing 状态")
        assert suf is not None
        assert "未绑定项目" in suf
        assert "/project clear" in suf or "clear" in suf
        # Does NOT name the specific project (only generic prompt at medium)
        assert "news" not in suf

    def test_low_confidence_match(self):
        m = ProjectMatch(project_id="news", confidence="low", matched_via="fuzzy_prefix")
        suf = _build_project_suffix(m, "newsletter 怎么订阅")
        assert suf is not None
        assert "未绑定项目" in suf

    def test_none_with_history_trigger(self):
        # message contains "上次" → trigger fires even though detector returned None
        suf = _build_project_suffix(None, "上次部署在哪台机器？")
        assert suf is not None
        assert "未绑定项目" in suf
        assert "建议" in suf

    def test_none_with_deploy_trigger(self):
        suf = _build_project_suffix(None, "部署到哪了")
        assert suf is not None

    def test_none_chitchat_no_suffix(self):
        # No match + no trigger word → no suffix (R3 narrow rule)
        assert _build_project_suffix(None, "今天天气真好") is None
        assert _build_project_suffix(None, "hello world") is None
        assert _build_project_suffix(None, "") is None


# ── _log_heuristic_event ───────────────────────────────────────────────────


class TestLogHeuristicEvent:
    def test_writes_jsonl_record(self, tmp_path):
        ws = tmp_path
        bot_id = "bot-test"
        m = ProjectMatch(project_id="feishu-bridge", confidence="high",
                         matched_via="id_token")
        _log_heuristic_event(str(ws), bot_id, "bot-test:chat-1:thread-A",
                             "feishu-bridge 怎么发版", m, history_triggered=False)
        log_path = ws / "state" / "feishu-bridge" / f"heuristic-log-{bot_id}.jsonl"
        assert log_path.exists()
        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["tag"] == "bot-test:chat-1:thread-A"
        assert rec["match"] == {
            "id": "feishu-bridge",
            "confidence": "high",
            "via": "id_token",
        }
        assert rec["history_triggered"] is False
        # [R3 code-review M2] Spec task 4.5 requires user_action key
        assert "user_action" in rec
        assert rec["user_action"] is None
        assert "ts" in rec
        assert "message_hash" in rec
        # message_hash must NOT contain the raw message text
        assert "feishu-bridge" not in rec["message_hash"]

    def test_match_none_recorded_as_null(self, tmp_path):
        _log_heuristic_event(str(tmp_path), "bot", "tag", "hi",
                             None, history_triggered=True)
        log_path = tmp_path / "state" / "feishu-bridge" / "heuristic-log-bot.jsonl"
        rec = json.loads(log_path.read_text().strip())
        assert rec["match"] is None
        assert rec["history_triggered"] is True
        assert "user_action" in rec
        assert rec["user_action"] is None

    def test_append_mode(self, tmp_path):
        # Two events written → two lines
        _log_heuristic_event(str(tmp_path), "bot", "tag1", "msg1", None, False)
        _log_heuristic_event(str(tmp_path), "bot", "tag2", "msg2", None, True)
        log_path = tmp_path / "state" / "feishu-bridge" / "heuristic-log-bot.jsonl"
        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2

    def test_oserror_silenced(self, tmp_path, monkeypatch):
        # If the parent directory creation fails, logging must not raise
        def boom(*args, **kwargs):
            raise OSError("no space left")
        monkeypatch.setattr("feishu_bridge.worker.Path.mkdir", boom)
        # Should swallow silently
        _log_heuristic_event(str(tmp_path), "bot", "tag", "msg", None, False)


# ── /project session_map.delete behavior (R3-H3) ───────────────────────────


@pytest.fixture
def real_workspace(tmp_path):
    ws = tmp_path / "fake-target"
    ws.mkdir()
    return ws


def _build_bot_mock(tmp_path, real_workspace):
    """Construct just enough of a FeishuBot to exercise _handle_project.

    The workspace dir is named ``.claude`` so ``resolve_dotclaude_root`` picks
    the dotclaude-root layout (matching production where ``bot.workspace =
    ~/.claude``).
    """
    bot = MagicMock()
    ws = tmp_path / ".claude"
    ws.mkdir(exist_ok=True)
    (ws / "memory").mkdir(exist_ok=True)
    bot.workspace = str(ws)
    bot.bot_id = "bot-test"
    bot.session_map = MagicMock()
    bot.thread_projects = ThreadProjects(
        tmp_path / "thread-projects-test.json",
    )
    # Mock runner — not a ClaudeRunner so the Claude-mode suffix doesn't apply
    bot.runner = MagicMock()
    # Avoid isinstance checks succeeding for ClaudeRunner
    bot.runner.__class__.__name__ = "OmpRpcRunner"
    return bot


def _handler_for(bot):
    from feishu_bridge.commands import BridgeCommandHandler
    return BridgeCommandHandler(bot)


def _capture_delivers(handle_mock):
    return [c.args[0] for c in handle_mock.deliver.call_args_list]


class TestProjectCommandSessionClear:
    def test_set_via_path_deletes_session(self, tmp_path, real_workspace):
        bot = _build_bot_mock(tmp_path, real_workspace)
        handler = _handler_for(bot)
        handle = MagicMock()
        item = {
            "bot_id": "bot-test", "chat_id": "chat-1", "thread_id": "thread-A",
        }
        handler._handle_project(item, str(real_workspace), handle)
        bot.session_map.delete.assert_called_once_with(
            ("bot-test", "chat-1", "thread-A")
        )
        # ThreadProjects must have the binding persisted
        assert bot.thread_projects.get("bot-test:chat-1:thread-A") is not None
        # Reply mentions reset
        replies = _capture_delivers(handle)
        assert any("已绑定" in r for r in replies)
        assert any("已重置会话" in r for r in replies)

    def test_clear_when_bound_deletes_session(self, tmp_path, real_workspace):
        bot = _build_bot_mock(tmp_path, real_workspace)
        # Pre-bind so clear has something to remove
        bot.thread_projects.set(
            "bot-test:chat-1:thread-A",
            project_id="x",
            workspace=str(real_workspace),
        )
        handler = _handler_for(bot)
        handle = MagicMock()
        item = {
            "bot_id": "bot-test", "chat_id": "chat-1", "thread_id": "thread-A",
        }
        handler._handle_project(item, "clear", handle)
        bot.session_map.delete.assert_called_once_with(
            ("bot-test", "chat-1", "thread-A")
        )
        assert bot.thread_projects.get("bot-test:chat-1:thread-A") is None

    def test_clear_when_unbound_does_not_touch_session(self, tmp_path):
        bot = _build_bot_mock(tmp_path, tmp_path)  # workspace dir unused here
        handler = _handler_for(bot)
        handle = MagicMock()
        item = {
            "bot_id": "bot-test", "chat_id": "chat-1", "thread_id": "thread-A",
        }
        handler._handle_project(item, "clear", handle)
        bot.session_map.delete.assert_not_called()
        replies = _capture_delivers(handle)
        assert any("本就未绑定" in r for r in replies)

    def test_show_when_unbound_does_not_touch_session(self, tmp_path):
        bot = _build_bot_mock(tmp_path, tmp_path)
        handler = _handler_for(bot)
        handle = MagicMock()
        item = {
            "bot_id": "bot-test", "chat_id": "chat-1", "thread_id": "thread-A",
        }
        handler._handle_project(item, "", handle)
        bot.session_map.delete.assert_not_called()

    def test_bind_validation_failure_does_not_touch_session(self, tmp_path):
        bot = _build_bot_mock(tmp_path, tmp_path)
        handler = _handler_for(bot)
        handle = MagicMock()
        item = {
            "bot_id": "bot-test", "chat_id": "chat-1", "thread_id": "thread-A",
        }
        # Non-existent path → ValueError from ThreadProjects.set
        handler._handle_project(item, "/no/such/path/xyzzy", handle)
        bot.session_map.delete.assert_not_called()
        replies = _capture_delivers(handle)
        assert any("❌" in r for r in replies)


class TestProjectCommandIdResolution:
    """Codex R3 code-review HIGH: bare-id `/project X` MUST NOT fall back to
    path binding when the registry is missing/empty/malformed."""

    def _bot_with_registry(self, tmp_path, real_workspace, content):
        bot = _build_bot_mock(tmp_path, real_workspace)
        # _build_bot_mock already created `<tmp_path>/.claude/memory/`.
        registry_path = Path(bot.workspace) / "memory" / "projects.md"
        if content is not None:
            registry_path.write_text(content, encoding="utf-8")
        return bot

    def test_missing_registry_rejects_id(self, tmp_path, real_workspace):
        bot = self._bot_with_registry(tmp_path, real_workspace, None)
        handle = MagicMock()
        item = {"bot_id": "bot-test", "chat_id": "c", "thread_id": "t"}
        _handler_for(bot)._handle_project(item, "feishu-bridge", handle)
        bot.session_map.delete.assert_not_called()
        replies = _capture_delivers(handle)
        assert any("找不到项目注册表" in r for r in replies)

    def test_empty_registry_rejects_id(self, tmp_path, real_workspace):
        bot = self._bot_with_registry(
            tmp_path, real_workspace, "no tables here\nplain text only"
        )
        handle = MagicMock()
        item = {"bot_id": "bot-test", "chat_id": "c", "thread_id": "t"}
        _handler_for(bot)._handle_project(item, "feishu-bridge", handle)
        bot.session_map.delete.assert_not_called()
        replies = _capture_delivers(handle)
        assert any("注册表为空" in r for r in replies)

    def test_unknown_id_rejects(self, tmp_path, real_workspace):
        registry = (
            "## Projects\n\n"
            "| ID | 名称 | 路径 | 状态 |\n"
            "|---|---|---|---|\n"
            f"| feishu-bridge | FB | `{real_workspace}` | active |\n"
        )
        bot = self._bot_with_registry(tmp_path, real_workspace, registry)
        handle = MagicMock()
        item = {"bot_id": "bot-test", "chat_id": "c", "thread_id": "t"}
        _handler_for(bot)._handle_project(item, "unknown-id", handle)
        bot.session_map.delete.assert_not_called()
        replies = _capture_delivers(handle)
        assert any("未在 `memory/projects.md` 中找到项目" in r for r in replies)
        assert any("feishu-bridge" in r for r in replies)

    def test_known_id_binds_and_clears_session(self, tmp_path, real_workspace):
        registry = (
            "| ID | 名称 | 路径 | 状态 |\n"
            "|---|---|---|---|\n"
            f"| fb | Feishu | `{real_workspace}` | active |\n"
        )
        bot = self._bot_with_registry(tmp_path, real_workspace, registry)
        handle = MagicMock()
        item = {"bot_id": "bot-test", "chat_id": "c", "thread_id": "t"}
        _handler_for(bot)._handle_project(item, "fb", handle)
        bot.session_map.delete.assert_called_once_with(("bot-test", "c", "t"))
        entry = bot.thread_projects.get("bot-test:c:t")
        assert entry is not None
        assert entry["project_id"] == "fb"
        assert entry["workspace"] == str(real_workspace)
