#!/usr/bin/env python3
"""Unit tests for pi TUI-style usage footer (task: pi-runner usage/context footer)."""

from feishu_bridge.ui import _format_pi_usage_footer, build_cardkit_final_card
from feishu_bridge.runtime_pi import PiRunner


# ---------------------------------------------------------------------------
# _format_pi_usage_footer
# ---------------------------------------------------------------------------

class TestFormatPiUsageFooter:
    def test_empty_dict_returns_empty_string(self):
        assert _format_pi_usage_footer({}) == ""

    def test_non_dict_returns_empty_string(self):
        assert _format_pi_usage_footer(None) == ""
        assert _format_pi_usage_footer("not a dict") == ""
        assert _format_pi_usage_footer(42) == ""

    def test_full_cumulative_fields(self):
        """All token segments + cache CH% + cost."""
        data = {
            "cumulative_input": 12345,
            "cumulative_output": 5678,
            "cumulative_cache_read": 4500,
            "cumulative_cache_write": 200,
            "cumulative_cost_total": 0.123,
            "latest_cache_hit_rate": 85.1,
            "context_window": 0,
            "is_oauth": False,
            "model": None,
        }
        result = _format_pi_usage_footer(data)
        assert "↑12.3k" in result
        assert "↓5.7k" in result
        assert "R4.5k" in result
        # W=200 is <1000, so "200" not "0.2k"
        assert "W200" in result
        assert "CH85.1%" in result
        assert "$0.123" in result

    def test_oauth_sub_label_with_zero_cost(self):
        data = {
            "cumulative_cost_total": 0.0,
            "is_oauth": True,
        }
        result = _format_pi_usage_footer(data)
        assert "$0.000 (sub)" in result

    def test_cost_zero_not_shown_when_not_oauth(self):
        data = {
            "cumulative_cost_total": 0.0,
            "is_oauth": False,
        }
        result = _format_pi_usage_footer(data)
        assert "$" not in result

    def test_context_window_percent(self):
        data = {
            "cumulative_input": 50000,
            "cumulative_cache_read": 25000,
            "cumulative_cache_write": 5000,
            "context_window": 200000,
            "context_tokens": 80000,
        }
        result = _format_pi_usage_footer(data)
        # 80000/200000 * 100 = 40.0%; 200000 → 200.0k
        assert "40.0%/200.0k" in result

    def test_context_percent_capped_at_100(self):
        """cumulative_input far exceeds window → context% still ≤ 100%."""
        data = {
            "cumulative_input": 500000,
            "cumulative_cache_read": 200000,
            "cumulative_cache_write": 100000,
            "context_window": 200000,
            "context_tokens": 200000,
        }
        result = _format_pi_usage_footer(data)
        # peak_context_tokens == window → 100.0%; cumulative would have been 400%
        assert "100.0%/200.0k" in result

    def test_context_percent_uses_peak_not_cumulative(self):
        """Even with moderate cumulative, use context_tokens (peak)."""
        data = {
            "cumulative_input": 150000,
            "cumulative_cache_read": 0,
            "cumulative_cache_write": 0,
            "context_window": 200000,
            "context_tokens": 150000,
        }
        result = _format_pi_usage_footer(data)
        # peak 150000 / window 200000 = 75.0%
        assert "75.0%/200.0k" in result

    def test_context_window_question_mark_when_no_tokens(self):
        data = {
            "context_window": 100000,
        }
        result = _format_pi_usage_footer(data)
        # 100000 → 100.0k
        assert "?/100.0k" in result

    def test_context_window_zero_skipped(self):
        data = {
            "context_window": 0,
            "cumulative_input": 100,
        }
        result = _format_pi_usage_footer(data)
        assert "%/" not in result

    def test_model_appended(self):
        data = {
            "model": "anthropic/claude-opus-4-6",
        }
        result = _format_pi_usage_footer(data)
        assert "anthropic/claude-opus-4-6" in result

    def test_include_context_false_skips_context_window(self):
        data = {
            "cumulative_input": 50000,
            "cumulative_cache_read": 25000,
            "cumulative_cache_write": 5000,
            "context_window": 200000,
            "context_tokens": 80000,
            "model": "anthropic/claude-opus-4-6",
        }
        result = _format_pi_usage_footer(data, include_context=False)
        assert "↑50.0k" in result
        assert "R25.0k" in result
        assert "%/" not in result
        assert "anthropic/claude-opus-4-6" in result

    def test_include_model_false_skips_model(self):
        data = {
            "cumulative_input": 12345,
            "cumulative_output": 5678,
            "model": "anthropic/claude-opus-4-6",
        }
        result = _format_pi_usage_footer(data, include_model=False)
        assert "↑12.3k" in result
        assert "anthropic/claude-opus-4-6" not in result

    def test_both_false_returns_only_token_cache_cost(self):
        data = {
            "cumulative_input": 12345,
            "cumulative_output": 5678,
            "cumulative_cache_read": 4500,
            "cumulative_cost_total": 0.123,
            "context_window": 200000,
            "context_tokens": 80000,
            "model": "anthropic/claude-opus-4-6",
        }
        result = _format_pi_usage_footer(
            data, include_context=False, include_model=False)
        assert "↑12.3k" in result
        assert "↓5.7k" in result
        assert "R4.5k" in result
        assert "$0.123" in result
        assert "%/" not in result
        assert "anthropic/claude-opus-4-6" not in result

    def test_zero_tokens_not_output(self):
        """cumulative fields with 0 should not produce prefix lines."""
        data = {
            "cumulative_input": 0,
            "cumulative_output": 0,
            "cumulative_cache_read": 0,
            "cumulative_cache_write": 0,
            "is_oauth": True,
        }
        result = _format_pi_usage_footer(data)
        # Only $0.000 (sub) should appear
        assert result == "$0.000 (sub)"

    def test_cache_hit_rate_only_when_cache_active_and_rate_present(self):
        data = {
            "cumulative_cache_read": 100,
            "latest_cache_hit_rate": 50.0,
        }
        result = _format_pi_usage_footer(data)
        assert "CH50.0%" in result

        # No cache rows, no CH% even if rate exists
        data2 = {
            "latest_cache_hit_rate": 50.0,
        }
        result2 = _format_pi_usage_footer(data2)
        assert "CH" not in result2

    def test_cache_hit_rate_with_cache_write(self):
        data = {
            "cumulative_cache_write": 100,
            "latest_cache_hit_rate": 25.0,
        }
        result = _format_pi_usage_footer(data)
        assert "CH25.0%" in result


# ---------------------------------------------------------------------------
# build_cardkit_final_card with pi_footer
# ---------------------------------------------------------------------------

class TestBuildCardkitFinalCardPiFooter:
    def test_pi_footer_appears_in_footer_markdown(self):
        card = build_cardkit_final_card("Hello", pi_footer="↑1k  ↓500  R200")
        elements = card["body"]["elements"]
        # Find the footer markdown element
        footer = elements[-1]
        assert footer["tag"] == "markdown"
        assert footer["text_size"] == "notation"
        assert "↑1k  ↓500  R200" in footer["content"]
        # The status line should still be present
        assert "✅" in footer["content"]

    def test_pi_footer_after_status_line(self):
        card = build_cardkit_final_card(
            "Hello", pi_footer="X", elapsed_s=30, model_name="claude-sonnet"
        )
        footer = card["body"]["elements"][-1]
        content = footer["content"]
        # status_line comes first, then pi_footer on next line
        lines = content.split("\n")
        # line 0: "---", line 1: status_line, line 2: pi_footer (if present)
        assert len(lines) >= 3
        assert "✅" in lines[1]
        assert lines[2] == "X"

    def test_no_pi_footer_output_matches_old_behavior(self):
        """Without pi_footer, output structure is byte-identical to before."""
        card = build_cardkit_final_card(
            "Hello", elapsed_s=30, model_name="claude-sonnet"
        )
        elements = card["body"]["elements"]
        footer = elements[-1]
        content = footer["content"]
        lines = content.split("\n")
        # "---\n✅ · sonnet · 30.0s" exactly — no extra blank pi_footer line
        assert lines[0] == "---"
        assert "✅" in lines[1]
        assert "sonnet" in lines[1]
        assert "30.0s" in lines[1]

    def test_usage_footer_takes_priority_over_last_call_usage(self):
        usage = {"input_tokens": 100, "output_tokens": 50}
        card = build_cardkit_final_card(
            "Hello",
            usage_footer="PI_USAGE_HERE",
            last_call_usage=usage,
            model_name="claude-sonnet",
        )
        content = card["body"]["elements"][-1]["content"]
        assert "PI_USAGE_HERE" in content
        assert "100 in" not in content
        assert "50 out" not in content

    def test_usage_footer_used_in_status_line_without_pi_footer(self):
        card = build_cardkit_final_card(
            "Hello",
            usage_footer="↑1k  ↓500  R200",
            pi_footer=None,
            model_name="claude-sonnet",
            elapsed_s=30,
        )
        lines = card["body"]["elements"][-1]["content"].split("\n")
        assert "✅" in lines[1]
        assert "sonnet" in lines[1]
        assert "↑1k" in lines[1]
        assert "↓500" in lines[1]
        assert len(lines) == 2

    def test_usage_footer_falls_back_to_last_call_usage(self):
        usage = {"input_tokens": 12000, "output_tokens": 500}
        card = build_cardkit_final_card("Hello", last_call_usage=usage)
        content = card["body"]["elements"][-1]["content"]
        assert "12.0k in" in content
        assert "500 out" in content

    def test_usage_footer_falls_back_to_total_tokens(self):
        card = build_cardkit_final_card("Hello", total_tokens=5000)
        content = card["body"]["elements"][-1]["content"]
        assert "5.0k tokens" in content

    def test_pi_footer_with_context_alert_and_banner(self):
        """pi_footer should appear between status_line and context_alert."""
        card = build_cardkit_final_card(
            "Hello",
            pi_footer="CF",
            context_alert="⚠️ near limit",
        )
        footer = card["body"]["elements"][-1]
        content = footer["content"]
        lines = content.split("\n")
        # line 0: ---
        # line 1: status_line
        # line 2: pi_footer
        # line 3: context_alert
        # line 4: banner (if any)
        assert "CF" in lines[2]
        assert "⚠️ near limit" in lines[3]

    def test_card_structure_unchanged_when_no_pi_footer(self):
        """All other elements (markdown, buttons, etc.) unchanged."""
        card = build_cardkit_final_card(
            "Hello https://github.com/x",
            chat_id="oc_1", bot_id="cli_1",
        )
        elements = card["body"]["elements"]
        # markdown + url column_set + footer
        assert len(elements) == 3
        assert elements[0]["tag"] == "markdown"
        assert elements[1]["tag"] == "column_set"
        assert elements[2]["tag"] == "markdown"
        assert elements[2]["text_size"] == "notation"


# ---------------------------------------------------------------------------
# PiRunner._normalize_usage — cost_total field
# ---------------------------------------------------------------------------

class TestPiNormalizeUsage:
    def test_cost_total_extracted(self):
        usage = {
            "input": 100,
            "output": 50,
            "cost": {"total": 0.05},
        }
        result = PiRunner._normalize_usage(usage)
        assert result["cost_total"] == 0.05

    def test_cost_total_zero_when_missing(self):
        usage = {"input": 100, "output": 50}
        result = PiRunner._normalize_usage(usage)
        assert result["cost_total"] == 0.0

    def test_cost_total_zero_when_cost_empty(self):
        usage = {"input": 100, "cost": {}}
        result = PiRunner._normalize_usage(usage)
        assert result["cost_total"] == 0.0

    def test_old_fields_preserved(self):
        usage = {
            "input": 100,
            "output": 50,
            "cacheRead": 30,
            "cacheWrite": 10,
        }
        result = PiRunner._normalize_usage(usage)
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50
        assert result["cache_read_input_tokens"] == 30
        assert result["cache_creation_input_tokens"] == 10

    def test_none_usage_returns_empty(self):
        assert PiRunner._normalize_usage(None) == {}
