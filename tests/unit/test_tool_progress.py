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

    def test_summary_updated_skips(self, handle):
        handle._summary_updated = True
        handle.tool_status_update([{"name": "Read", "hint_data": "/a.py"}])
        assert len(handle._tool_history) == 0

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

    def test_subagent_excluded_from_tool_history(self, handle):
        """Subagent is always routed through agent_list_update, never tool card."""
        handle.tool_status_update([
            {"name": "Subagent", "hint_data": "scout: 分析代码"},
        ])
        assert len(handle._tool_history) == 0

    def test_tool_status_update_skip_marks_prior_running_as_done(self, handle):
        """When a skipped tool (Subagent, etc.) arrives, prior in-flight
        tool entries should be marked done, not left as running forever."""
        # Push WebFetch tool_call → 1 entry status=running
        handle.tool_status_update([
            {"name": "WebFetch", "hint_data": "https://example.com"},
        ])
        assert len(handle._tool_history) == 1
        assert handle._tool_history[0]["status"] == "running"
        # Push Subagent tool_call → skipped, but marks prior running → done
        handle.tool_status_update([
            {"name": "Subagent", "hint_data": ""},
        ])
        # History still has 1 entry (Subagent skipped), but WebFetch is done
        assert len(handle._tool_history) == 1
        assert handle._tool_history[0]["status"] == "done"

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

    def test_repeat_after_skip_restores_running(self, handle):
        """When a skipped tool (Subagent) marks a prior entry done,
        repeating the same tool should restore running status."""
        # Step 1: WebFetch → entry status=running, count=1
        handle.tool_status_update([
            {"name": "WebFetch", "hint_data": "https://example.com"},
        ])
        assert len(handle._tool_history) == 1
        assert handle._tool_history[0]["status"] == "running"
        assert handle._tool_history[0]["count"] == 1
        # Step 2: Subagent → skip, marks prior running → done
        handle.tool_status_update([
            {"name": "Subagent", "hint_data": ""},
        ])
        assert len(handle._tool_history) == 1
        assert handle._tool_history[0]["status"] == "done"
        assert handle._tool_history[0]["count"] == 1
        # Step 3: WebFetch (same hint) → repeat entry, restore running
        handle.tool_status_update([
            {"name": "WebFetch", "hint_data": "https://example.com"},
        ])
        assert len(handle._tool_history) == 1
        assert handle._tool_history[0]["status"] == "running"
        assert handle._tool_history[0]["count"] == 2

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
        # Mock patch to succeed
        mock_resp = MagicMock()
        mock_resp.success.return_value = True
        h.client.im.message.patch.return_value = mock_resp
        return h

    def test_update_tool_card_no_throttle_patches_every_update(self, handle):
        """5 consecutive _update_tool_card calls → 5 patch calls (no throttle)."""
        mock_patch = handle.client.im.message.patch

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
        h.client.im.message.patch.return_value = mock_resp
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
        mock_patch = handle.client.im.message.patch
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
        handle.client.im.message.patch.assert_not_called()

    def test_finalize_no_history_no_patch(self, handle):
        """Empty history → no patch."""
        handle._finalize_tool_card()
        handle.client.im.message.patch.assert_not_called()


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
