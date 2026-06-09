#!/usr/bin/env python3
"""Unit tests for the dynamic silent-output timeout budget (pi-tool-active-silent-budget).

Covers the pure budget-synthesis function and the pi tool-active lifecycle
counting that feeds it. Timer-thread races are not exercised here (non-deterministic);
the generation-guard correctness is verified by construction in runtime.py and the
budget/state logic — the deterministic part — is what these tests pin.
"""

from feishu_bridge.runtime import (
    BG_AGENT_SILENT_TIMEOUT,
    SILENT_TIMEOUT,
    TOOL_ACTIVE_SILENT_TIMEOUT,
    StreamState,
    compute_silent_budget,
)
from feishu_bridge.runtime_pi import PiRunner


def _runner(tmp_path, **kwargs):
    params = {
        "command": "pi",
        "model": "Qwen3.6-35B-A3B-mxfp4",
        "workspace": str(tmp_path),
        "timeout": 30,
        "safety_prompt_mode": "off",
    }
    params.update(kwargs)
    return PiRunner(**params)


# ── compute_silent_budget: pure synthesis ──

def test_budget_idle_is_base():
    assert compute_silent_budget(
        SILENT_TIMEOUT, bg_agent_running=False,
        tool_active_count=0, tool_active_enabled=True) == SILENT_TIMEOUT


def test_budget_tool_active_raises_to_tool_window():
    assert compute_silent_budget(
        SILENT_TIMEOUT, bg_agent_running=False,
        tool_active_count=1, tool_active_enabled=True) == TOOL_ACTIVE_SILENT_TIMEOUT


def test_budget_bg_agent_raises_to_bg_window():
    assert compute_silent_budget(
        SILENT_TIMEOUT, bg_agent_running=True,
        tool_active_count=0, tool_active_enabled=True) == BG_AGENT_SILENT_TIMEOUT


def test_budget_takes_max_when_both_active():
    # bg (3600) > tool-active (1800) → max wins
    assert compute_silent_budget(
        SILENT_TIMEOUT, bg_agent_running=True,
        tool_active_count=3, tool_active_enabled=True) == BG_AGENT_SILENT_TIMEOUT


def test_budget_flag_off_ignores_tool_active():
    assert compute_silent_budget(
        SILENT_TIMEOUT, bg_agent_running=False,
        tool_active_count=5, tool_active_enabled=False) == SILENT_TIMEOUT


def test_budget_flag_off_still_honors_bg_agent():
    # Feature flag gates only the pi tool-active term, never the Claude bg latch.
    assert compute_silent_budget(
        SILENT_TIMEOUT, bg_agent_running=True,
        tool_active_count=5, tool_active_enabled=False) == BG_AGENT_SILENT_TIMEOUT


# ── StreamState defaults: new fields default to inactive ──

def test_streamstate_new_fields_default_inactive():
    s = StreamState()
    assert s.tool_active_count == 0
    assert s.pending_silent_reset is False


# ── pi tool lifecycle → active count + heartbeat ──

def test_pi_tool_execution_start_marks_active_and_heartbeat(tmp_path):
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line(
        {"type": "tool_execution_start", "toolName": "bash"}, state)
    assert state.tool_active_count == 1
    assert state.pending_silent_reset is True


def test_pi_tool_execution_end_clears_active_and_heartbeat(tmp_path):
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line(
        {"type": "tool_execution_start", "toolName": "bash"}, state)
    state.pending_silent_reset = False  # consumed by loop
    runner.parse_streaming_line(
        {"type": "tool_execution_end", "toolName": "bash"}, state)
    assert state.tool_active_count == 0
    assert state.pending_silent_reset is True


def test_pi_unbalanced_end_clamps_to_zero(tmp_path):
    """Stray end (no matching start) must not drive the count negative."""
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line(
        {"type": "tool_execution_end", "toolName": "bash"}, state)
    assert state.tool_active_count == 0


def test_pi_nested_starts_count_up(tmp_path):
    runner = _runner(tmp_path)
    state = StreamState()
    for _ in range(3):
        runner.parse_streaming_line(
            {"type": "tool_execution_start", "toolName": "bash"}, state)
    assert state.tool_active_count == 3
    runner.parse_streaming_line(
        {"type": "tool_execution_end", "toolName": "bash"}, state)
    assert state.tool_active_count == 2


def test_pi_turn_end_resets_active_count(tmp_path):
    """A missing tool_execution_end must not leak active count past turn end."""
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line(
        {"type": "tool_execution_start", "toolName": "bash"}, state)
    runner.parse_streaming_line(
        {"type": "turn_end",
         "message": {"role": "assistant", "stopReason": "stop",
                     "content": [{"type": "text", "text": "done"}]}}, state)
    assert state.tool_active_count == 0


def test_pi_error_resets_active_count(tmp_path):
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line(
        {"type": "tool_execution_start", "toolName": "bash"}, state)
    runner.parse_streaming_line(
        {"type": "error", "message": "boom"}, state)
    assert state.tool_active_count == 0


# ── _run_streaming integration: the actual loop logic (budget re-arm) ──
#
# These drive the shared loop with a fake proc + a recording Timer that never
# fires on its own, so we can assert exactly which silent budgets the loop armed.

from feishu_bridge import runtime  # noqa: E402

_SILENT_BUDGETS = {SILENT_TIMEOUT, BG_AGENT_SILENT_TIMEOUT, TOOL_ACTIVE_SILENT_TIMEOUT}


class _FakeProc:
    def __init__(self, lines):
        self.stdout = iter(lines)
        self.stderr = iter([])
        self.returncode = 0
        self.pid = 4242

    def wait(self, timeout=None):
        return 0


def _patch_recording_timer(monkeypatch):
    """Replace threading.Timer with a recorder that does not auto-fire.

    Returns (started_intervals, created_timers). Idle timers arm at the runner
    timeout (30); silent timers arm at a value in _SILENT_BUDGETS, so the two are
    distinguishable by interval.
    """
    started = []
    created = []

    class _RecTimer:
        def __init__(self, interval, fn):
            self.interval = interval
            self.fn = fn
            self.cancelled = False
            created.append(self)

        def start(self):
            started.append(self.interval)

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(runtime.threading, "Timer", _RecTimer)
    return started, created


def _silent_arms(started):
    return [i for i in started if i in _SILENT_BUDGETS]


_TOOL_START = '{"type": "tool_execution_start", "toolName": "bash"}\n'
_TOOL_END = '{"type": "tool_execution_end", "toolName": "bash"}\n'
_TEXT = ('{"type": "message_update", "assistantMessageEvent": '
         '{"type": "text_delta", "delta": "working"}}\n')
_TURN_END = ('{"type": "turn_end", "message": {"role": "assistant", '
             '"stopReason": "stop", "content": [{"type": "text", "text": "done"}]}}\n')


def test_loop_raises_budget_while_tool_active_and_drops_after_end(tmp_path, monkeypatch):
    started, _ = _patch_recording_timer(monkeypatch)
    runner = _runner(tmp_path)
    runner._run_streaming(
        _FakeProc([_TOOL_START, _TEXT, _TOOL_END, _TURN_END]),
        session_id="sid", tag=None, on_output=lambda _t: None)

    arms = _silent_arms(started)
    # raised to the tool-active window while the tool was in flight
    assert TOOL_ACTIVE_SILENT_TIMEOUT in arms
    # dropped back to base by the time the tool ended / turn closed
    assert arms[-1] == SILENT_TIMEOUT
    last_active = len(arms) - 1 - arms[::-1].index(TOOL_ACTIVE_SILENT_TIMEOUT)
    assert SILENT_TIMEOUT in arms[last_active + 1:]


def test_loop_flag_off_keeps_base_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_TOOL_ACTIVE_BUDGET_ENABLED", "0")
    started, _ = _patch_recording_timer(monkeypatch)
    runner = _runner(tmp_path)
    runner._run_streaming(
        _FakeProc([_TOOL_START, _TEXT, _TOOL_END, _TURN_END]),
        session_id="sid", tag=None, on_output=lambda _t: None)

    arms = _silent_arms(started)
    assert TOOL_ACTIVE_SILENT_TIMEOUT not in arms
    assert set(arms) == {SILENT_TIMEOUT}


def test_loop_todo_resets_silent_timer_without_callback(tmp_path, monkeypatch):
    """Todo progress is liveness even when no on_todo_update callback is wired."""
    started, _ = _patch_recording_timer(monkeypatch)

    class _TodoRunner(PiRunner):
        def parse_streaming_line(self, event, state):
            if event.get("type") == "__todo__":
                state.pending_todo_update = [{"content": "x", "status": "pending"}]
                return
            super().parse_streaming_line(event, state)

    runner = _TodoRunner(
        command="pi", model="m", workspace=str(tmp_path),
        timeout=30, safety_prompt_mode="off")
    # No on_todo_update passed → with the fix, the todo event still re-arms silent.
    runner._run_streaming(
        _FakeProc(['{"type": "__todo__"}\n', _TURN_END]),
        session_id="sid", tag=None, on_output=lambda _t: None)

    # init arm + at least one reset from the callback-less todo event
    assert len(_silent_arms(started)) >= 2


def test_loop_silent_timeout_reports_tool_was_active(tmp_path, monkeypatch):
    _, created = _patch_recording_timer(monkeypatch)
    monkeypatch.setattr(runtime.BaseRunner, "_kill_proc_tree",
                        staticmethod(lambda _proc: None))

    def _lines():
        yield _TOOL_START
        # The loop has now armed the tool-active silent timer; fire it manually.
        for t in reversed(created):
            if t.interval in _SILENT_BUDGETS and not t.cancelled:
                t.fn()
                break
        return

    runner = _runner(tmp_path)
    result = runner._run_streaming(
        _FakeProc(_lines()), session_id="sid", tag=None,
        on_output=lambda _t: None)

    assert result["silent_timeout"] is True
    assert result["tool_was_active"] is True
