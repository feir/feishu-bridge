"""Unit tests for tool progress display: _extract_hint_data, _format_tool_hint,
_mcp_display_name, and tool_status_update dedup/normalization logic."""

import pytest

from feishu_bridge.runtime import _extract_hint_data


class TestExtractHintData:
    def test_bash_extracts_first_word(self):
        assert _extract_hint_data("Bash", {"command": "git status"}) == "git"

    def test_bash_path_command(self):
        assert _extract_hint_data("Bash", {"command": "/usr/bin/env python3 -m pytest"}) == "/usr/bin/env"

    def test_bash_empty_command(self):
        assert _extract_hint_data("Bash", {"command": ""}) == ""
        assert _extract_hint_data("Bash", {}) == ""

    def test_read_file_path(self):
        assert _extract_hint_data("Read", {"file_path": "/home/user/project/main.py"}) == "/home/user/project/main.py"

    def test_edit_file_path(self):
        assert _extract_hint_data("Edit", {"file_path": "/tmp/foo.txt"}) == "/tmp/foo.txt"

    def test_write_file_path(self):
        assert _extract_hint_data("Write", {"file_path": "/a/b.md"}) == "/a/b.md"

    def test_agent_description_truncated(self):
        desc = "A" * 60
        assert _extract_hint_data("Agent", {"description": desc}) == "A" * 40

    def test_agent_short_description(self):
        assert _extract_hint_data("Agent", {"description": "Review code"}) == "Review code"

    def test_skill_name(self):
        assert _extract_hint_data("Skill", {"skill": "done"}) == "done"

    def test_grep_pattern_truncated(self):
        pattern = "x" * 50
        assert _extract_hint_data("Grep", {"pattern": pattern}) == "x" * 30

    def test_unknown_tool_returns_empty(self):
        assert _extract_hint_data("Glob", {"pattern": "*.py"}) == ""
        assert _extract_hint_data("WebFetch", {"url": "https://example.com"}) == ""

    def test_empty_input(self):
        assert _extract_hint_data("Read", {}) == ""
        assert _extract_hint_data("Agent", {}) == ""


class TestFormatToolHint:
    """Tests for ResponseHandle._format_tool_hint (static method)."""

    @pytest.fixture()
    def fmt(self):
        from feishu_bridge.ui import ResponseHandle
        return ResponseHandle._format_tool_hint

    def test_bash_basename(self, fmt):
        assert fmt("Bash", "/usr/bin/git") == "git"
        assert fmt("Bash", "git") == "git"

    def test_read_basename(self, fmt):
        assert fmt("Read", "/home/user/project/main.py") == "main.py"

    def test_edit_basename(self, fmt):
        assert fmt("Edit", "/tmp/foo.txt") == "foo.txt"

    def test_empty_returns_empty(self, fmt):
        assert fmt("Read", "") == ""
        assert fmt("Bash", "") == ""

    def test_agent_passthrough(self, fmt):
        assert fmt("Agent", "Review code") == "Review code"

    def test_unknown_tool_passthrough(self, fmt):
        assert fmt("Grep", "pattern") == "pattern"


class TestMcpDisplayName:
    """Tests for ResponseHandle._mcp_display_name (static method)."""

    @pytest.fixture()
    def mcp(self):
        from feishu_bridge.ui import ResponseHandle
        return ResponseHandle._mcp_display_name

    def test_standard_mcp(self, mcp):
        assert mcp("mcp__hindsight__retain") == "Hindsight: retain"

    def test_mcp_with_underscores(self, mcp):
        assert mcp("mcp__claude_ai_Google_Drive__authenticate") == "Claude Ai Google Drive: authenticate"

    def test_short_mcp_name(self, mcp):
        assert mcp("mcp__foo") == "mcp__foo"

    def test_non_mcp_passthrough(self, mcp):
        assert mcp("Bash") == "Bash"


class TestToolStatusUpdateDedup:
    """Test tool_status_update dedup and normalization via _tool_history."""

    @pytest.fixture()
    def handle(self):
        from unittest.mock import MagicMock
        from feishu_bridge.ui import ResponseHandle
        h = ResponseHandle.__new__(ResponseHandle)
        from collections import deque
        h._tool_history = deque(maxlen=8)
        h._terminated = False
        h._summary_updated = False
        h.card_message_id = "msg-1"
        h._cardkit_card_id = "card-1"
        h._update_summary = MagicMock()
        h._render_progress = MagicMock()
        h._ensure_card = MagicMock(return_value=True)
        h._loading_icon_cleared = True
        return h

    def test_dict_input_accumulates(self, handle):
        handle.tool_status_update([
            {"name": "Read", "hint_data": "/a/foo.py"},
            {"name": "Edit", "hint_data": "/a/bar.py"},
        ])
        assert len(handle._tool_history) == 2
        assert handle._tool_history[0]["label"] == "读取文件"
        assert handle._tool_history[0]["hint"] == "foo.py"
        assert handle._tool_history[1]["label"] == "编辑文件"

    def test_consecutive_same_tool_dedup(self, handle):
        handle.tool_status_update([
            {"name": "Read", "hint_data": "/a/foo.py"},
            {"name": "Read", "hint_data": "/a/foo.py"},
            {"name": "Read", "hint_data": "/a/foo.py"},
        ])
        assert len(handle._tool_history) == 1
        assert handle._tool_history[0]["count"] == 3

    def test_different_hints_no_dedup(self, handle):
        handle.tool_status_update([
            {"name": "Read", "hint_data": "/a/foo.py"},
            {"name": "Read", "hint_data": "/a/bar.py"},
        ])
        assert len(handle._tool_history) == 2

    def test_pi_runner_str_compat(self, handle):
        handle.tool_status_update(["ls", "read", "read"])
        assert len(handle._tool_history) == 2
        assert handle._tool_history[0]["label"] == "ls"
        assert handle._tool_history[1]["label"] == "read"
        assert handle._tool_history[1]["count"] == 2

    def test_agent_excluded(self, handle):
        handle.tool_status_update([
            {"name": "Agent", "hint_data": "Review code"},
            {"name": "TodoWrite", "hint_data": ""},
        ])
        assert len(handle._tool_history) == 0

    def test_mcp_tool_display(self, handle):
        handle.tool_status_update([
            {"name": "mcp__hindsight__retain", "hint_data": ""},
        ])
        assert len(handle._tool_history) == 1
        assert handle._tool_history[0]["label"] == "Hindsight: retain"

    def test_deque_maxlen_respected(self, handle):
        tools = [{"name": f"tool_{i}", "hint_data": ""} for i in range(12)]
        handle.tool_status_update(tools)
        assert len(handle._tool_history) == 8

    def test_summary_updated_from_latest(self, handle):
        handle.tool_status_update([
            {"name": "Read", "hint_data": "/a/foo.py"},
            {"name": "Bash", "hint_data": "git status"},
        ])
        handle._update_summary.assert_called_with("执行命令...")

    def test_empty_list_no_crash(self, handle):
        handle.tool_status_update([])
        assert len(handle._tool_history) == 0

    def test_terminated_skips(self, handle):
        handle._terminated = True
        handle.tool_status_update([{"name": "Read", "hint_data": "/a.py"}])
        assert len(handle._tool_history) == 0

    def test_summary_updated_skips(self, handle):
        handle._summary_updated = True
        handle.tool_status_update([{"name": "Read", "hint_data": "/a.py"}])
        assert len(handle._tool_history) == 0
