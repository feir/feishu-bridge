"""Unit tests for tool progress display: _extract_hint_data, _format_tool_hint,
_mcp_display_name, and tool_status_update dedup/normalization logic."""

import json
import logging
import threading
from collections import deque
from unittest.mock import MagicMock, Mock

import pytest

from feishu_bridge.runtime import _extract_hint_data
from feishu_bridge.ui import ResponseHandle


class TestExtractHintData:
    # ── Bash ──
    def test_bash_returns_basename_only(self):
        """Only executable basename is shown — never full args (may contain secrets)."""
        assert _extract_hint_data("Bash", {"command": "git status"}) == "git"

    def test_bash_path_command_returns_basename(self):
        assert _extract_hint_data("Bash", {"command": "/usr/bin/env python3 -m pytest"}) == "env"

    def test_bash_prefers_description(self):
        assert _extract_hint_data("Bash", {
            "command": "pip install foo",
            "description": "Installing deps",
        }) == "Installing deps"

    def test_bash_prefers_intent_over_command(self):
        """OMP _i is preferred over raw command basename."""
        assert _extract_hint_data("Bash", {
            "command": "curl -H 'Authorization: Bearer sk-...'",
            "_i": "Fetching API data",
        }) == "Fetching API data"

    def test_bash_empty_command_returns_empty(self):
        assert _extract_hint_data("Bash", {"command": ""}) == ""
        assert _extract_hint_data("Bash", {}) == ""

    def test_bash_empty_command_falls_back_to_intent(self):
        assert _extract_hint_data("Bash", {"_i": "Running tests"}) == "Running tests"

    # ── Read / Write / Edit ──
    def test_read_file_path_alma(self):
        """Alma (Claude Code) uses file_path."""
        assert _extract_hint_data("Read", {"file_path": "/home/user/main.py"}) == "/home/user/main.py"

    def test_read_path_omp(self):
        """OMP uses path."""
        assert _extract_hint_data("Read", {"path": "/home/user/main.py"}) == "/home/user/main.py"

    def test_read_prefers_file_path(self):
        """file_path takes precedence over path (Alma compat)."""
        assert _extract_hint_data("Read", {
            "file_path": "/alma.py",
            "path": "/omp.py",
        }) == "/alma.py"

    def test_edit_path_omp(self):
        assert _extract_hint_data("Edit", {"path": "/tmp/foo.txt"}) == "/tmp/foo.txt"

    def test_write_path_omp(self):
        assert _extract_hint_data("Write", {"path": "/a/b.md"}) == "/a/b.md"

    def test_read_empty(self):
        assert _extract_hint_data("Read", {}) == ""

    # ── Agent / Task ──
    def test_agent_description_truncated(self):
        desc = "A" * 60
        assert _extract_hint_data("Agent", {"description": desc}) == "A" * 40

    def test_task_description(self):
        assert _extract_hint_data("Task", {"description": "Review code"}) == "Review code"

    # ── Grep / Search ──
    def test_grep_pattern_truncated(self):
        pattern = "x" * 50
        assert _extract_hint_data("Grep", {"pattern": pattern}) == "x" * 30

    def test_search_alias(self):
        assert _extract_hint_data("Search", {"pattern": "TODO"}) == "TODO"

    # ── Skill ──
    def test_skill_name(self):
        assert _extract_hint_data("Skill", {"skill": "done"}) == "done"

    # ── WebSearch / WebFetch ──
    def test_websearch_query(self):
        assert _extract_hint_data("WebSearch", {"query": "python async"}) == "python async"

    def test_webfetch_url(self):
        assert _extract_hint_data("WebFetch", {"url": "https://example.com"}) == "https://example.com"

    # ── Find ──
    def test_find_single_path(self):
        assert _extract_hint_data("Find", {"paths": ["src/**/*.ts"]}) == "src/**/*.ts"

    def test_find_multiple_paths(self):
        result = _extract_hint_data("Find", {"paths": ["src/**/*.ts", "tests/**/*.ts"]})
        assert result == "src/**/*.ts +1"

    def test_find_empty_falls_back_to_intent(self):
        assert _extract_hint_data("Find", {"_i": "Finding test files"}) == "Finding test files"

    # ── Lsp ──
    def test_lsp_action_and_file(self):
        result = _extract_hint_data("Lsp", {"action": "definition", "file": "main.py"})
        assert result == "definition main.py"

    def test_lsp_action_only(self):
        assert _extract_hint_data("Lsp", {"action": "diagnostics"}) == "diagnostics"

    def test_lsp_falls_back_to_intent(self):
        assert _extract_hint_data("Lsp", {"_i": "Finding definitions"}) == "Finding definitions"

    # ── Browser ──
    def test_browser_action_and_url(self):
        result = _extract_hint_data("Browser", {"action": "open", "url": "https://example.com"})
        assert result == "open https://example.com"

    def test_browser_action_only(self):
        assert _extract_hint_data("Browser", {"action": "close"}) == "close"

    # ── Eval ──
    def test_eval_cell_title(self):
        result = _extract_hint_data("Eval", {"cells": [{"title": "imports", "language": "py"}]})
        assert result == "imports"

    def test_eval_cell_language_fallback(self):
        result = _extract_hint_data("Eval", {"cells": [{"language": "py"}]})
        assert result == "py"

    # ── AstGrep / AstEdit ──
    def test_ast_grep_pattern(self):
        assert _extract_hint_data("AstGrep", {"pat": "console.log($$$)"}) == "console.log($$$)"

    def test_ast_edit_ops_pattern(self):
        result = _extract_hint_data("AstEdit", {"ops": [{"pat": "old($A)", "out": "new($A)"}]})
        assert result == "old($A)"

    # ── Debug ──
    def test_debug_action_and_program(self):
        result = _extract_hint_data("Debug", {"action": "launch", "program": "./app"})
        assert result == "launch ./app"

    # ── Universal _i fallback ──
    def test_unknown_tool_uses_intent(self):
        assert _extract_hint_data("SomeNewTool", {"_i": "Doing work"}) == "Doing work"

    def test_unknown_tool_no_intent(self):
        assert _extract_hint_data("SomeNewTool", {}) == ""

    def test_intent_truncated(self):
        intent = "A" * 80
        assert _extract_hint_data("SomeNewTool", {"_i": intent}) == "A" * 50


class TestFormatToolHint:
    """Tests for ResponseHandle._format_tool_hint (static method)."""

    @pytest.fixture()
    def fmt(self):
        from feishu_bridge.ui import ResponseHandle
        return ResponseHandle._format_tool_hint

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

    def test_webfetch_host(self, fmt):
        assert fmt("WebFetch", "https://docs.python.org/3/library/os.html") == "docs.python.org/3/library/os.html"

    def test_webfetch_host_only(self, fmt):
        assert fmt("WebFetch", "https://example.com/") == "example.com"

    def test_websearch_truncates(self, fmt):
        assert fmt("WebSearch", "short query") == "short query"
        long_query = "a" * 60
        assert fmt("WebSearch", long_query) == "a" * 40

    def test_get_subagent_result_truncates(self, fmt):
        short = "subagent-abc"
        assert fmt("GetSubagentResult", short) == "subagent-abc"
        long = "subagent-01234567-89ab-cdef-0123-456789abcdef"
        result = fmt("GetSubagentResult", long)
        assert result == "subagent-0123456…"
        assert len(result) == 17  # 16 chars + …


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

    def test_summary_updated_does_not_skip(self, handle):
        """Regression: ``_summary_updated`` is one-shot for the CardKit summary
        text patch (思考中→输入中). It must NOT gate tool history updates;
        every tool call after the first ``text_delta`` had been silently
        dropped, so the user saw no tool card.
        """
        handle._summary_updated = True
        handle.tool_status_update([{"name": "Read", "hint_data": "/a.py"}])
        assert len(handle._tool_history) == 1
        assert handle._tool_history[0]["name"] == "Read"

    def test_backfill_updates_empty_hint(self, handle):
        """_backfill entries update the last matching entry with empty hint."""
        handle.tool_status_update([
            {"name": "Read", "hint_data": ""},
        ])
        assert handle._tool_history[0]["hint"] == ""
        handle.tool_status_update([
            {"name": "Read", "hint_data": "/a/foo.py", "_backfill": True},
        ])
        assert handle._tool_history[0]["hint"] == "foo.py"

    def test_backfill_skips_if_hint_already_set(self, handle):
        """_backfill doesn't overwrite an existing non-empty hint."""
        handle.tool_status_update([
            {"name": "Read", "hint_data": "/a/foo.py"},
        ])
        handle.tool_status_update([
            {"name": "Read", "hint_data": "/a/bar.py", "_backfill": True},
        ])
        # Should NOT have been changed
        assert handle._tool_history[0]["hint"] == "foo.py"

    def test_backfill_no_new_entry(self, handle):
        """_backfill entries never create new history entries."""
        handle.tool_status_update([
            {"name": "Bash", "hint_data": "git status", "_backfill": True},
        ])
        assert len(handle._tool_history) == 0

    def test_task_excluded(self, handle):
        """Task tool is excluded from tool history (shown as agent)."""
        handle.tool_status_update([
            {"name": "Task", "hint_data": ""},
        ])
        assert len(handle._tool_history) == 0

    def test_subagent_included_in_tool_history(self, handle):
        """Subagent is rendered in the normal tool card."""
        handle.tool_status_update([
            {"name": "Subagent", "hint_data": "scout: 分析代码"},
        ])
        assert len(handle._tool_history) == 1
        assert handle._tool_history[0]["name"] == "Subagent"
        assert handle._tool_history[0]["label"] == "分发子任务"

    def test_tool_status_update_subagent_marks_prior_done(self, handle):
        """A new Subagent tool entry closes the prior in-flight tool."""
        handle.tool_status_update([
            {"name": "WebFetch", "hint_data": "https://example.com"},
        ])
        assert len(handle._tool_history) == 1
        assert handle._tool_history[0]["status"] == "running"
        handle.tool_status_update([
            {"name": "Subagent", "hint_data": ""},
        ])
        assert len(handle._tool_history) == 2
        assert handle._tool_history[0]["status"] == "done"
        assert handle._tool_history[1]["name"] == "Subagent"

    def test_tool_status_update_without_main_card_accumulates_history(self, handle):
        """tool_status_update accumulates _tool_history even when no main card exists.

        _render_progress is mocked so cardkit ops don't crash; _update_summary
        call is recorded by the mock but the real guard inside would no-op.
        """
        handle.card_message_id = None
        handle._cardkit_card_id = None
        handle.source_message_id = "src-msg-1"
        handle.tool_status_update([
            {"name": "Read", "hint_data": "/a/foo.py"},
        ])
        # _tool_history should still be populated
        assert len(handle._tool_history) == 1
        assert handle._tool_history[0]["label"] == "读取文件"
        # _render_progress was called (tool card path is independent)
        handle._render_progress.assert_called_once()

    def test_tool_status_update_no_source_msg_still_accumulates(self, handle):
        """tool_status_update still accumulates _tool_history without source_message_id."""
        handle.card_message_id = None
        handle._cardkit_card_id = None
        handle.source_message_id = None
        handle.tool_status_update([
            {"name": "Bash", "hint_data": "git status"},
        ])
        assert len(handle._tool_history) == 1
        assert handle._tool_history[0]["label"] == "执行命令"
        handle._render_progress.assert_called_once()

    def test_repeat_after_subagent_creates_new_entry(self, handle):
        """Subagent is no longer skipped, so following tools are separate entries."""
        handle.tool_status_update([
            {"name": "WebFetch", "hint_data": "https://example.com"},
        ])
        handle.tool_status_update([
            {"name": "Subagent", "hint_data": ""},
        ])
        handle.tool_status_update([
            {"name": "WebFetch", "hint_data": "https://example.com"},
        ])
        assert len(handle._tool_history) == 3
        assert [e["name"] for e in handle._tool_history] == ["WebFetch", "Subagent", "WebFetch"]
        assert handle._tool_history[-1]["status"] == "running"

    def test_mark_agents_completed_marks_not_clears(self, handle):
        """_mark_agents_completed should mark agents as completed, not clear them."""
        handle._active_agents = [
            {"status": "in_progress", "description": "分析代码", "subagent_type": "scout"},
        ]
        handle._mark_agents_completed()
        assert len(handle._active_agents) == 1
        assert handle._active_agents[0]["status"] == "completed"

    def test_agent_list_update_then_completed_renders_strikethrough(self, handle):
        """Full lifecycle: launch → complete → rendered as strikethrough."""
        handle._active_agents = []
        handle.agent_list_update([
            {"description": "分析代码", "name": None, "subagent_type": "scout"},
        ])
        assert len(handle._active_agents) == 1
        assert handle._active_agents[0]["status"] == "in_progress"
        handle._mark_agents_completed()
        assert handle._active_agents[0]["status"] == "completed"


# ---- Fix D: _send_tool_card real path (no main card) ----

def test_send_tool_card_no_main_card_replies_with_collapsible_panel():
    """Tool card is sent via IM reply when no main card exists.

    Verifies the full path: tool_status_update → _render_progress →
    _render_tool_progress → _update_tool_card → _send_tool_card →
    client.im.v1.message.reply.
    """
    from unittest.mock import MagicMock
    from collections import deque
    from feishu_bridge.ui import ResponseHandle

    handle = ResponseHandle.__new__(ResponseHandle)
    handle._tool_history = deque(maxlen=8)
    handle._terminated = False
    handle._summary_updated = False
    handle._active_agents = []
    handle._last_todos = None
    handle.card_message_id = None
    handle._cardkit_card_id = None
    handle._cardkit_seq = 0
    handle.thread_id = None
    handle.source_message_id = "om_test_src"
    handle._tool_msg_id = None

    # Mock the IM client; _update_summary / _render_agent_progress safely
    # no-op when _cardkit_card_id is None.
    mock_client = MagicMock()
    mock_reply_resp = MagicMock()
    mock_reply_resp.success.return_value = True
    mock_reply_resp.data.message_id = "msg-tool-test-999"
    mock_client.im.v1.message.reply.return_value = mock_reply_resp
    handle.client = mock_client

    handle.tool_status_update([
        {"name": "Bash", "hint_data": "git status"},
    ])

    # _tool_history grows
    assert len(handle._tool_history) == 1
    assert handle._tool_history[0]["label"] == "执行命令"

    # client.im.v1.message.reply was called
    mock_client.im.v1.message.reply.assert_called_once()

    # The card content sent to IM must contain collapsible_panel
    call_args = mock_client.im.v1.message.reply.call_args
    req = call_args[0][0]  # ReplyMessageRequest
    body = req.request_body  # ReplyMessageRequestBody
    assert "collapsible_panel" in body.content

    # _tool_msg_id is set from the successful reply
    assert handle._tool_msg_id == "msg-tool-test-999"


# ---- F-1: tool_status_end_update ----


class TestToolStatusEndUpdate:
    """Tests for ResponseHandle.tool_status_end_update."""

    @pytest.fixture
    def handle(self):
        h = ResponseHandle.__new__(ResponseHandle)
        h._cardkit_card_id = None
        h._tool_history = deque(maxlen=8)
        h._tool_msg_id = None
        h._seq_lock = threading.Lock()
        h._terminated = False
        h._summary_updated = False
        h._active_agents = []
        h._last_todos = None
        # Minimal mock so _render_progress doesn't crash
        h._update_tool_card = Mock()
        h._update_element = Mock()
        return h

    def test_single_call_start_then_end(self, handle):
        """start → status=running + tool_call_ids={"A"};
        end_update(["A"]) → status=done + tool_call_ids=set()."""
        handle.tool_status_update([
            {"name": "Read", "hint_data": "/a.py", "id": "A"},
        ])
        assert handle._tool_history[0]["status"] == "running"
        assert handle._tool_history[0]["tool_call_ids"] == {"A"}

        handle.tool_status_end_update(["A"])
        assert handle._tool_history[0]["status"] == "done"
        assert handle._tool_history[0]["tool_call_ids"] == set()

    def test_aggregated_two_starts_then_ends(self, handle):
        """Same label+hint ×2 → count=2 + tool_call_ids={"A","B"};
        end A → still running (ids={"B"}); end B → done (ids=set())."""
        handle.tool_status_update([
            {"name": "Read", "hint_data": "/x.py", "id": "A"},
        ])
        handle.tool_status_update([
            {"name": "Read", "hint_data": "/x.py", "id": "B"},
        ])
        assert len(handle._tool_history) == 1
        entry = handle._tool_history[0]
        assert entry["count"] == 2
        assert entry["tool_call_ids"] == {"A", "B"}
        assert entry["status"] == "running"

        handle.tool_status_end_update(["A"])
        assert entry["status"] == "running"  # B still pending
        assert entry["tool_call_ids"] == {"B"}

        handle.tool_status_end_update(["B"])
        assert entry["status"] == "done"
        assert entry["tool_call_ids"] == set()

    def test_id_not_found_no_raise(self, handle, caplog):
        """end_update(["X"]) where X is not in any entry → log warning, no raise."""
        handle.tool_status_update([
            {"name": "Read", "hint_data": "/a.py", "id": "A"},
        ])
        with caplog.at_level(logging.WARNING):
            handle.tool_status_end_update(["X"])
        assert "X not found in history" in caplog.text
        # Original entry untouched
        assert handle._tool_history[0]["status"] == "running"
        assert handle._tool_history[0]["tool_call_ids"] == {"A"}

    def test_empty_end_ids_noop(self, handle):
        """Empty list → no-op."""
        handle.tool_status_update([
            {"name": "Read", "hint_data": "/a.py", "id": "A"},
        ])
        handle.tool_status_end_update([])
        assert handle._tool_history[0]["status"] == "running"

    def test_start_without_id_then_end_without_id(self, handle, caplog):
        """Entry without tool_call_id survives end_update (no matching)."""
        handle.tool_status_update([
            {"name": "Read", "hint_data": "/a.py"},
        ])
        # Entry has empty tool_call_ids set (no id)
        assert handle._tool_history[0]["tool_call_ids"] == set()
        with caplog.at_level(logging.WARNING):
            handle.tool_status_end_update(["nonexistent"])
        assert "nonexistent not found in history" in caplog.text
        # status unchanged (can't be marked done without end event; F-3 handles it)
        assert handle._tool_history[0]["status"] == "running"


# ---- F-3: throttle removal / _update_tool_card immediate ----


class TestUpdateToolCardNoThrottle:
    """F-3.2: _update_tool_card patches immediately, no throttle."""

    @pytest.fixture
    def handle(self):
        h = ResponseHandle.__new__(ResponseHandle)
        h._tool_history = deque(maxlen=8)
        h._tool_msg_id = "om_tool_existing"
        h._terminated = False
        h.client = MagicMock()
        # Mock patch to succeed (correct path is im.v1.message.patch)
        mock_resp = MagicMock()
        mock_resp.success.return_value = True
        h.client.im.v1.message.patch.return_value = mock_resp
        return h

    def test_update_tool_card_no_throttle_patches_every_update(self, handle):
        """5 consecutive _update_tool_card calls → 5 patch calls (no throttle)."""
        mock_patch = handle.client.im.v1.message.patch

        for i in range(5):
            panels = [{"tag": "markdown", "content": f"panel_{i}"}]
            handle._update_tool_card(panels)

        assert mock_patch.call_count == 5
        # Last call should contain the 5th panel
        last_call = mock_patch.call_args_list[-1]
        req = last_call[0][0]
        body_content = req.request_body.content
        assert "panel_4" in body_content


# ---- F-3: _finalize_tool_card ----


class TestFinalizeToolCard:
    """F-3.3: _finalize_tool_card marks done and force-patches once."""

    @pytest.fixture
    def handle(self):
        h = ResponseHandle.__new__(ResponseHandle)
        h._tool_history = deque(maxlen=8)
        h._tool_msg_id = "om_tool_final"
        h._tool_card_finalized = False
        h._terminated = False
        h._seq_lock = threading.Lock()
        h._cardkit_card_id = None
        h._active_agents = []
        h.client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.success.return_value = True
        h.client.im.v1.message.patch.return_value = mock_resp
        return h

    def test_finalize_tool_card_marks_done_and_force_patches_once(self, handle):
        """2 running entries → finalize marks done, force-patches once."""
        # Set up 2 running entries with tool_call_ids
        handle._tool_history.append({
            "name": "Read", "label": "读取文件", "hint": "foo.py",
            "count": 1, "status": "running",
            "tool_call_ids": {"A"},
        })
        handle._tool_history.append({
            "name": "Bash", "label": "执行命令", "hint": "git status",
            "count": 1, "status": "running",
            "tool_call_ids": {"B"},
        })

        # Call twice — second should no-op
        handle._finalize_tool_card()
        handle._finalize_tool_card()

        # All entries marked done
        assert handle._tool_history[0]["status"] == "done"
        assert handle._tool_history[0]["tool_call_ids"] == set()
        assert handle._tool_history[1]["status"] == "done"
        assert handle._tool_history[1]["tool_call_ids"] == set()

        # Patch called only once
        mock_patch = handle.client.im.v1.message.patch
        assert mock_patch.call_count == 1

        # status_text = "**完成 (2)**"
        call = mock_patch.call_args
        req = call[0][0]
        body_content = req.request_body.content
        assert "**完成 (2)**" in body_content

    def test_finalize_tool_card_no_msg_id_noop(self, handle):
        """When _tool_msg_id is None, finalize still marks done but no patch."""
        handle._tool_msg_id = None
        handle._tool_history.append({
            "name": "Read", "label": "读取文件", "hint": "foo.py",
            "count": 1, "status": "running",
            "tool_call_ids": {"A"},
        })
        handle._finalize_tool_card()
        assert handle._tool_history[0]["status"] == "done"
        handle.client.im.v1.message.patch.assert_not_called()

    def test_finalize_no_history_no_patch(self, handle):
        """Empty history → no patch."""
        handle._finalize_tool_card()
        handle.client.im.v1.message.patch.assert_not_called()


# ---- F-3: deliver() finally calls _finalize_tool_card ----


class TestDeliverCallsFinalize:
    """F-3.4: All deliver paths call _finalize_tool_card in finally."""

    @pytest.fixture
    def handle(self):
        h = ResponseHandle.__new__(ResponseHandle)
        h._use_cardkit = False
        h._cardkit_card_id = None
        h.card_message_id = "om_main"
        h._terminated = False
        h._typing_reaction_id = None
        h._flush_ctrl = None
        h._card_fallback_timer = None
        h._tool_card_finalized = False
        h._tool_msg_id = None
        h._tool_history = deque(maxlen=8)
        h._handle_start_time = 1000.0
        h._seq_lock = threading.Lock()
        h._cardkit_seq = 0
        h.source_message_id = "om_src"
        h.thread_id = None
        h.chat_id = "oc_chat"
        h.bot_id = "bot_1"
        h._last_todos = None
        h._active_agents = []
        h.client = MagicMock()
        # Mock _try_patch to succeed
        h._try_patch = MagicMock(return_value=True)
        # Capture _finalize_tool_card
        h._finalize_tool_card = MagicMock()
        h._deliver_im_patch = MagicMock(return_value=True)
        return h

    def test_deliver_calls_finalize_on_im_patch_path(self, handle):
        """Non-cardkit deliver → _finalize_tool_card called once."""
        handle.deliver("hello")
        handle._finalize_tool_card.assert_called_once()

    def test_deliver_calls_finalize_on_cardkit_success(self, handle):
        """CardKit deliver success path → _finalize_tool_card called once."""
        handle._use_cardkit = True
        handle._cardkit_card_id = "ck_card_1"

        # Mock cardkit settings + update to succeed
        mock_settings_resp = MagicMock()
        mock_settings_resp.success.return_value = True
        handle.client.cardkit.v1.card.settings.return_value = mock_settings_resp

        mock_update_resp = MagicMock()
        mock_update_resp.success.return_value = True
        handle.client.cardkit.v1.card.update.return_value = mock_update_resp

        handle.deliver("hello")
        handle._finalize_tool_card.assert_called_once()

    def test_deliver_calls_finalize_on_cardkit_settings_failure_fallback(self, handle):
        """CardKit settings fail → fallback to IM → _finalize_tool_card."""
        handle._use_cardkit = True
        handle._cardkit_card_id = "ck_card_1"

        mock_resp = MagicMock()
        mock_resp.success.return_value = False
        mock_resp.code = -1
        mock_resp.msg = "error"
        handle.client.cardkit.v1.card.settings.return_value = mock_resp

        handle.deliver("hello")
        handle._finalize_tool_card.assert_called_once()
        # Fallback to IM was called
        handle._deliver_im_patch.assert_called_once()

    def test_deliver_calls_finalize_on_cardkit_update_failure_fallback(self, handle):
        """CardKit update fails → fallback to IM → _finalize_tool_card."""
        handle._use_cardkit = True
        handle._cardkit_card_id = "ck_card_1"

        mock_settings_resp = MagicMock()
        mock_settings_resp.success.return_value = True
        handle.client.cardkit.v1.card.settings.return_value = mock_settings_resp

        mock_update_resp = MagicMock()
        mock_update_resp.success.return_value = False
        mock_update_resp.code = -1
        mock_update_resp.msg = "update error"
        handle.client.cardkit.v1.card.update.return_value = mock_update_resp

        handle.deliver("hello")
        handle._finalize_tool_card.assert_called_once()
        # Fallback to IM was called
        handle._deliver_im_patch.assert_called_once()


# ---- F-5: main card tool panel fallback ----


class TestMainCardToolPanelFallback:
    """F-5: Main card only embeds tool panels on fallback (no standalone tool card)."""

    @pytest.fixture
    def handle(self):
        h = ResponseHandle.__new__(ResponseHandle)
        h._use_cardkit = True
        h._cardkit_card_id = "ck_card_1"
        h.card_message_id = "om_main"
        h._handle_start_time = 1000.0
        h._seq_lock = threading.Lock()
        h._cardkit_seq = 0
        h._terminated = False
        h._typing_reaction_id = None
        h._flush_ctrl = None
        h._card_fallback_timer = None
        h._tool_msg_id = None
        h._tool_history = deque(maxlen=8)
        h._tool_card_finalized = False
        h.source_message_id = "om_src"
        h.thread_id = None
        h.chat_id = "oc_chat"
        h.bot_id = "bot_1"
        h._last_todos = None
        h._active_agents = []
        h.client = MagicMock()
        # Mock _try_patch to succeed
        h._try_patch = MagicMock(return_value=True)
        # Capture build_cardkit_final_card for inspection
        return h

    def test_deliver_final_card_no_tool_panels_when_tool_card_exists(
            self, handle, monkeypatch):
        """When _tool_msg_id exists, final card has no tool collapsible_panel."""
        handle._tool_msg_id = "om_tool"
        handle._tool_history.append({
            "name": "Bash", "label": "执行命令", "hint": "git",
            "count": 1, "status": "running",
            "tool_call_ids": {"A"},
        })

        # Mock cardkit settings + update to succeed
        mock_settings_resp = MagicMock()
        mock_settings_resp.success.return_value = True
        handle.client.cardkit.v1.card.settings.return_value = mock_settings_resp

        captured_card = {}

        def fake_update(req):
            captured_card["data"] = json.loads(req.request_body.card.data)
            mock_resp = MagicMock()
            mock_resp.success.return_value = True
            return mock_resp

        handle.client.cardkit.v1.card.update = fake_update

        handle.deliver("hello")

        body_elements = captured_card["data"].get("body", {}).get("elements", [])
        contains_collapsible = any(
            el.get("tag") == "collapsible_panel" for el in body_elements
        )
        assert not contains_collapsible, (
            "Final card should NOT contain tool collapsible_panel "
            "when standalone tool card exists"
        )

    def test_deliver_final_card_fallback_embeds_tool_panels_when_no_tool_card(
            self, handle, monkeypatch):
        """When _tool_msg_id=None, final card includes tool collapsible_panel."""
        handle._tool_msg_id = None
        handle._tool_history.append({
            "name": "Bash", "label": "执行命令", "hint": "git",
            "count": 1, "status": "running",
            "tool_call_ids": {"A"},
        })

        mock_settings_resp = MagicMock()
        mock_settings_resp.success.return_value = True
        handle.client.cardkit.v1.card.settings.return_value = mock_settings_resp

        captured_card = {}

        def fake_update(req):
            captured_card["data"] = json.loads(req.request_body.card.data)
            mock_resp = MagicMock()
            mock_resp.success.return_value = True
            return mock_resp

        handle.client.cardkit.v1.card.update = fake_update

        handle.deliver("hello")

        body_elements = captured_card["data"].get("body", {}).get("elements", [])
        contains_collapsible = any(
            el.get("tag") == "collapsible_panel" for el in body_elements
        )
        assert contains_collapsible, (
            "Final card SHOULD contain tool collapsible_panel "
            "as fallback when standalone tool card was never created"
        )


# ---- F-3.6: _build_tool_panels_for_streaming header dynamic ----


class TestBuildToolPanelsForStreamingHeaders:
    """F-3.6: Panel headers show ✅ for done, ⏳ for running."""

    @pytest.fixture
    def handle(self):
        h = ResponseHandle.__new__(ResponseHandle)
        h._tool_history = deque(maxlen=8)
        return h

    def test_build_tool_panels_for_streaming_done_headers(self, handle):
        """Done entry → header content contains ✅; running → ⏳."""
        handle._tool_history.append({
            "name": "Read", "label": "读取文件", "hint": "a.py",
            "count": 1, "status": "running",
            "tool_call_ids": set(),
        })
        handle._tool_history.append({
            "name": "Bash", "label": "执行命令", "hint": "git",
            "count": 1, "status": "done",
            "tool_call_ids": set(),
        })

        panels = handle._build_tool_panels_for_streaming()
        assert len(panels) == 2

        # First panel (running) → ⏳
        header_0 = panels[0]["header"]["title"]["content"]
        assert "⏳" in header_0

        # Second panel (done) → ✅
        header_1 = panels[1]["header"]["title"]["content"]
        assert "✅" in header_1

    def test_all_done_panels_have_checkmark(self, handle):
        """After _finalize_tool_card, all panels show ✅."""
        handle._tool_history.append({
            "name": "Read", "label": "读取文件", "hint": "a.py",
            "count": 1, "status": "done",
            "tool_call_ids": set(),
        })
        handle._tool_history.append({
            "name": "Bash", "label": "执行命令", "hint": "git",
            "count": 1, "status": "done",
            "tool_call_ids": set(),
        })

        panels = handle._build_tool_panels_for_streaming()
        for p in panels:
            header = p["header"]["title"]["content"]
            assert "✅" in header
            assert "⏳" not in header


# ============================================================
# Regression: bridge live bugs (2026-06-18)
# ============================================================


class TestToolStatusUpdateNotGatedBySummary:
    """Regression: ``_summary_updated`` must NOT silently drop tool updates.

    The flag is one-shot for the CardKit summary patch (思考中→输入中). Earlier
    the same flag also gated ``tool_status_update``, so every tool that fired
    AFTER the first ``text_delta`` was silently dropped — the user saw no tool
    card and the standalone tool card was never created. Their ``toolcall_end``
    counterparts then logged ``id ... not found in history`` warnings.
    """

    @pytest.fixture
    def handle(self):
        h = ResponseHandle.__new__(ResponseHandle)
        h._tool_history = deque(maxlen=8)
        h._tool_msg_id = None
        h._terminated = False
        h._summary_updated = True  # first text_delta already streamed
        h._cardkit_card_id = None
        h._active_agents = []
        h._last_todos = None
        h._seq_lock = threading.Lock()
        h.client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.success.return_value = True
        h.client.im.v1.message.create.return_value = mock_resp
        h.client.im.v1.message.create.return_value.data.message_id = "om_tool_new"
        # _send_tool_card is the slow path; stub it out so we just check history
        h._send_tool_card = MagicMock(return_value="om_tool_new")
        return h

    def test_tool_status_update_runs_after_summary_updated(self, handle):
        """summary_updated=True must NOT skip; entry lands in history."""
        handle.tool_status_update([
            {"name": "Read", "hint_data": "/a/b.py", "id": "call_a"},
        ])
        assert len(handle._tool_history) == 1
        assert handle._tool_history[0]["name"] == "Read"
        assert "call_a" in handle._tool_history[0]["tool_call_ids"]

    def test_end_id_found_when_start_came_after_summary(self, handle):
        """start (post-summary) → end-id finds entry, marks done; no warning."""
        handle.tool_status_update([
            {"name": "Bash", "hint_data": "ls", "id": "call_b"},
        ])
        handle.tool_status_end_update(["call_b"])
        entry = handle._tool_history[0]
        assert entry["status"] == "done"
        assert entry["tool_call_ids"] == set()


class TestToolCardUsesV1ImApi:
    """Regression: tool card patches must hit ``client.im.v1.message.patch``.

    The Lark Python SDK exposes ``client.im.v1.message`` for v1 IM endpoints;
    ``client.im.message`` does not exist and raises ``AttributeError`` at
    runtime. The earlier code path silently logged ``Tool card patch error``
    for every update and every finalize, so the standalone tool card froze
    at ⏳ forever and never moved to ✅. A ``spec=`` mock catches the typo.
    """

    @pytest.fixture
    def handle(self):
        h = ResponseHandle.__new__(ResponseHandle)
        h._tool_history = deque(maxlen=8)
        h._tool_msg_id = "om_tool_existing"
        h._tool_card_finalized = False
        h._terminated = False
        h._seq_lock = threading.Lock()
        h._cardkit_card_id = None
        h._active_agents = []

        # spec-based client: only the documented v1 path exists.  Any access
        # to ``client.im.message`` raises AttributeError → bug reappears.
        from types import SimpleNamespace
        message_ns = MagicMock()
        message_ns.patch.return_value = MagicMock()
        message_ns.patch.return_value.success.return_value = True
        v1_ns = SimpleNamespace(message=message_ns)
        im_ns = SimpleNamespace(v1=v1_ns)
        h.client = SimpleNamespace(im=im_ns)
        return h

    def test_update_tool_card_calls_v1_patch(self, handle):
        handle._update_tool_card([{"tag": "markdown", "content": "x"}])
        handle.client.im.v1.message.patch.assert_called_once()

    def test_force_patch_tool_card_calls_v1_patch(self, handle):
        handle._tool_history.append({
            "name": "Read", "label": "读取文件", "hint": "a.py",
            "count": 1, "status": "done", "tool_call_ids": set(),
        })
        handle._force_patch_tool_card(
            [{"tag": "markdown", "content": "x"}], "**完成 (1)**")
        handle.client.im.v1.message.patch.assert_called_once()


# ============================================================
# pi-feishu parity: exec_args / exec_result rich tool cards
# ============================================================


class TestExecArgsBackfill:
    """tool_status_update receives _exec_args→backfills matching entry."""

    @pytest.fixture
    def handle(self):
        h = ResponseHandle.__new__(ResponseHandle)
        h._tool_history = deque(maxlen=8)
        h._tool_msg_id = None
        h._terminated = False
        h._cardkit_card_id = None
        h._active_agents = []
        h._last_todos = None
        h._seq_lock = threading.Lock()
        return h

    def test_exec_args_backfills_existing_entry(self, handle):
        """_exec_args with matching id writes exec_args on the entry."""
        handle.tool_status_update([
            {"name": "Bash", "hint_data": "ls", "id": "call_a"},
        ])
        handle.tool_status_update([
            {"name": "Bash", "hint_data": "", "id": "call_a",
             "_exec_args": {"command": "ls -la"}},
        ])
        entry = handle._tool_history[0]
        assert entry["exec_args"] == {"command": "ls -la"}
        # status unaffected by exec_args backfill
        assert entry["status"] == "running"

    def test_exec_args_fallback_name_match(self, handle):
        """exec_args with unmatched id falls back to name match on last entry."""
        handle.tool_status_update([
            {"name": "Bash", "hint_data": "ls", "id": "call_a"},
        ])
        handle.tool_status_update([
            {"name": "Bash", "hint_data": "", "id": "call_z",
             "_exec_args": {"command": "ls"}},
        ])
        # Should match by name "Bash" on the entry (fallback after id mismatch)
        assert handle._tool_history[0]["exec_args"] == {"command": "ls"}

    def test_exec_args_before_entry_no_crash(self, handle):
        """_exec_args with empty history → no crash."""
        handle.tool_status_update([
            {"name": "Bash", "hint_data": "", "id": "call_x",
             "_exec_args": {"command": "ls"}},
        ])
        assert len(handle._tool_history) == 0  # no entry created


class TestExecResultBackfill:
    """tool_status_update receives _exec_result→backfills matching entry."""

    @pytest.fixture
    def handle(self):
        h = ResponseHandle.__new__(ResponseHandle)
        h._tool_history = deque(maxlen=8)
        h._tool_msg_id = None
        h._terminated = False
        h._cardkit_card_id = None
        h._active_agents = []
        h._last_todos = None
        h._seq_lock = threading.Lock()
        return h

    def test_exec_result_backfills_existing_entry(self, handle):
        """_exec_result with matching id writes result on the entry."""
        handle.tool_status_update([
            {"name": "Read", "hint_data": "/a.py", "id": "call_r1"},
        ])
        result = {"content": [{"type": "text", "text": "line1\nline2"}]}
        handle.tool_status_update([
            {"name": "Read", "hint_data": "", "id": "call_r1",
             "_exec_result": result, "_is_error": False},
        ])
        entry = handle._tool_history[0]
        assert entry["exec_result"] is result
        assert entry["result_is_error"] is False

    def test_exec_result_error_flag(self, handle):
        """_exec_result with _is_error=True sets result_is_error."""
        handle.tool_status_update([
            {"name": "Bash", "hint_data": "x", "id": "call_e"},
        ])
        handle.tool_status_update([
            {"name": "Bash", "hint_data": "", "id": "call_e",
             "_exec_result": {"content": [{"type": "text", "text": "fail"}]},
             "_is_error": True},
        ])
        assert handle._tool_history[0]["result_is_error"] is True

    def test_exec_result_aggregated_entry_uses_tool_call_ids(self, handle):
        """exec_result matches via tool_call_ids set, not just last entry."""
        # Two same-label entries create aggregation (count++), sharing tool_call_ids.
        handle.tool_status_update([
            {"name": "Bash", "hint_data": "ls", "id": "call1"},
        ])
        handle.tool_status_update([
            {"name": "Bash", "hint_data": "ls", "id": "call2"},
        ])
        # call1 should be in the aggregated entry's tool_call_ids
        assert len(handle._tool_history) == 1
        assert "call1" in handle._tool_history[0]["tool_call_ids"]
        assert "call2" in handle._tool_history[0]["tool_call_ids"]

        result1 = {"content": [{"type": "text", "text": "out1"}]}
        handle.tool_status_update([
            {"name": "Bash", "hint_data": "", "id": "call1",
             "_exec_result": result1},
        ])
        assert handle._tool_history[0]["exec_result"] is result1


class TestExtractResultText:
    """_extract_result_text extracts readable text from pi result envelope."""

    def test_single_text_block(self):
        result = {"content": [{"type": "text", "text": "hello world"}]}
        text = ResponseHandle._extract_result_text(result)
        assert text == "hello world"

    def test_multiple_text_blocks(self):
        result = {
            "content": [
                {"type": "text", "text": "line1"},
                {"type": "text", "text": "line2"},
            ]
        }
        text = ResponseHandle._extract_result_text(result)
        assert text == "line1\nline2"

    def test_empty_content(self):
        result = {"content": []}
        text = ResponseHandle._extract_result_text(result)
        assert isinstance(text, str)

    def test_non_dict_fallback(self):
        text = ResponseHandle._extract_result_text("plain string")
        assert text == "plain string"

    def test_no_text_content_falls_back_to_json(self):
        result = {"key": "val"}
        text = ResponseHandle._extract_result_text(result)
        assert '"key"' in text


class TestBuildToolPanelElements:
    """_build_tool_panel_elements renders args/result in collapsible panels."""

    @pytest.fixture
    def handle(self):
        h = ResponseHandle.__new__(ResponseHandle)
        h._tool_history = deque(maxlen=8)
        h._tool_msg_id = None
        h._terminated = False
        h._cardkit_card_id = None
        h._active_agents = []
        h._last_todos = None
        h._seq_lock = threading.Lock()
        return h

    def test_basic_entry_no_exec_data(self, handle):
        """Entry without exec_args/exec_result → only tool name + hint."""
        elements = handle._build_tool_panel_elements(
            {"name": "Read", "label": "读取文件", "hint": "foo.py"},
            "Read", "foo.py",
        )
        # Tool name always present
        assert any("**工具名**: Read" in e["content"] for e in elements)
        # Hint present
        assert any("foo.py" in e["content"] for e in elements)
        # No args/result blocks
        contents = " ".join(e["content"] for e in elements)
        assert "输入参数" not in contents
        assert "输出结果" not in contents

    def test_entry_with_exec_args(self, handle):
        """Entry with exec_args renders 输入参数 block."""
        elements = handle._build_tool_panel_elements(
            {"name": "Bash", "label": "执行命令", "hint": "echo",
             "exec_args": {"command": "echo hello"}},
            "Bash", "echo",
        )
        contents = " ".join(e["content"] for e in elements)
        assert "输入参数" in contents
        assert '"command"' in contents
        assert "echo hello" in contents

    def test_entry_with_exec_result(self, handle):
        """Entry with exec_result renders 输出结果 block."""
        elements = handle._build_tool_panel_elements(
            {"name": "Read", "label": "读取文件", "hint": "a.py",
             "exec_result": {"content": [{"type": "text", "text": "abc123"}]}},
            "Read", "a.py",
        )
        contents = " ".join(e["content"] for e in elements)
        assert "输出结果" in contents
        assert "abc123" in contents

    def test_entry_with_error_result(self, handle):
        """Entry with error result shows ❌ marker."""
        elements = handle._build_tool_panel_elements(
            {"name": "Bash", "label": "执行命令", "hint": "x",
             "exec_result": {"content": [{"type": "text", "text": "fail"}]},
             "result_is_error": True},
            "Bash", "x",
        )
        contents = " ".join(e["content"] for e in elements)
        assert "输出结果" in contents
        assert "❌" in contents

    def test_entry_with_both_args_and_result(self, handle):
        """Entry with both exec_args and exec_result renders both blocks."""
        elements = handle._build_tool_panel_elements(
            {"name": "Read", "label": "读取文件", "hint": "/x",
             "exec_args": {"path": "/x"},
             "exec_result": {"content": [{"type": "text", "text": "content"}]}},
            "Read", "/x",
        )
        contents = " ".join(e["content"] for e in elements)
        assert "输入参数" in contents
        assert "输出结果" in contents


# ============================================================
# Subagent agent_result_update (T2.1)
# ============================================================


class TestAgentResultUpdate:
    """Tests for ResponseHandle.agent_result_update."""

    @pytest.fixture
    def handle(self):
        h = ResponseHandle.__new__(ResponseHandle)
        h._active_agents = []
        h._cardkit_card_id = "card-1"
        h._terminated = False
        h._tool_history = []
        h._last_todos = None
        h._seq_lock = threading.Lock()
        h._cardkit_seq = 0
        h._render_progress = Mock()
        h._render_agent_progress = Mock()
        return h

    def _add_agent(self, handle, desc, subagent_type, tool_call_id=None,
                   status="in_progress"):
        a = {"description": desc, "subagent_type": subagent_type,
             "status": status, "result_text": None, "name": None}
        if tool_call_id:
            a["tool_call_id"] = tool_call_id
        handle._active_agents.append(a)
        return a

    def test_matches_by_tool_call_id_single(self, handle):
        """Single agent matched by exact tool_call_id."""
        self._add_agent(handle, "分析代码", "scout", tool_call_id="call_a")
        result = {"content": [{"type": "text", "text": "done"}]}
        handle.agent_result_update([
            {"toolCallId": "call_a", "result": result, "isError": False},
        ])
        a = handle._active_agents[0]
        assert a["status"] == "completed"
        assert a["result_text"] == "done"
        handle._render_progress.assert_called_once()

    def test_matches_parallel_batch_by_call_id_prefix(self, handle):
        """Parallel batch: call_id:index entries match end event's bare call_id."""
        self._add_agent(handle, "分析认证", "scout", tool_call_id="batch_1:0")
        self._add_agent(handle, "分析路由", "scout", tool_call_id="batch_1:1")
        self._add_agent(handle, "分析数据", "scout", tool_call_id="batch_2:0")
        result = {"content": [{"type": "text", "text": "batch done"}]}
        handle.agent_result_update([
            {"toolCallId": "batch_1", "result": result, "isError": False},
        ])
        # Agents with batch_1:* → completed
        assert handle._active_agents[0]["status"] == "completed"
        assert handle._active_agents[1]["status"] == "completed"
        # Agent with batch_2:* → still in_progress
        assert handle._active_agents[2]["status"] == "in_progress"

    def test_sets_error_status(self, handle):
        """isError=True → status='error'."""
        self._add_agent(handle, "失败任务", "developer", tool_call_id="call_e")
        result = {"content": [{"type": "text", "text": "failed"}]}
        handle.agent_result_update([
            {"toolCallId": "call_e", "result": result, "isError": True},
        ])
        assert handle._active_agents[0]["status"] == "error"
        assert handle._active_agents[0]["result_text"] == "failed"

    def test_description_fallback_single_candidate(self, handle):
        """No tool_call_id match → description fallback with 1 candidate."""
        self._add_agent(handle, "分析代码", "scout")
        result = {"content": [{"type": "text", "text": "ok"}]}
        handle.agent_result_update([
            {"toolCallId": "unknown", "result": result, "isError": False,
             "description": "分析代码"},
        ])
        # tool_call_id not matched, but description matches single candidate
        assert handle._active_agents[0]["status"] == "completed"

    def test_description_fallback_ambiguous_skips(self, handle):
        """Description matches multiple candidates → skip (don't guess)."""
        self._add_agent(handle, "分析", "scout", tool_call_id="call_x")
        self._add_agent(handle, "分析", "developer", tool_call_id="call_y")
        result = {"content": [{"type": "text", "text": "ok"}]}
        handle.agent_result_update([
            {"toolCallId": "unknown", "result": result, "isError": False,
             "description": "分析"},
        ])
        # Ambiguous: both still in_progress
        assert handle._active_agents[0]["status"] == "in_progress"
        assert handle._active_agents[1]["status"] == "in_progress"
        # render_progress NOT called (no change)
        handle._render_progress.assert_not_called()

    def test_empty_results_noop(self, handle):
        """Empty results list → no-op."""
        self._add_agent(handle, "任务", "scout")
        handle.agent_result_update([])
        assert handle._active_agents[0]["status"] == "in_progress"

    def test_terminated_skips(self, handle):
        """When terminated, agent_result_update is skipped."""
        self._add_agent(handle, "任务", "scout", tool_call_id="call_a")
        handle._terminated = True
        handle.agent_result_update([
            {"toolCallId": "call_a", "result": {}, "isError": False},
        ])
        assert handle._active_agents[0]["status"] == "in_progress"


class TestAgentListUpdateEnsureCard:
    """T2.2: agent_list_update proactively creates card when none exists."""

    @pytest.fixture
    def handle(self):
        h = ResponseHandle.__new__(ResponseHandle)
        h.card_message_id = None
        h._cardkit_card_id = None
        h._active_agents = []
        h._terminated = False
        h._render_progress = Mock()
        h._ensure_card = Mock(return_value=True)
        # Simulate card creation succeeded
        h._ensure_card.return_value = True
        h._cardkit_card_id = None  # _ensure_card mock bypasses real creation
        return h

    def test_agent_list_update_calls_ensure_card_when_no_card(self, handle):
        """No card_message_id → _ensure_card("") is called."""
        handle.agent_list_update([
            {"description": "分析代码", "subagent_type": "scout",
             "tool_call_id": "call_a"},
        ])
        handle._ensure_card.assert_called_once_with("")

    def test_agent_list_update_skips_ensure_card_when_card_exists(self, handle):
        """Card already exists → _ensure_card is NOT called."""
        handle.card_message_id = "msg-1"
        handle.agent_list_update([
            {"description": "分析代码", "subagent_type": "scout",
             "tool_call_id": "call_a"},
        ])
        handle._ensure_card.assert_not_called()

    def test_agent_list_update_terminated_skips_all(self, handle):
        """When terminated, skip everything (no card creation, no agent add)."""
        handle._terminated = True
        handle.agent_list_update([
            {"description": "分析代码", "subagent_type": "scout"},
        ])
        handle._ensure_card.assert_not_called()
        assert handle._active_agents == []


class TestFormatAgentsMarkdown:
    """T3.1: _format_agents_markdown shared formatter."""

    def test_empty_agents_returns_empty(self):
        from feishu_bridge.ui import ResponseHandle
        assert ResponseHandle._format_agents_markdown([]) == ""
        assert ResponseHandle._format_agents_markdown(None) == ""

    def test_in_progress_agent(self):
        from feishu_bridge.ui import ResponseHandle
        agents = [{"description": "分析", "subagent_type": "scout",
                   "status": "in_progress", "name": None}]
        md = ResponseHandle._format_agents_markdown(agents)
        assert "◉ **分析 (scout)**" in md

    def test_completed_agent_with_result(self):
        from feishu_bridge.ui import ResponseHandle
        agents = [{"description": "分析", "subagent_type": "scout",
                   "status": "completed", "name": None,
                   "result_text": "done"}]
        md = ResponseHandle._format_agents_markdown(agents)
        assert "~~☑ 分析 (scout)~~" in md
        assert "done" in md

    def test_completed_without_subagent_type(self):
        from feishu_bridge.ui import ResponseHandle
        agents = [{"description": "任务", "subagent_type": "",
                   "status": "completed", "name": None}]
        md = ResponseHandle._format_agents_markdown(agents)
        # No suffix in parens
        assert "~~☑ 任务~~" in md

    def test_error_agent_with_result(self):
        from feishu_bridge.ui import ResponseHandle
        agents = [{"description": "失败", "subagent_type": "developer",
                   "status": "error", "name": None,
                   "result_text": "boom"}]
        md = ResponseHandle._format_agents_markdown(agents)
        assert "❌ **失败 (developer)**" in md
        assert "boom" in md


# ---- GetSubagentResult in tool_status_update ----


def test_tool_status_update_includes_get_subagent_result():
    """GetSubagentResult is rendered as a normal tool panel entry."""
    from collections import deque
    from unittest.mock import MagicMock
    from feishu_bridge.ui import ResponseHandle

    h = ResponseHandle.__new__(ResponseHandle)
    h._tool_history = deque(maxlen=8)
    h._terminated = False
    h._summary_updated = False
    h.card_message_id = "msg-1"
    h._cardkit_card_id = "card-1"
    h._update_summary = MagicMock()
    h._render_progress = MagicMock()
    h._ensure_card = MagicMock(return_value=True)

    h.tool_status_update([
        {"name": "Read", "hint_data": "/a/foo.py"},
        {"name": "GetSubagentResult", "hint_data": "subagent-abc"},
    ])
    assert len(h._tool_history) == 2
    assert h._tool_history[0]["name"] == "Read"
    assert h._tool_history[0]["status"] == "done"
    assert h._tool_history[1]["name"] == "GetSubagentResult"
    assert h._tool_history[1]["label"] == "查询后台任务"
