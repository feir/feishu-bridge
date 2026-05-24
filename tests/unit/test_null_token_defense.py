"""Regression tests for GitHub issue #1: API returning null token values.

When Claude/Codex API returns {"output_tokens": null}, dict.get("key", 0)
returns None (not 0), causing TypeError in arithmetic.  Every call site
that reads token counts from API responses must tolerate None values.
"""

from feishu_bridge.ui import _format_usage_footer
from feishu_bridge.worker import _context_health_alert


# ---------------------------------------------------------------------------
# ui._format_usage_footer — directly exercises the most user-visible path
# ---------------------------------------------------------------------------

class TestFormatUsageFooterNullDefense:
    """_format_usage_footer must not crash when any token field is None."""

    def test_all_none_values(self):
        usage = {
            "input_tokens": None,
            "cache_read_input_tokens": None,
            "cache_creation_input_tokens": None,
            "output_tokens": None,
        }
        result = _format_usage_footer(usage)
        assert isinstance(result, str)  # no TypeError

    def test_partial_none_values(self):
        usage = {
            "input_tokens": 1000,
            "cache_read_input_tokens": None,
            "cache_creation_input_tokens": 0,
            "output_tokens": None,
        }
        result = _format_usage_footer(usage)
        assert isinstance(result, str)
        assert "1.0k in" in result

    def test_output_tokens_none(self):
        usage = {
            "input_tokens": 500,
            "output_tokens": None,
        }
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
        result = _format_usage_footer({})
        assert result == ""


# ---------------------------------------------------------------------------
# worker._context_health_alert — exercises the arithmetic path
# ---------------------------------------------------------------------------

class TestContextHealthAlertNullDefense:
    """_context_health_alert must not crash when usage values are None."""

    def test_null_token_values_no_crash(self):
        result_dict = {
            "last_call_usage": {
                "input_tokens": None,
                "cache_read_input_tokens": None,
                "cache_creation_input_tokens": None,
            },
            "compact_detected": False,
            "peak_context_tokens": 0,
            "modelUsage": {},
        }
        alert = _context_health_alert(result_dict, runner=None)
        assert alert is None

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
