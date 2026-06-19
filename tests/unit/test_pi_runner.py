#!/usr/bin/env python3
"""Unit tests for the Pi runner integration."""

import json
import logging

from feishu_bridge import main as bridge
from feishu_bridge.runtime import StreamState
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


def test_pi_build_args_defaults_to_json_no_tool_injection_and_session(tmp_path):
    runner = _runner(
        tmp_path,
        extra_cli_args=["--provider", "omlx"],
        extra_system_prompts=["bridge rules"],
    )

    args = runner.build_args("hello", "sid/with spaces", False, True)

    assert args[:3] == ["pi", "--mode", "json"]
    assert args[3:5] == ["--provider", "omlx"]
    assert ["--model", "Qwen3.6-35B-A3B-mxfp4"] == args[5:7]
    # Bridge is a pure conduit: it must NOT inject a tool allowlist. Pi uses
    # its native toolset unless the operator scopes it via config args.
    assert "--tools" not in args
    assert "--append-system-prompt" in args
    assert "bridge rules" in args
    assert "--session" in args
    session_path = args[args.index("--session") + 1]
    assert session_path.endswith(
        "state/feishu-bridge/pi-sessions/sid_with_spaces.jsonl"
    )
    assert args[-2:] == ["-p", "hello"]


def test_pi_build_args_respects_tool_and_session_overrides(tmp_path):
    runner = _runner(tmp_path, extra_cli_args=["--no-tools", "--no-session"])

    args = runner.build_args("hello", None, False, True)

    assert "--tools" not in args
    assert args.count("--no-session") == 1

    # Operator scopes pi's tools via config args_by_type — passes through
    # verbatim, bridge adds nothing on top.
    runner = _runner(tmp_path, extra_cli_args=["--tools", "read,write,bash,edit"])
    args = runner.build_args("hello", "sid", False, True)
    assert args.count("--tools") == 1
    assert "read,write,bash,edit" in args

    runner = _runner(
        tmp_path,
        extra_cli_args=["--session", str(tmp_path / "custom.jsonl")],
    )
    args = runner.build_args("hello", "bridge-sid", False, True)
    assert args.count("--session") == 1
    assert str(tmp_path / "custom.jsonl") in args


def test_pi_build_args_accepts_fresh_context(tmp_path):
    # Regression: BaseRunner.run() passes fresh_context= to build_args; PiRunner
    # must accept it and fold it into the appended system prompt (same channel as
    # ClaudeRunner/OmpRunner), not raise TypeError.
    runner = _runner(tmp_path, extra_system_prompts=["bridge rules"])

    args = runner.build_args(
        "hello", "sid", False, True,
        fresh_context="REMEMBER: fresh memory note",
    )

    sp = args[args.index("--append-system-prompt") + 1]
    assert "REMEMBER: fresh memory note" in sp
    assert "bridge rules" in sp


def test_pi_display_default_model_prefers_pinned(tmp_path):
    runner = _runner(tmp_path, model="claude-sonnet-4-6")
    assert runner.display_default_model() == "claude-sonnet-4-6"


def test_pi_display_default_model_reads_pi_settings(tmp_path, monkeypatch):
    # No pinned model → surface ~/.pi/agent/settings.json defaultModel instead
    # of "(cli-default)" (mirrors OmpRunner reading ~/.omp/agent/config.yml).
    home = tmp_path / "home"
    (home / ".pi" / "agent").mkdir(parents=True)
    (home / ".pi" / "agent" / "settings.json").write_text(
        '{"defaultProvider": "anthropic", "defaultModel": "claude-opus-4-6"}')
    monkeypatch.setattr("feishu_bridge.runtime_pi.Path.home", lambda: home)
    runner = _runner(tmp_path, model=None)
    assert runner.display_default_model() == "claude-opus-4-6"


def test_pi_display_default_model_none_when_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr("feishu_bridge.runtime_pi.Path.home",
                        lambda: tmp_path / "nonexistent")
    runner = _runner(tmp_path, model=None)
    assert runner.display_default_model() is None


def test_pi_modelusage_key_uses_resolved_default(tmp_path, monkeypatch):
    # The card-footer model name comes from the modelUsage key; with no pinned
    # model it must be the resolved pi default, not "(cli-default)".
    home = tmp_path / "home"
    (home / ".pi" / "agent").mkdir(parents=True)
    (home / ".pi" / "agent" / "settings.json").write_text(
        '{"defaultModel": "claude-opus-4-6"}')
    monkeypatch.setattr("feishu_bridge.runtime_pi.Path.home", lambda: home)
    runner = _runner(tmp_path, model=None)
    state = StreamState(session_id="sid")
    state.accumulated_text = "hello"
    state.done = True
    result = runner._build_streaming_result(state, "sid")
    assert list(result["modelUsage"].keys()) == ["claude-opus-4-6"]


def test_pi_parse_text_delta_and_final_usage(tmp_path):
    runner = _runner(tmp_path)
    state = StreamState(session_id="bridge-sid")

    runner.parse_streaming_line({
        "type": "message_update",
        "assistantMessageEvent": {"type": "text_delta", "delta": "PI_"},
    }, state)
    runner.parse_streaming_line({
        "type": "message_update",
        "assistantMessageEvent": {"type": "text_delta", "delta": "OK"},
    }, state)
    # message_end (authoritative termination) triggers cumulative accumulation
    runner.parse_streaming_line({
        "type": "message_end",
        "message": {
            "stopReason": "stop",
            "content": [{"type": "text", "text": "PI_OK"}],
            "usage": {
                "input": 369,
                "output": 3,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 372,
            },
        },
    }, state)
    runner.parse_streaming_line({
        "type": "turn_end",
        "message": {
            "stopReason": "stop",
            "content": [{"type": "text", "text": "PI_OK"}],
            "usage": {
                "input": 369,
                "output": 3,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 372,
            },
        },
    }, state)

    result = runner._build_streaming_result(state, "bridge-sid")

    assert state.done is True
    assert result == {
        "result": "PI_OK",
        "session_id": "bridge-sid",
        "is_error": False,
        "usage": {
            "input_tokens": 369,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 3,
            "cost_total": 0.0,
        },
        "last_call_usage": {
            "input_tokens": 369,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 3,
            "cost_total": 0.0,
        },
        "modelUsage": {
            "Qwen3.6-35B-A3B-mxfp4": {
                "contextWindow": 0,
                "inputTokens": 369,
                "outputTokens": 3,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
            },
        },
        "peak_context_tokens": 369,
        "compact_detected": False,
        "pi_footer_data": {
            "cumulative_input": 369,
            "cumulative_output": 3,
            "cumulative_cache_read": 0,
            "cumulative_cache_write": 0,
            "cumulative_cost_total": 0.0,
            "latest_cache_hit_rate": 0.0,
            "context_window": 0,
            "context_tokens": 369,
            "is_oauth": False,
            "model": None,
        },
    }


def test_pi_result_exposes_configured_model_for_footer(tmp_path):
    runner = _runner(tmp_path, model="custom-pi-model")
    state = StreamState(session_id="bridge-sid")

    runner.parse_streaming_line({
        "type": "turn_end",
        "message": {
            "stopReason": "stop",
            "content": [{"type": "text", "text": "OK"}],
            "usage": {"input": 10, "output": 2},
        },
    }, state)

    result = runner._build_streaming_result(state, "bridge-sid")

    assert result["modelUsage"] == {
        "custom-pi-model": {
            "contextWindow": 0,
            "inputTokens": 10,
            "outputTokens": 2,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0,
        },
    }


def test_pi_result_uses_actual_jsonl_model_for_footer(tmp_path):
    runner = _runner(tmp_path, model="configured-before-fallback")
    state = StreamState(session_id="bridge-sid")

    runner.parse_streaming_line({
        "type": "turn_end",
        "message": {
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "stopReason": "stop",
            "content": [{"type": "text", "text": "OK"}],
            "usage": {"input": 10, "output": 2},
        },
    }, state)

    result = runner._build_streaming_result(state, "bridge-sid")

    assert list(result["modelUsage"].keys()) == ["deepseek/deepseek-v4-pro"]


def test_pi_parse_provider_error(tmp_path):
    runner = _runner(tmp_path)
    state = StreamState(session_id="bridge-sid")

    runner.parse_streaming_line({
        "type": "turn_end",
        "message": {
            "stopReason": "error",
            "errorMessage": "404 Model not found",
            "usage": {"input": 0, "output": 0},
        },
    }, state)

    result = runner._build_streaming_result(state, "bridge-sid")

    assert state.done is True
    assert result["is_error"] is True
    assert result["result"] == "Pi 模型不可用或不存在：404 Model not found"
    assert result["session_id"] == "bridge-sid"


def test_pi_error_formatting_classes(tmp_path):
    runner = _runner(tmp_path)

    cases = [
        ("401 Invalid API key", "Pi provider 鉴权失败：401 Invalid API key"),
        (
            "connect ECONNREFUSED 127.0.0.1:8000",
            "Pi provider 不可用：connect ECONNREFUSED 127.0.0.1:8000",
        ),
        ("tool not allowed: bash", "Pi 工具调用被拒绝：tool not allowed: bash"),
        ("invalid JSON protocol frame", "Pi 协议错误：invalid JSON protocol frame"),
    ]

    for raw, expected in cases:
        state = StreamState(session_id="bridge-sid")
        runner.parse_streaming_line({
            "type": "turn_end",
            "message": {
                "stopReason": "error",
                "errorMessage": raw,
                "usage": {"input": 0, "output": 0},
            },
        }, state)

        result = runner._build_streaming_result(state, "bridge-sid")

        assert result["is_error"] is True
        assert result["result"] == expected


def test_pi_parse_protocol_error_event(tmp_path):
    runner = _runner(tmp_path)
    state = StreamState(session_id="bridge-sid")

    runner.parse_streaming_line({
        "type": "error",
        "message": "invalid JSON protocol frame",
    }, state)

    result = runner._build_streaming_result(state, "bridge-sid")

    assert state.done is True
    assert result["is_error"] is True
    assert result["result"] == "Pi 协议错误：invalid JSON protocol frame"


def _toolcall_event(utype, *, call_id=None, name="read", arguments=None):
    tc = {"type": "toolCall", "name": name}
    if call_id is not None:
        tc["id"] = call_id
    if arguments is not None:
        tc["arguments"] = arguments
    return {
        "type": "message_update",
        "assistantMessageEvent": {
            "type": utype,
            "partial": {"content": [tc]},
        },
    }


def test_normalize_pi_tool():
    assert PiRunner._normalize_pi_tool("read") == "Read"
    assert PiRunner._normalize_pi_tool("bash") == "Bash"
    assert PiRunner._normalize_pi_tool("ls") == "Ls"
    assert PiRunner._normalize_pi_tool("grep") == "Grep"
    # Unknown pi tool → .title() fallback (still renders, no crash)
    assert PiRunner._normalize_pi_tool("inspect") == "Inspect"
    assert PiRunner._normalize_pi_tool("") == ""


def test_normalize_pi_tool_web_search():
    assert PiRunner._normalize_pi_tool("web_search") == "WebSearch"


def test_normalize_pi_tool_web_fetch():
    assert PiRunner._normalize_pi_tool("web_fetch") == "WebFetch"


def test_normalize_pi_tool_get_subagent_result():
    assert PiRunner._normalize_pi_tool("get_subagent_result") == "GetSubagentResult"


def test_tool_status_single_emit_per_id(tmp_path):
    """toolcall_* is authoritative; tool_execution_* is a no-op; one entry."""
    runner = _runner(tmp_path)
    state = StreamState()

    # Same call id arrives via start (no args yet), tool_execution noise, then
    # end (with args). Must surface exactly one normalized dict.
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", call_id="toolu_1", name="read"), state)
    runner.parse_streaming_line(
        {"type": "tool_execution_start", "toolName": "read"}, state)
    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="toolu_1", name="read",
                        arguments={"path": "/a/README.md"}), state)
    runner.parse_streaming_line(
        {"type": "tool_execution_end", "toolName": "read",
         "result": {"isError": True}}, state)

    assert state.pending_tool_status == [
        {"name": "Read", "hint_data": "/a/README.md", "id": "toolu_1"}]
    assert state.is_error is False
    assert state.done is False


def test_tool_execution_only_stream_no_status(tmp_path):
    """Degenerate stream with only tool_execution_* (id-less) → no status."""
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line(
        {"type": "tool_execution_start", "toolName": "read"}, state)
    runner.parse_streaming_line(
        {"type": "tool_execution_end", "toolName": "read"}, state)
    assert state.pending_tool_status == []


def test_emit_deferred_until_args(tmp_path):
    """start with empty args emits nothing; emit once when args arrive."""
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", call_id="toolu_9", name="bash"), state)
    assert state.pending_tool_status == []
    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="toolu_9", name="bash",
                        arguments={"command": "ls -1"}), state)
    assert state.pending_tool_status == [{"name": "Bash", "hint_data": "ls", "id": "toolu_9"}]


def test_two_blank_starts_no_miscorrelation(tmp_path):
    """Two same-name calls with distinct ids each get their own correct hint."""
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", call_id="a", name="read"), state)
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", call_id="b", name="read"), state)
    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="b", name="read",
                        arguments={"path": "/x/b.py"}), state)
    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="a", name="read",
                        arguments={"path": "/x/a.py"}), state)
    assert state.pending_tool_status == [
        {"name": "Read", "hint_data": "/x/b.py", "id": "b"},
        {"name": "Read", "hint_data": "/x/a.py", "id": "a"},
    ]


def test_extract_hint_ls_find():
    from feishu_bridge.runtime import _extract_hint_data
    assert _extract_hint_data("Ls", {"path": "/a/b"}) == "/a/b"
    assert _extract_hint_data("Find", {"path": "/a", "pattern": "*.py"}) == "*.py"
    assert _extract_hint_data("Find", {"path": "/a"}) == "/a"


def test_extract_hint_subagent():
    from feishu_bridge.runtime import _extract_hint_data
    assert _extract_hint_data("Subagent", {"agent": "scout", "task": "分析代码"}) == "scout: 分析代码"
    assert _extract_hint_data("Subagent", {"agent": "scout"}) == "scout"
    assert _extract_hint_data("Subagent", {"task": "分析代码"}) == "分析代码"
    assert _extract_hint_data("Subagent", {}) == ""


def test_extract_hint_get_subagent_result():
    from feishu_bridge.runtime import _extract_hint_data
    result = _extract_hint_data("GetSubagentResult", {"subagentId": "subagent-abc-def-ghi-jkl"})
    assert result == "subagent-abc-def-ghi-jkl"
    assert len(result) <= 60
    # subagent_id alias
    assert _extract_hint_data("GetSubagentResult", {"subagent_id": "sb-123"}) == "sb-123"
    # empty
    assert _extract_hint_data("GetSubagentResult", {}) == ""


def test_extract_hint_subagent_tasks_multi():
    """Multi-task parallel Subagent dispatch (tasks=[]) → joined hint."""
    from feishu_bridge.runtime import _extract_hint_data
    assert _extract_hint_data("Subagent", {
        "tasks": [
            {"agent": "scout", "task": "分析认证流程"},
            {"agent": "scout", "task": "分析路由结构"},
        ],
    }) == "scout: 分析认证流程, scout: 分析路由结构"

    # Truncation at 60 chars (each task[:40], joined, then overall[:60])
    long_task = "分析 1234567890123456789012345678901234567890"
    result = _extract_hint_data("Subagent", {
        "tasks": [
            {"agent": "scout", "task": long_task},
            {"agent": "developer", "task": "实现功能X"},
        ],
    })
    assert len(result) <= 60
    assert result.startswith("scout: 分析 ")

    # Fallback: empty tasks list → original single-path logic
    assert _extract_hint_data("Subagent", {
        "tasks": [],
        "agent": "scout",
    }) == "scout"

    # Fallback: tasks contains non-dict entries (ignored)
    assert _extract_hint_data("Subagent", {
        "tasks": ["bad entry", {"agent": "scout", "task": "分析"}],
    }) == "scout: 分析"

    # Only agent in task (no task field)
    assert _extract_hint_data("Subagent", {
        "tasks": [{"agent": "scout"}],
    }) == "scout"


def test_format_tool_hint_ls():
    """Ls renders the basename of its directory path, like Read/Write/Edit."""
    from feishu_bridge.ui import ResponseHandle
    assert ResponseHandle._format_tool_hint("Ls", "/a/b/c") == "c"
    assert ResponseHandle._format_tool_hint("Read", "/a/b/README.md") == "README.md"




def test_tool_status_malformed_event_no_raise(tmp_path):
    """Malformed events must not raise; end without usable args and no
    prior start → only pending_tool_end_ids (unmatched warning at drain)."""
    runner = _runner(tmp_path)
    state = StreamState()
    # arguments not a dict, arriving on END (call resolving) → no bare entry
    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="m1", name="read",
                        arguments="oops"), state)
    # missing name → cannot label → skipped (no crash)
    runner.parse_streaming_line({
        "type": "message_update",
        "assistantMessageEvent": {
            "type": "toolcall_end",
            "partial": {"content": [{"type": "toolCall", "id": "m2"}]},
        },
    }, state)
    assert state.pending_tool_status == []
    assert state.pending_tool_end_ids == ["m1"]


def test_create_runner_pi_builds_pi_runner(tmp_path):
    agent_cfg = {
        "type": "pi",
        "_resolved_command": "pi",
        "timeout_seconds": 30,
        "providers": {
            "default": {
                "args_by_type": {"pi": ["--provider", "omlx"]},
                "models": {"pi": "Qwen3.6-35B-A3B-mxfp4"},
                "model_aliases": {
                    "pi": "Qwen3.6-35B-A3B-mxfp4",
                    "qwen": "Qwen3.6-35B-A3B-mxfp4",
                    "gemma": "gemma-4-26b-a4b-it-mxfp4",
                },
            },
        },
        "prompt": {"safety": "minimal", "feishu_cli": False, "cron_mgr": False},
    }
    bot_cfg = {"workspace": str(tmp_path), "model": "fallback-model"}

    runner = bridge.create_runner(agent_cfg, bot_cfg, ["extra"])

    assert isinstance(runner, PiRunner)
    assert runner.model == "Qwen3.6-35B-A3B-mxfp4"
    args = runner.build_args("hello", "sid", False, True)
    assert args[:5] == ["pi", "--mode", "json", "--provider", "omlx"]
    assert "--append-system-prompt" in args
    assert runner.wants_auth_file() is False
    assert runner.supports_compact() is False
    # Aliases now live on the Bot, not the Runner
    assert bridge.resolve_model_aliases(agent_cfg) == {
        "pi": "Qwen3.6-35B-A3B-mxfp4",
        "qwen": "Qwen3.6-35B-A3B-mxfp4",
        "gemma": "gemma-4-26b-a4b-it-mxfp4",
    }


def test_load_config_pi_type_resolves_command(monkeypatch, tmp_path):
    config = {
        "bots": [{
            "name": "test",
            "app_id": "cli_a",
            "app_secret": "secret",
            "encrypt_key": "encrypt",
            "verification_token": "verify",
            "workspace": str(tmp_path),
            "allowed_users": ["u1"],
        }],
        "agent": {"type": "pi"},
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(config))
    monkeypatch.setattr(
        bridge,
        "resolve_effective_agent_command",
        lambda agent_cfg, agent_type: ("pi", "pi"),
    )

    result = bridge.load_config(str(cfg_file), "test")

    assert result["agent"]["type"] == "pi"
    assert result["agent"]["_resolved_command"] == "pi"


# ---- Subagent tool status ----

def test_subagent_normal_extract_to_agent_launches(tmp_path):
    """subagent with agent+task → pending_agent_launches, not tool_status."""
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", call_id="sub_1", name="subagent",
                        arguments={"agent": "scout", "task": "分析代码"}), state)
    assert state.pending_tool_status == []
    assert state.pending_agent_launches == [
        {"description": "分析代码", "name": None, "subagent_type": "scout"},
    ]
    assert "sub_1" in state._tool_seen_starts


def test_subagent_missing_agent_logs_warning_and_skips(tmp_path, caplog):
    """subagent without agent → logs warning, no tool_status or agent_launches."""
    runner = _runner(tmp_path)
    state = StreamState()
    with caplog.at_level(logging.WARNING):
        runner.parse_streaming_line(
            _toolcall_event("toolcall_start", call_id="sub_2", name="subagent",
                            arguments={"task": "分析代码"}), state)
    assert state.pending_agent_launches is None
    assert state.pending_tool_status == []
    assert "sub_2" in state._tool_seen_starts
    assert "Subagent toolcall with unrecognized args shape" in caplog.text


def test_subagent_missing_task_logs_warning_and_skips(tmp_path, caplog):
    """subagent without task → logs warning, no tool_status or agent_launches."""
    runner = _runner(tmp_path)
    state = StreamState()
    with caplog.at_level(logging.WARNING):
        runner.parse_streaming_line(
            _toolcall_event("toolcall_start", call_id="sub_3", name="subagent",
                            arguments={"agent": "scout"}), state)
    assert state.pending_agent_launches is None
    assert state.pending_tool_status == []
    assert "sub_3" in state._tool_seen_starts
    assert "Subagent toolcall with unrecognized args shape" in caplog.text


def test_subagent_missing_both_agent_and_task_logs_warning(tmp_path, caplog):
    """subagent with unrecognized args → logs warning, no tool_status or agent_launches."""
    runner = _runner(tmp_path)
    state = StreamState()
    with caplog.at_level(logging.WARNING):
        runner.parse_streaming_line(
            _toolcall_event("toolcall_start", call_id="sub_empty", name="subagent",
                            arguments={"unknown": "x"}), state)
    assert state.pending_agent_launches is None
    assert state.pending_tool_status == []
    assert "sub_empty" in state._tool_seen_starts
    assert "Subagent toolcall with unrecognized args shape" in caplog.text


def test_subagent_deferred_start_to_end(tmp_path):
    """start(no args) defers; end returns early → no agent_launch (F-1)."""
    runner = _runner(tmp_path)
    state = StreamState()
    # start with no args → deferred
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", call_id="sub_4", name="subagent"), state)
    assert state.pending_tool_status == []
    assert state.pending_agent_launches is None
    # end with args → skipped (Subagent end early-return)
    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="sub_4", name="subagent",
                        arguments={"agent": "developer", "task": "实现功能"}), state)
    assert state.pending_agent_launches is None
    assert state.pending_tool_status == []


def test_subagent_idless_start_only_no_duplicate(tmp_path):
    """id-less subagent: only start is processed (end is dropped)."""
    runner = _runner(tmp_path)
    state = StreamState()
    # id-less start with args → processes
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", name="subagent",
                        arguments={"agent": "git-ops", "task": "提交代码"}), state)
    assert state.pending_agent_launches == [
        {"description": "提交代码", "name": None, "subagent_type": "git-ops"},
    ]
    # id-less end → dropped (existing logic)
    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", name="subagent",
                        arguments={"agent": "git-ops", "task": "提交代码"}), state)
    # Still only one launch
    assert len(state.pending_agent_launches) == 1


def test_subagent_id_dedup(tmp_path):
    """Same call_id should not produce duplicate launches (dedup via _tool_seen_starts)."""
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", call_id="sub_5", name="subagent",
                        arguments={"agent": "scout", "task": "分析"}), state)
    # Replay same id
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", call_id="sub_5", name="subagent",
                        arguments={"agent": "scout", "task": "分析"}), state)
    assert len(state.pending_agent_launches) == 1


def test_normalize_pi_tool_subagent():
    assert PiRunner._normalize_pi_tool("subagent") == "Subagent"


# ---- Subagent tasks[] multi-dispatch ----

def test_subagent_tasks_multi_dispatches_agent_launches(tmp_path):
    """tasks=[{agent, task}, ...] → multiple pending_agent_launches."""
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", call_id="sub_m1", name="subagent",
                        arguments={
                            "tasks": [
                                {"agent": "scout", "task": "分析认证流程"},
                                {"agent": "scout", "task": "分析路由结构"},
                            ],
                        }), state)
    assert state.pending_tool_status == []
    assert state.pending_agent_launches == [
        {"description": "分析认证流程", "name": None, "subagent_type": "scout"},
        {"description": "分析路由结构", "name": None, "subagent_type": "scout"},
    ]
    assert "sub_m1" in state._tool_seen_starts


def test_subagent_tasks_multi_ignores_invalid_entries(tmp_path):
    """tasks[] with non-dict entries → only valid entries become launches."""
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", call_id="sub_m2", name="subagent",
                        arguments={
                            "tasks": [
                                "not a dict",
                                {"agent": "scout", "task": "分析"},
                                {"agent": "developer"},
                            ],
                        }), state)
    assert state.pending_agent_launches == [
        {"description": "分析", "name": None, "subagent_type": "scout"},
        {"description": "developer", "name": None, "subagent_type": "developer"},
    ]


def test_subagent_tasks_empty_falls_back_to_single(tmp_path):
    """Empty tasks[] → fallback to single agent+task path."""
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", call_id="sub_m3", name="subagent",
                        arguments={
                            "tasks": [],
                            "agent": "scout",
                            "task": "分析",
                        }), state)
    assert state.pending_agent_launches == [
        {"description": "分析", "name": None, "subagent_type": "scout"},
    ]
    assert state.pending_tool_status == []


def test_subagent_tasks_multi_deferred_start_to_end(tmp_path):
    """tasks[] args on end after deferred start → end is skipped (F-1)."""
    runner = _runner(tmp_path)
    state = StreamState()
    # start with no args → deferred
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", call_id="sub_m4", name="subagent"), state)
    assert state.pending_agent_launches is None
    # end with tasks[] → skipped (Subagent end early-return)
    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="sub_m4", name="subagent",
                        arguments={
                            "tasks": [
                                {"agent": "developer", "task": "实现A"},
                                {"agent": "git-ops", "task": "提交"},
                            ],
                        }), state)
    assert state.pending_agent_launches is None
    assert state.pending_tool_status == []


# ---- Fix C: Subagent empty-args / non-dict-args paths ----

def test_subagent_empty_args_on_end_skipped(tmp_path, caplog):
    """Subagent args={} on end event → silently skipped (F-1 early return)."""
    runner = _runner(tmp_path)
    state = StreamState()
    with caplog.at_level(logging.WARNING):
        runner.parse_streaming_line(
            _toolcall_event("toolcall_end", call_id="sub_empty_end",
                            name="subagent", arguments={}), state)
    assert state.pending_tool_status == []
    assert state.pending_agent_launches is None
    # End is skipped before reaching the warning path.
    assert "Subagent toolcall with unrecognized args shape" not in caplog.text


def test_subagent_non_dict_args_end_skipped(tmp_path, caplog):
    """Subagent arguments=None on end event → silently skipped (F-1 early return)."""
    runner = _runner(tmp_path)
    state = StreamState()
    with caplog.at_level(logging.WARNING):
        runner.parse_streaming_line(
            _toolcall_event("toolcall_end", call_id="sub_none",
                            name="subagent", arguments=None), state)
    assert state.pending_tool_status == []
    assert state.pending_agent_launches is None
    # End is skipped before reaching the warning path.
    assert "Subagent toolcall with unrecognized args shape" not in caplog.text


def test_subagent_empty_args_on_start_defers(tmp_path, caplog):
    """Subagent args={} on start → defer (no warning, no push, no seen_starts).

    With F-1, end events are skipped so the deferred call is silently dropped.
    """
    runner = _runner(tmp_path)
    state = StreamState()
    with caplog.at_level(logging.WARNING):
        runner.parse_streaming_line(
            _toolcall_event("toolcall_start", call_id="sub_defer",
                            name="subagent", arguments={}), state)
    assert state.pending_tool_status == []
    assert state.pending_agent_launches is None
    assert "sub_defer" not in state._tool_seen_starts
    # No warning logged for deferred start
    assert "Subagent toolcall with unrecognized args shape" not in caplog.text


# ---- F-1: Split dedup + pending_tool_end_ids ----

def test_seen_starts_seen_ends_split(tmp_path):
    """start id=A twice → 2nd dedup; end id=A twice → 2nd dedup;
    start id=A + end id=A → each side independent (no cross-dedup)."""
    runner = _runner(tmp_path)
    state = StreamState()

    # start id=A twice → second dedup'd
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", call_id="A", name="read",
                        arguments={"path": "/a.py"}), state)
    assert len(state.pending_tool_status) == 1
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", call_id="A", name="read",
                        arguments={"path": "/a.py"}), state)
    assert len(state.pending_tool_status) == 1  # dedup'd
    assert "A" in state._tool_seen_starts

    # end id=A twice → second dedup'd; end after start does not
    # re-emit pending_tool_status (case A fix).
    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="A", name="read",
                        arguments={"path": "/a.py"}), state)
    assert len(state.pending_tool_status) == 1  # end does not re-emit status
    assert "A" in state._tool_seen_ends
    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="A", name="read",
                        arguments={"path": "/a.py"}), state)
    assert len(state.pending_tool_status) == 1  # 2nd end dedup'd

    # pending_tool_end_ids has "A" exactly once (from the first end)
    assert state.pending_tool_end_ids == ["A"]


def test_toolcall_end_adds_to_pending_tool_end_ids(tmp_path):
    """toolcall_end update (no prior start, deferred compensation) →
    both pending_tool_status (one entry with id) and
    pending_tool_end_ids have call_id."""
    runner = _runner(tmp_path)
    state = StreamState()

    status_before = len(state.pending_tool_status)
    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="tid_1", name="read",
                        arguments={"path": "/x.py"}), state)
    # Tool status entry includes the id now (for tool_call_ids tracking)
    assert len(state.pending_tool_status) == status_before + 1
    assert state.pending_tool_status[-1]["id"] == "tid_1"
    # End id appended to pending_tool_end_ids
    assert state.pending_tool_end_ids == ["tid_1"]


def test_toolcall_end_after_start_does_not_append_status_again(tmp_path):
    """start(with args, id=A) → end(with same id) must NOT append a
    second pending_tool_status entry (case A fix)."""
    runner = _runner(tmp_path)
    state = StreamState()

    # start with args
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", call_id="A", name="read",
                        arguments={"path": "/a.py"}), state)
    assert len(state.pending_tool_status) == 1
    assert state.pending_tool_end_ids == []
    assert "A" in state._tool_seen_starts

    # end with same id → MUST NOT increase pending_tool_status
    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="A", name="read",
                        arguments={"path": "/a.py"}), state)
    assert len(state.pending_tool_status) == 1  # unchanged
    assert state.pending_tool_end_ids == ["A"]
    assert "A" in state._tool_seen_ends


def test_toolcall_end_after_deferred_start_emits_status_then_end(tmp_path):
    """start(no args, id=A) deferred → end(with args, id=A) compensates
    by emitting both pending_tool_status and pending_tool_end_ids."""
    runner = _runner(tmp_path)
    state = StreamState()

    # start with no args → deferred
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", call_id="A", name="read"), state)
    assert state.pending_tool_status == []
    assert "A" not in state._tool_seen_starts

    # end with args → status emitted as compensation + end_id
    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="A", name="read",
                        arguments={"path": "/a.py"}), state)
    assert len(state.pending_tool_status) == 1
    assert state.pending_tool_status[0]["id"] == "A"
    assert state.pending_tool_end_ids == ["A"]
    assert "A" in state._tool_seen_starts  # compensated
    assert "A" in state._tool_seen_ends


def test_toolcall_end_without_args_no_status(tmp_path):
    """end(no args, id=A) with no prior start → only pending_tool_end_ids,
    no bare pending_tool_status entry (unmatched warning at drain)."""
    runner = _runner(tmp_path)
    state = StreamState()

    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="A", name="read"), state)
    assert state.pending_tool_status == []
    assert state.pending_tool_end_ids == ["A"]
    assert "A" in state._tool_seen_ends


def test_subagent_end_not_in_pending_tool_end_ids(tmp_path):
    """Subagent end events are skipped entirely → no pending_tool_end_ids."""
    runner = _runner(tmp_path)
    state = StreamState()

    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="sub_end", name="subagent",
                        arguments={"agent": "scout", "task": "分析"}), state)
    assert state.pending_tool_end_ids == []
    assert state.pending_agent_launches is None


# ── tool_execution_start / tool_execution_end → pending_tool_status ──
# pi-feishu parity: forward execution args/result for rich tool-card display.


def test_tool_execution_start_emits_exec_args(tmp_path):
    """tool_execution_start with toolCallId+args → _exec_args in pending."""
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line({
        "type": "tool_execution_start",
        "toolCallId": "call_exec_01",
        "toolName": "bash",
        "args": {"command": "echo hi"},
    }, state)
    assert len(state.pending_tool_status) == 1
    entry = state.pending_tool_status[0]
    assert entry["id"] == "call_exec_01"
    assert entry["name"] == "Bash"
    assert entry["_exec_args"] == {"command": "echo hi"}
    assert state.tool_active_count == 1
    assert state.pending_silent_reset is True


def test_tool_execution_start_without_id_no_emit(tmp_path):
    """tool_execution_start without callId is a no-op for UI (liveness only)."""
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line({
        "type": "tool_execution_start",
        "toolName": "bash",
        "args": {"command": "x"},
    }, state)
    assert state.pending_tool_status == []
    assert state.tool_active_count == 1


def test_tool_execution_start_empty_args_no_emit(tmp_path):
    """tool_execution_start without args dict is a no-op for UI."""
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line({
        "type": "tool_execution_start",
        "toolCallId": "call_exec_02",
        "toolName": "read",
    }, state)
    assert state.pending_tool_status == []


def test_tool_execution_end_emits_exec_result(tmp_path):
    """tool_execution_end with toolCallId+result → _exec_result in pending."""
    runner = _runner(tmp_path)
    state = StreamState()
    result = {"content": [{"type": "text", "text": "hello world"}]}
    runner.parse_streaming_line({
        "type": "tool_execution_end",
        "toolCallId": "call_exec_03",
        "toolName": "bash",
        "result": result,
        "isError": False,
    }, state)
    assert len(state.pending_tool_status) == 1
    entry = state.pending_tool_status[0]
    assert entry["id"] == "call_exec_03"
    assert entry["name"] == "Bash"
    assert entry["_exec_result"] is result
    assert entry["_is_error"] is False
    assert state.tool_active_count == 0


def test_tool_execution_end_with_error(tmp_path):
    """tool_execution_end with isError=True propagates _is_error flag."""
    runner = _runner(tmp_path)
    state = StreamState()
    state.tool_active_count = 1  # pre-condition
    runner.parse_streaming_line({
        "type": "tool_execution_end",
        "toolCallId": "call_exec_err",
        "toolName": "bash",
        "result": {"content": [{"type": "text", "text": "permission denied"}]},
        "isError": True,
    }, state)
    assert state.pending_tool_status[0]["_is_error"] is True
    assert state.tool_active_count == 0


def test_tool_execution_end_without_id_no_emit(tmp_path):
    """tool_execution_end without callId is a no-op for UI."""
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line({
        "type": "tool_execution_end",
        "toolName": "bash",
        "result": {},
    }, state)
    assert state.pending_tool_status == []


def test_tool_call_from_update_uses_content_index(tmp_path):
    """Multi-tool turn: contentIndex picks the correct toolCall, not the first."""
    runner = _runner(tmp_path)
    # Simulate a partial with 2 toolCalls — start for the second tool.
    update = {
        "type": "toolcall_start",
        "contentIndex": 1,
        "partial": {
            "content": [
                {"type": "toolCall", "id": "first", "name": "read",
                 "arguments": {"path": "/a"}},
                {"type": "toolCall", "id": "second", "name": "bash",
                 "arguments": {"command": "ls"}},
            ]
        }
    }
    tc = PiRunner._tool_call_from_update(update)
    # Must return the SECOND tool (contentIndex=1), not the first.
    assert tc["id"] == "second"
    assert tc["name"] == "bash"


def test_tool_call_from_update_falls_back_to_scan(tmp_path):
    """Without contentIndex, fall back to scanning content for first toolCall."""
    runner = _runner(tmp_path)
    update = {
        "type": "toolcall_start",
        "partial": {
            "content": [
                {"type": "toolCall", "id": "only", "name": "read",
                 "arguments": {"path": "/x"}},
            ]
        }
    }
    tc = PiRunner._tool_call_from_update(update)
    assert tc["id"] == "only"


def test_tool_call_from_update_toolcall_end_prefers_top_level(tmp_path):
    """toolcall_end with top-level toolCall → returns it (ignoring content)."""
    runner = _runner(tmp_path)
    update = {
        "type": "toolcall_end",
        "contentIndex": 0,
        "toolCall": {"type": "toolCall", "id": "end_id", "name": "bash",
                      "arguments": {"command": "ls"}},
        "partial": {"content": []},
    }
    tc = PiRunner._tool_call_from_update(update)
    assert tc["id"] == "end_id"


# ── Cumulative usage: dedup across text_end / message_end / turn_end ──

def test_cumulative_usage_dedup_text_end_message_end_turn_end(tmp_path):
    """text_end + message_end + turn_end with same usage → cumulative == single,
    not ×3. last_call_usage and peak_context_tokens are still refreshed on
    every event (no regression)."""
    runner = _runner(tmp_path)
    state = StreamState(session_id="s")

    usage = {
        "input": 10,
        "output": 1,
        "cacheRead": 2,
        "cacheWrite": 3,
        "cost": {"total": 0.01},
    }

    # text_end: refreshes last_call_usage + peak, does NOT accumulate
    runner.parse_streaming_line({
        "type": "message_update",
        "assistantMessageEvent": {
            "type": "text_end",
            "partial": {"usage": usage},
        },
    }, state)
    assert state.last_call_usage["input_tokens"] == 10
    assert state.last_call_usage["output_tokens"] == 1
    assert state.peak_context_tokens == 10 + 2 + 3  # 15
    # Not yet accumulated
    assert state.cumulative_input == 0
    assert state.cumulative_output == 0
    assert state.cumulative_cache_read == 0
    assert state.cumulative_cache_write == 0
    assert state.cumulative_cost_total == 0.0

    # message_end: authority → now accumulate
    runner.parse_streaming_line({
        "type": "message_end",
        "message": {"stopReason": "stop", "usage": usage},
    }, state)
    assert state.cumulative_input == 10
    assert state.cumulative_output == 1
    assert state.cumulative_cache_read == 2
    assert state.cumulative_cache_write == 3
    assert state.cumulative_cost_total == 0.01

    # turn_end: refreshes last_call_usage + peak, does NOT re-accumulate
    runner.parse_streaming_line({
        "type": "turn_end",
        "message": {
            "stopReason": "stop",
            "content": [{"type": "text", "text": "ok"}],
            "usage": usage,
        },
    }, state)
    # Cumulative must NOT double — still single usage
    assert state.cumulative_input == 10
    assert state.cumulative_output == 1
    assert state.cumulative_cache_read == 2
    assert state.cumulative_cache_write == 3
    assert state.cumulative_cost_total == 0.01
    # last_call_usage + peak still refreshed
    assert state.last_call_usage["input_tokens"] == 10
    assert state.peak_context_tokens == 15

    # Build result → pi_footer_data carries the correct cumulative
    result = runner._build_streaming_result(state, "s")
    footer = result["pi_footer_data"]
    assert footer["cumulative_input"] == 10
    assert footer["cumulative_output"] == 1
    assert footer["cumulative_cache_read"] == 2
    assert footer["cumulative_cache_write"] == 3
    assert footer["cumulative_cost_total"] == 0.01


# ── Fallback accumulation: turn_end without message_end (error/aborted) ──


def test_terminal_turn_end_without_message_end_accumulates_fallback(tmp_path):
    """error/aborted turn_end without preceding message_end → fallback
    accumulate once, cumulative_* equals single usage (not 0, not 2x)."""
    runner = _runner(tmp_path)
    state = StreamState(session_id="s")

    usage = {
        "input": 50,
        "output": 5,
        "cacheRead": 10,
        "cacheWrite": 0,
        "cost": {"total": 0.05},
    }

    # text_end with partial usage (no cumulative accumulation)
    runner.parse_streaming_line({
        "type": "message_update",
        "assistantMessageEvent": {
            "type": "text_end",
            "partial": {"usage": usage},
        },
    }, state)
    # No message_end — error/aborted path
    runner.parse_streaming_line({
        "type": "turn_end",
        "message": {
            "stopReason": "error",
            "errorMessage": "something went wrong",
            "usage": usage,
        },
    }, state)

    # Stream terminated
    assert state.done is True
    assert state.is_error is True
    # Cumulative_* must equal single usage (fallback accumulated exactly once)
    assert state.cumulative_input == 50
    assert state.cumulative_output == 5
    assert state.cumulative_cache_read == 10
    assert state.cumulative_cache_write == 0
    assert state.cumulative_cost_total == 0.05
    # last_call_usage still refreshed
    assert state.last_call_usage["input_tokens"] == 50

    # pi_footer_data reflects correct cumulative
    result = runner._build_streaming_result(state, "s")
    footer = result["pi_footer_data"]
    assert footer["cumulative_input"] == 50
    assert footer["cumulative_output"] == 5
    assert footer["cumulative_cache_read"] == 10
    assert footer["cumulative_cache_write"] == 0
    assert footer["cumulative_cost_total"] == 0.05


def test_terminal_turn_end_without_message_end_aborted(tmp_path):
    """Same as above but with stopReason=aborted."""
    runner = _runner(tmp_path)
    state = StreamState(session_id="s")

    usage = {"input": 20, "output": 2, "cacheRead": 0, "cacheWrite": 0,
             "cost": {"total": 0.02}}

    runner.parse_streaming_line({
        "type": "turn_end",
        "message": {
            "stopReason": "aborted",
            "content": [{"type": "text", "text": "aborted early"}],
            "usage": usage,
        },
    }, state)

    assert state.done is True
    assert state.is_error is True
    assert state.cumulative_input == 20
    assert state.cumulative_output == 2
    assert state.cumulative_cost_total == 0.02


def test_message_end_then_turn_end_no_double_accumulation(tmp_path):
    """message_end accumulated → turn_end must NOT double-accumulate.
    Regression guard for the normal path."""
    runner = _runner(tmp_path)
    state = StreamState(session_id="s")

    usage = {"input": 100, "output": 10, "cacheRead": 0, "cacheWrite": 0,
             "cost": {"total": 0.10}}

    # message_end accumulates
    runner.parse_streaming_line({
        "type": "message_end",
        "message": {"stopReason": "stop", "usage": usage},
    }, state)
    assert state.cumulative_input == 100
    assert state._current_message_accumulated is True

    # turn_end must NOT accumulate again
    runner.parse_streaming_line({
        "type": "turn_end",
        "message": {
            "stopReason": "stop",
            "content": [{"type": "text", "text": "done"}],
            "usage": usage,
        },
    }, state)

    assert state.done is True
    assert state.is_error is False
    # Still single usage — no double
    assert state.cumulative_input == 100
    assert state.cumulative_output == 10
    assert state.cumulative_cost_total == 0.10
    # Flag reset for next turn
    assert state._current_message_accumulated is False

    # pi_footer_data
    result = runner._build_streaming_result(state, "s")
    footer = result["pi_footer_data"]
    assert footer["cumulative_input"] == 100


def test_cross_turn_flag_reset_prevents_leak(tmp_path):
    """After an error turn (fallback accumulated, flag reset),
    the next turn must accumulate its own usage independently."""
    runner = _runner(tmp_path)
    state = StreamState(session_id="s")

    # Turn 1: error, no message_end → fallback accumulate
    usage1 = {"input": 30, "output": 3, "cacheRead": 0, "cacheWrite": 0,
              "cost": {"total": 0.03}}
    runner.parse_streaming_line({
        "type": "turn_end",
        "message": {
            "stopReason": "error",
            "errorMessage": "fail",
            "usage": usage1,
        },
    }, state)
    assert state._current_message_accumulated is False  # reset after termination
    assert state.cumulative_input == 30

    # Simulate a new turn by resetting done (the stream loop creates a new
    # StreamState; here we reuse and clear done for test purposes).
    state.done = False
    state.is_error = False

    # Turn 2: normal path → message_end accumulates independently
    usage2 = {"input": 40, "output": 4, "cacheRead": 0, "cacheWrite": 0,
              "cost": {"total": 0.04}}
    runner.parse_streaming_line({
        "type": "message_end",
        "message": {"stopReason": "stop", "usage": usage2},
    }, state)
    runner.parse_streaming_line({
        "type": "turn_end",
        "message": {
            "stopReason": "stop",
            "content": [{"type": "text", "text": "ok"}],
            "usage": usage2,
        },
    }, state)

    # Cumulative = turn1 + turn2 (no leak, no skip)
    assert state.cumulative_input == 30 + 40
    assert state.cumulative_output == 3 + 4
    assert state.cumulative_cost_total == 0.03 + 0.04
