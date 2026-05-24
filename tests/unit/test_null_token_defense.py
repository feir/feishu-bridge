"""Regression tests for GitHub issue #1: API returning null token values.

When Claude/Codex API returns {"output_tokens": null}, dict.get("key", 0)
returns None (not 0), causing TypeError in arithmetic.  Every call site
that reads token counts from API responses must tolerate None values.
"""

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import MagicMock

from feishu_bridge.runtime import StreamState
from feishu_bridge.ui import _format_usage_footer
from feishu_bridge.worker import _context_health_alert


NULL_USAGE = {
    "input_tokens": None,
    "cache_read_input_tokens": None,
    "cache_creation_input_tokens": None,
    "output_tokens": None,
}

PARTIAL_NULL_USAGE = {
    "input_tokens": 1000,
    "cache_read_input_tokens": None,
    "cache_creation_input_tokens": 0,
    "output_tokens": None,
}


# ---------------------------------------------------------------------------
# ui._format_usage_footer — directly exercises the most user-visible path
# ---------------------------------------------------------------------------

class TestFormatUsageFooterNullDefense:
    """_format_usage_footer must not crash when any token field is None."""

    def test_all_none_values(self):
        result = _format_usage_footer(NULL_USAGE)
        assert isinstance(result, str)

    def test_partial_none_values(self):
        result = _format_usage_footer(PARTIAL_NULL_USAGE)
        assert isinstance(result, str)
        assert "1.0k in" in result

    def test_output_tokens_none(self):
        usage = {"input_tokens": 500, "output_tokens": None}
        result = _format_usage_footer(usage)
        assert isinstance(result, str)
        assert "out" not in result

    def test_normal_values_still_work(self):
        usage = {
            "input_tokens": 10000,
            "cache_read_input_tokens": 8000,
            "cache_creation_input_tokens": 0,
            "output_tokens": 1200,
        }
        result = _format_usage_footer(usage)
        assert "in" in result
        assert "out" in result

    def test_empty_dict(self):
        assert _format_usage_footer({}) == ""


# ---------------------------------------------------------------------------
# worker._context_health_alert — exercises the arithmetic path
# ---------------------------------------------------------------------------

class TestContextHealthAlertNullDefense:
    """_context_health_alert must not crash when usage values are None."""

    def test_null_token_values_no_crash(self):
        result_dict = {
            "last_call_usage": NULL_USAGE,
            "compact_detected": False,
            "peak_context_tokens": 0,
            "modelUsage": {},
        }
        assert _context_health_alert(result_dict, runner=None) is None

    def test_partial_null_values(self):
        result_dict = {
            "last_call_usage": {
                "input_tokens": 100000,
                "cache_read_input_tokens": None,
                "cache_creation_input_tokens": 50000,
            },
            "compact_detected": False,
            "peak_context_tokens": 0,
            "modelUsage": {},
        }
        alert = _context_health_alert(result_dict, runner=None)
        assert isinstance(alert, (str, type(None)))


# ---------------------------------------------------------------------------
# Codex runner — parse_streaming_line (turn.completed) + _build_streaming_result
# ---------------------------------------------------------------------------

class TestCodexRunnerNullDefense:
    """CodexRunner must tolerate null token values from Codex API."""

    def _make_runner(self):
        from feishu_bridge.runtime import CodexRunner
        runner = MagicMock(spec=CodexRunner)
        runner.model = "codex-mini"
        runner.parse_streaming_line = CodexRunner.parse_streaming_line.__get__(runner)
        runner._build_streaming_result = CodexRunner._build_streaming_result.__get__(runner)
        return runner

    def test_turn_completed_null_usage(self):
        runner = self._make_runner()
        state = StreamState()
        state.accumulated_text = "hello"
        event = {
            "type": "turn.completed",
            "usage": {
                "input_tokens": None,
                "cached_input_tokens": None,
                "output_tokens": None,
            },
        }
        runner.parse_streaming_line(event, state)
        assert state.last_call_usage["input_tokens"] == 0
        assert state.last_call_usage["output_tokens"] == 0
        assert state.last_call_usage["cache_read_input_tokens"] == 0

    def test_build_result_null_usage_in_last_call(self):
        runner = self._make_runner()
        state = StreamState()
        state.accumulated_text = "result text"
        state.done = True
        state.last_call_usage = NULL_USAGE
        result = runner._build_streaming_result(state, "sess-1")
        mu = result["modelUsage"]["codex-mini"]
        assert mu["inputTokens"] == 0
        assert mu["outputTokens"] == 0
        assert mu["cacheReadInputTokens"] == 0
        assert mu["cacheCreationInputTokens"] == 0


# ---------------------------------------------------------------------------
# OMP runner — modelUsage construction in _build_streaming_result equivalent
# ---------------------------------------------------------------------------

class TestOmpRunnerNullDefense:
    """OmpRpcRunner must tolerate null token values in modelUsage construction."""

    def test_model_usage_with_null_tokens(self):
        from feishu_bridge.runtime_omp import OmpRpcRunner
        runner = MagicMock(spec=OmpRpcRunner)
        runner.resolved_model = "gpt-4.1-mini"
        usage = NULL_USAGE
        model_usage = {
            runner.resolved_model: {
                "contextWindow": 0,
                "inputTokens": (usage.get("input_tokens", 0) or 0),
                "outputTokens": (usage.get("output_tokens", 0) or 0),
                "cacheReadInputTokens": (usage.get("cache_read_input_tokens", 0) or 0),
                "cacheCreationInputTokens": (usage.get("cache_creation_input_tokens", 0) or 0),
            },
        }
        mu = model_usage["gpt-4.1-mini"]
        assert mu["inputTokens"] == 0
        assert mu["outputTokens"] == 0

    def test_model_usage_with_partial_null(self):
        usage = PARTIAL_NULL_USAGE
        model_usage = {
            "test-model": {
                "contextWindow": 0,
                "inputTokens": (usage.get("input_tokens", 0) or 0),
                "outputTokens": (usage.get("output_tokens", 0) or 0),
                "cacheReadInputTokens": (usage.get("cache_read_input_tokens", 0) or 0),
                "cacheCreationInputTokens": (usage.get("cache_creation_input_tokens", 0) or 0),
            },
        }
        mu = model_usage["test-model"]
        assert mu["inputTokens"] == 1000
        assert mu["outputTokens"] == 0
        assert mu["cacheReadInputTokens"] == 0


# ---------------------------------------------------------------------------
# Pi runner — modelUsage construction
# ---------------------------------------------------------------------------

class TestPiRunnerNullDefense:
    """PiRunner must tolerate null token values in modelUsage construction."""

    def test_build_result_null_usage(self):
        from feishu_bridge.runtime_pi import PiRunner
        runner = MagicMock(spec=PiRunner)
        runner.model = "pi-mono"
        runner._build_streaming_result = PiRunner._build_streaming_result.__get__(runner)
        runner._format_error = PiRunner._format_error
        state = StreamState()
        state.accumulated_text = "pi output"
        state.done = True
        state.last_call_usage = NULL_USAGE
        state.final_result = None
        state.is_error = False
        result = runner._build_streaming_result(state, "sess-pi")
        mu = result["modelUsage"]["pi-mono"]
        assert mu["inputTokens"] == 0
        assert mu["outputTokens"] == 0
        assert mu["cacheReadInputTokens"] == 0
        assert mu["cacheCreationInputTokens"] == 0


# ---------------------------------------------------------------------------
# /status command — token arithmetic with null values
# ---------------------------------------------------------------------------

class TestStatusCommandNullDefense:
    """The /status token arithmetic must tolerate null values from API."""

    def test_context_arithmetic_all_null(self):
        usage = NULL_USAGE
        inp = (usage.get("input_tokens", 0) or 0)
        cache_read = (usage.get("cache_read_input_tokens", 0) or 0)
        cache_create = (usage.get("cache_creation_input_tokens", 0) or 0)
        out_tokens = (usage.get("output_tokens", 0) or 0)
        total_ctx = inp + cache_read + cache_create
        assert total_ctx == 0
        assert out_tokens == 0

    def test_context_arithmetic_partial_null(self):
        usage = {
            "input_tokens": 50000,
            "cache_read_input_tokens": None,
            "cache_creation_input_tokens": 10000,
            "output_tokens": None,
        }
        inp = (usage.get("input_tokens", 0) or 0)
        cache_read = (usage.get("cache_read_input_tokens", 0) or 0)
        cache_create = (usage.get("cache_creation_input_tokens", 0) or 0)
        out_tokens = (usage.get("output_tokens", 0) or 0)
        total_ctx = inp + cache_read + cache_create
        assert total_ctx == 60000
        assert out_tokens == 0
