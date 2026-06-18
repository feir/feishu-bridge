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
        },
        "last_call_usage": {
            "input_tokens": 369,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 3,
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
        {"name": "Read", "hint_data": "/a/README.md"}]
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
    assert state.pending_tool_status == [{"name": "Bash", "hint_data": "ls"}]


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
        {"name": "Read", "hint_data": "/x/b.py"},
        {"name": "Read", "hint_data": "/x/a.py"},
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
    """Malformed events must not raise; degrade to a bare-name entry (spec:
    never-raises → bare tool label), not silently dropped."""
    runner = _runner(tmp_path)
    state = StreamState()
    # arguments not a dict, arriving on END (call resolving) → bare-name entry
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
    assert state.pending_tool_status == [{"name": "Read", "hint_data": ""}]


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
        _toolcall_event("toolcall_end", call_id="sub_1", name="subagent",
                        arguments={"agent": "scout", "task": "分析代码"}), state)
    assert state.pending_tool_status == []
    assert state.pending_agent_launches == [
        {"description": "分析代码", "name": None, "subagent_type": "scout"},
    ]
    assert "sub_1" in state._tool_seen_ids


def test_subagent_missing_agent_logs_warning_and_skips(tmp_path, caplog):
    """subagent without agent → logs warning, no tool_status or agent_launches."""
    runner = _runner(tmp_path)
    state = StreamState()
    with caplog.at_level(logging.WARNING):
        runner.parse_streaming_line(
            _toolcall_event("toolcall_end", call_id="sub_2", name="subagent",
                            arguments={"task": "分析代码"}), state)
    assert state.pending_agent_launches is None
    assert state.pending_tool_status == []
    assert "sub_2" in state._tool_seen_ids
    assert "Subagent toolcall with unrecognized args shape" in caplog.text


def test_subagent_missing_task_logs_warning_and_skips(tmp_path, caplog):
    """subagent without task → logs warning, no tool_status or agent_launches."""
    runner = _runner(tmp_path)
    state = StreamState()
    with caplog.at_level(logging.WARNING):
        runner.parse_streaming_line(
            _toolcall_event("toolcall_end", call_id="sub_3", name="subagent",
                            arguments={"agent": "scout"}), state)
    assert state.pending_agent_launches is None
    assert state.pending_tool_status == []
    assert "sub_3" in state._tool_seen_ids
    assert "Subagent toolcall with unrecognized args shape" in caplog.text


def test_subagent_missing_both_agent_and_task_logs_warning(tmp_path, caplog):
    """subagent with unrecognized args → logs warning, no tool_status or agent_launches."""
    runner = _runner(tmp_path)
    state = StreamState()
    with caplog.at_level(logging.WARNING):
        runner.parse_streaming_line(
            _toolcall_event("toolcall_end", call_id="sub_empty", name="subagent",
                            arguments={"unknown": "x"}), state)
    assert state.pending_agent_launches is None
    assert state.pending_tool_status == []
    assert "sub_empty" in state._tool_seen_ids
    assert "Subagent toolcall with unrecognized args shape" in caplog.text


def test_subagent_deferred_start_to_end(tmp_path):
    """start(no args) → end(with args): deferred extraction fires agent_launch."""
    runner = _runner(tmp_path)
    state = StreamState()
    # start with no args → deferred
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", call_id="sub_4", name="subagent"), state)
    assert state.pending_tool_status == []
    assert state.pending_agent_launches is None
    # end with args → fires
    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="sub_4", name="subagent",
                        arguments={"agent": "developer", "task": "实现功能"}), state)
    assert state.pending_agent_launches == [
        {"description": "实现功能", "name": None, "subagent_type": "developer"},
    ]
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
    """Same call_id should not produce duplicate launches."""
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="sub_5", name="subagent",
                        arguments={"agent": "scout", "task": "分析"}), state)
    # Replay same id
    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="sub_5", name="subagent",
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
        _toolcall_event("toolcall_end", call_id="sub_m1", name="subagent",
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
    assert "sub_m1" in state._tool_seen_ids


def test_subagent_tasks_multi_ignores_invalid_entries(tmp_path):
    """tasks[] with non-dict entries → only valid entries become launches."""
    runner = _runner(tmp_path)
    state = StreamState()
    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="sub_m2", name="subagent",
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
        _toolcall_event("toolcall_end", call_id="sub_m3", name="subagent",
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
    """tasks[] args arriving on end after deferred start → still dispatches."""
    runner = _runner(tmp_path)
    state = StreamState()
    # start with no args → deferred
    runner.parse_streaming_line(
        _toolcall_event("toolcall_start", call_id="sub_m4", name="subagent"), state)
    assert state.pending_agent_launches is None
    # end with tasks[] → fires
    runner.parse_streaming_line(
        _toolcall_event("toolcall_end", call_id="sub_m4", name="subagent",
                        arguments={
                            "tasks": [
                                {"agent": "developer", "task": "实现A"},
                                {"agent": "git-ops", "task": "提交"},
                            ],
                        }), state)
    assert state.pending_agent_launches == [
        {"description": "实现A", "name": None, "subagent_type": "developer"},
        {"description": "提交", "name": None, "subagent_type": "git-ops"},
    ]
    assert state.pending_tool_status == []


# ---- Fix C: Subagent empty-args / non-dict-args paths ----

def test_subagent_empty_args_on_end_logs_warning_no_status(tmp_path, caplog):
    """Subagent args={} on end event → warning, no pending_tool_status or agent_launches."""
    runner = _runner(tmp_path)
    state = StreamState()
    with caplog.at_level(logging.WARNING):
        runner.parse_streaming_line(
            _toolcall_event("toolcall_end", call_id="sub_empty_end",
                            name="subagent", arguments={}), state)
    assert state.pending_tool_status == []
    assert state.pending_agent_launches is None
    assert "sub_empty_end" in state._tool_seen_ids
    assert "Subagent toolcall with unrecognized args shape" in caplog.text


def test_subagent_non_dict_args_logs_warning(tmp_path, caplog):
    """Subagent arguments=None or string → warning, no tool_status or agent_launches."""
    runner = _runner(tmp_path)
    state = StreamState()
    with caplog.at_level(logging.WARNING):
        runner.parse_streaming_line(
            _toolcall_event("toolcall_end", call_id="sub_none",
                            name="subagent", arguments=None), state)
    assert state.pending_tool_status == []
    assert state.pending_agent_launches is None
    assert "sub_none" in state._tool_seen_ids
    assert "Subagent toolcall with unrecognized args shape" in caplog.text


def test_subagent_empty_args_on_start_defers(tmp_path, caplog):
    """Subagent args={} on start → defer (no warning, no push, no seen_ids).

    The end event may carry usable args; start must not consume the call_id.
    """
    runner = _runner(tmp_path)
    state = StreamState()
    with caplog.at_level(logging.WARNING):
        runner.parse_streaming_line(
            _toolcall_event("toolcall_start", call_id="sub_defer",
                            name="subagent", arguments={}), state)
    assert state.pending_tool_status == []
    assert state.pending_agent_launches is None
    assert "sub_defer" not in state._tool_seen_ids
    # No warning logged for deferred start
    assert "Subagent toolcall with unrecognized args shape" not in caplog.text
