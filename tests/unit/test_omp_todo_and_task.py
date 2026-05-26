"""Unit tests for OMP todo state machine and task progress display.

Covers:
- StreamState.apply_todo_ops: init, start, done, drop, rm, append, note
- StreamState.get_todo_list: flattening and status correctness
- _extract_hint_data: OMP Task format (tasks[] array)
- ResponseHandle._format_todos: dropped status rendering
"""

import pytest

from feishu_bridge.runtime import StreamState, _extract_hint_data
from feishu_bridge.ui import ResponseHandle


# ---------------------------------------------------------------------------
# Todo state machine
# ---------------------------------------------------------------------------

class TestTodoInit:
    def test_init_creates_phases_and_tasks(self):
        s = StreamState()
        s.apply_todo_ops([{
            "op": "init",
            "list": [
                {"phase": "Build", "items": ["a", "b"]},
                {"phase": "Test", "items": ["t1"]},
            ],
        }])
        todos = s.get_todo_list()
        assert len(todos) == 3
        assert todos[0] == {"content": "a", "status": "in_progress"}
        assert todos[1] == {"content": "b", "status": "pending"}
        assert todos[2] == {"content": "t1", "status": "pending"}

    def test_init_resets_prior_state(self):
        s = StreamState()
        s.apply_todo_ops([{"op": "init", "list": [{"phase": "P", "items": ["x"]}]}])
        s.apply_todo_ops([{"op": "init", "list": [{"phase": "Q", "items": ["y"]}]}])
        todos = s.get_todo_list()
        assert len(todos) == 1
        assert todos[0]["content"] == "y"

    def test_init_empty_list(self):
        s = StreamState()
        s.apply_todo_ops([{"op": "init", "list": []}])
        assert s.get_todo_list() == []


class TestTodoStart:
    def test_start_activates_task(self):
        s = StreamState()
        s.apply_todo_ops([{"op": "init", "list": [{"phase": "P", "items": ["a", "b"]}]}])
        s.apply_todo_ops([{"op": "start", "task": "b"}])
        todos = s.get_todo_list()
        assert todos[0]["status"] == "pending"   # demoted from in_progress
        assert todos[1]["status"] == "in_progress"

    def test_start_demotes_existing_active(self):
        s = StreamState()
        s.apply_todo_ops([{"op": "init", "list": [{"phase": "P", "items": ["a", "b", "c"]}]}])
        # a is auto-promoted
        assert s.get_todo_list()[0]["status"] == "in_progress"
        s.apply_todo_ops([{"op": "start", "task": "c"}])
        todos = s.get_todo_list()
        assert todos[0]["status"] == "pending"
        assert todos[2]["status"] == "in_progress"
        # Only one task should be in_progress
        assert sum(1 for t in todos if t["status"] == "in_progress") == 1


class TestTodoDone:
    def test_done_task_and_auto_promote(self):
        s = StreamState()
        s.apply_todo_ops([{"op": "init", "list": [{"phase": "P", "items": ["a", "b"]}]}])
        s.apply_todo_ops([{"op": "done", "task": "a"}])
        todos = s.get_todo_list()
        assert todos[0]["status"] == "completed"
        assert todos[1]["status"] == "in_progress"

    def test_done_phase(self):
        s = StreamState()
        s.apply_todo_ops([{
            "op": "init",
            "list": [
                {"phase": "A", "items": ["a1", "a2"]},
                {"phase": "B", "items": ["b1"]},
            ],
        }])
        s.apply_todo_ops([{"op": "done", "phase": "A"}])
        todos = s.get_todo_list()
        assert todos[0]["status"] == "completed"
        assert todos[1]["status"] == "completed"
        assert todos[2]["status"] == "in_progress"  # auto-promoted


class TestTodoDrop:
    def test_drop_task(self):
        s = StreamState()
        s.apply_todo_ops([{"op": "init", "list": [{"phase": "P", "items": ["a", "b"]}]}])
        s.apply_todo_ops([{"op": "drop", "task": "a"}])
        todos = s.get_todo_list()
        assert todos[0]["status"] == "dropped"
        assert todos[1]["status"] == "in_progress"

    def test_drop_phase_marks_dropped_not_completed(self):
        s = StreamState()
        s.apply_todo_ops([{
            "op": "init",
            "list": [
                {"phase": "A", "items": ["a1"]},
                {"phase": "B", "items": ["b1"]},
            ],
        }])
        s.apply_todo_ops([{"op": "drop", "phase": "A"}])
        todos = s.get_todo_list()
        assert todos[0]["status"] == "dropped"
        assert todos[1]["status"] == "in_progress"


class TestTodoRm:
    def test_rm_task(self):
        s = StreamState()
        s.apply_todo_ops([{"op": "init", "list": [{"phase": "P", "items": ["a", "b"]}]}])
        s.apply_todo_ops([{"op": "rm", "task": "a"}])
        todos = s.get_todo_list()
        assert len(todos) == 1
        assert todos[0]["content"] == "b"

    def test_rm_phase(self):
        s = StreamState()
        s.apply_todo_ops([{
            "op": "init",
            "list": [
                {"phase": "A", "items": ["a1"]},
                {"phase": "B", "items": ["b1"]},
            ],
        }])
        s.apply_todo_ops([{"op": "rm", "phase": "A"}])
        todos = s.get_todo_list()
        assert len(todos) == 1
        assert todos[0]["content"] == "b1"

    def test_rm_all(self):
        s = StreamState()
        s.apply_todo_ops([{"op": "init", "list": [{"phase": "P", "items": ["a"]}]}])
        s.apply_todo_ops([{"op": "rm"}])
        assert s.get_todo_list() == []

    def test_rm_active_task_auto_promotes(self):
        s = StreamState()
        s.apply_todo_ops([{"op": "init", "list": [{"phase": "P", "items": ["a", "b"]}]}])
        # a is in_progress
        s.apply_todo_ops([{"op": "rm", "task": "a"}])
        todos = s.get_todo_list()
        assert len(todos) == 1
        assert todos[0]["status"] == "in_progress"


class TestTodoAppend:
    def test_append_to_existing_phase(self):
        s = StreamState()
        s.apply_todo_ops([{"op": "init", "list": [{"phase": "P", "items": ["a"]}]}])
        s.apply_todo_ops([{"op": "append", "phase": "P", "items": ["b"]}])
        todos = s.get_todo_list()
        assert len(todos) == 2
        assert todos[1]["content"] == "b"

    def test_append_creates_new_phase(self):
        s = StreamState()
        s.apply_todo_ops([{"op": "append", "phase": "New", "items": ["x"]}])
        todos = s.get_todo_list()
        assert len(todos) == 1
        assert todos[0] == {"content": "x", "status": "in_progress"}


class TestTodoNote:
    def test_note_is_noop(self):
        s = StreamState()
        s.apply_todo_ops([{"op": "init", "list": [{"phase": "P", "items": ["a"]}]}])
        before = s.get_todo_list()
        s.apply_todo_ops([{"op": "note", "task": "a", "text": "some note"}])
        assert s.get_todo_list() == before


class TestTodoEdgeCases:
    def test_invalid_ops_ignored(self):
        s = StreamState()
        s.apply_todo_ops(["not_a_dict", None, 42])
        assert s.get_todo_list() == []

    def test_unknown_op_ignored(self):
        s = StreamState()
        s.apply_todo_ops([{"op": "init", "list": [{"phase": "P", "items": ["a"]}]}])
        s.apply_todo_ops([{"op": "unknown_op", "task": "a"}])
        assert len(s.get_todo_list()) == 1


# ---------------------------------------------------------------------------
# _extract_hint_data — OMP Task format
# ---------------------------------------------------------------------------

class TestExtractHintDataTask:
    def test_omp_tasks_array(self):
        args = {
            "agent": "task",
            "tasks": [
                {"id": "A", "description": "Do X"},
                {"id": "B", "description": "Do Y"},
            ],
        }
        hint = _extract_hint_data("Task", args)
        assert "Do X" in hint
        assert "Do Y" in hint

    def test_alma_top_level_description(self):
        args = {"description": "Fix login", "name": "Fixer"}
        assert _extract_hint_data("Task", args) == "Fix login"

    def test_empty_args_returns_empty(self):
        assert _extract_hint_data("Task", {}) == ""

    def test_fallback_to_intent(self):
        assert _extract_hint_data("Task", {"_i": "intent text"}) == "intent text"

    def test_truncation_at_40(self):
        args = {"tasks": [{"id": "X", "description": "A" * 100}]}
        assert len(_extract_hint_data("Task", args)) == 40


# ---------------------------------------------------------------------------
# _format_todos — dropped status rendering
# ---------------------------------------------------------------------------

class TestFormatTodosDropped:
    def test_dropped_renders_with_strikethrough_cross(self):
        fmt = ResponseHandle._format_todos([
            {"content": "abandoned", "status": "dropped"},
        ])
        assert "~~✗ abandoned~~" in fmt

    def test_all_statuses(self):
        fmt = ResponseHandle._format_todos([
            {"content": "done", "status": "completed"},
            {"content": "active", "status": "in_progress"},
            {"content": "waiting", "status": "pending"},
            {"content": "gone", "status": "dropped"},
        ])
        assert "~~☑ done~~" in fmt
        assert "◉ **active**" in fmt
        assert "☐ waiting" in fmt
        assert "~~✗ gone~~" in fmt
