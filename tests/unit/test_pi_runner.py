#!/usr/bin/env python3
"""Unit tests for the Pi runner integration."""

import json

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


def test_pi_build_args_defaults_to_json_readonly_tools_and_session(tmp_path):
    runner = _runner(
        tmp_path,
        extra_cli_args=["--provider", "omlx"],
        extra_system_prompts=["bridge rules"],
    )

    args = runner.build_args("hello", "sid/with spaces", False, True)

    assert args[:3] == ["pi", "--mode", "json"]
    assert args[3:5] == ["--provider", "omlx"]
    assert ["--model", "Qwen3.6-35B-A3B-mxfp4"] == args[5:7]
    assert ["--tools", "read,grep,find,ls"] == args[7:9]
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

    runner = _runner(
        tmp_path,
        extra_cli_args=["--session", str(tmp_path / "custom.jsonl")],
    )
    args = runner.build_args("hello", "bridge-sid", False, True)
    assert args.count("--session") == 1
    assert str(tmp_path / "custom.jsonl") in args


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
                "contextWindow": 32768,
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
            "contextWindow": 32768,
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
    assert result["result"] == "404 Model not found"
    assert result["session_id"] == "bridge-sid"


def test_pi_parse_tool_status_events(tmp_path):
    runner = _runner(tmp_path)
    state = StreamState()

    runner.parse_streaming_line({
        "type": "message_update",
        "assistantMessageEvent": {
            "type": "toolcall_start",
            "partial": {
                "content": [{"type": "toolCall", "name": "ls"}],
            },
        },
    }, state)
    runner.parse_streaming_line({
        "type": "tool_execution_start",
        "toolName": "read",
    }, state)
    runner.parse_streaming_line({
        "type": "tool_execution_end",
        "toolName": "read",
        "result": {"isError": True},
    }, state)

    assert state.pending_tool_status == ["ls", "read", "read"]
    assert state.is_error is False
    assert state.done is False


def test_create_runner_pi_builds_pi_runner(tmp_path):
    agent_cfg = {
        "type": "pi",
        "_resolved_command": "pi",
        "timeout_seconds": 30,
        "providers": {
            "default": {
                "args_by_type": {"pi": ["--provider", "omlx"]},
                "models": {"pi": "Qwen3.6-35B-A3B-mxfp4"},
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
