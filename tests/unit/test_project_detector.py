"""Unit tests for ``project_detector`` (Stage 2 memory-system-fix)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from feishu_bridge.project_detector import (
    HISTORY_TRIGGER_WORDS,
    ProjectMatch,
    detect_project_intent,
    has_history_trigger,
)
from feishu_bridge.state_thread_projects import ProjectEntry


@pytest.fixture
def projects():
    return [
        ProjectEntry(id="feishu-bridge", name="Feishu Claude Bridge",
                     path="`~/projects/feishu-bridge`"),
        ProjectEntry(id="investment-dashboard", name="Investment Dashboard",
                     path="`~/projects/investment-dashboard`"),
        ProjectEntry(id="options-trading", name="Options Trading",
                     path="`~/projects/options-trading`"),
        ProjectEntry(id="dotclaude", name="Claude Code Config",
                     path="`~/.claude`"),
        ProjectEntry(id="news", name="News Briefing 简报",
                     path="`~/projects/news-briefing`"),
    ]


# ── id-literal high-confidence ──────────────────────────────────────────────


class TestIdLiteral:
    def test_exact_id_match(self, projects):
        m = detect_project_intent("feishu-bridge 怎么发版", projects)
        assert m == ProjectMatch(
            project_id="feishu-bridge", confidence="high", matched_via="id_token"
        )

    def test_id_case_insensitive(self, projects):
        m = detect_project_intent("how do I deploy FEISHU-BRIDGE", projects)
        assert m is not None
        assert m.project_id == "feishu-bridge"
        assert m.confidence == "high"

    def test_longest_id_wins_when_multiple(self, projects):
        # Both "news" and a hypothetical longer id; only "news" present here so
        # this is a smoke test for the tiebreaker
        m = detect_project_intent("news 今天有什么新东西？", projects)
        assert m is not None
        assert m.project_id == "news"

    def test_id_surrounded_by_punctuation(self, projects):
        m = detect_project_intent("`feishu-bridge` 上次部署？", projects)
        assert m is not None
        assert m.project_id == "feishu-bridge"
        assert m.confidence == "high"


# ── path-prefix high-confidence ─────────────────────────────────────────────


class TestPathPrefix:
    def test_tilde_path(self, projects):
        m = detect_project_intent(
            "看一下 ~/projects/feishu-bridge/feishu_bridge/runtime.py 怎么写的",
            projects,
        )
        assert m is not None
        assert m.project_id == "feishu-bridge"
        assert m.confidence == "high"
        assert m.matched_via == "path_prefix"

    def test_absolute_path_matches_after_expanduser(self, projects, monkeypatch):
        home = os.path.expanduser("~")
        msg = f"投资仪表盘代码看 {home}/projects/investment-dashboard/backend"
        m = detect_project_intent(msg, projects)
        assert m is not None
        assert m.project_id == "investment-dashboard"

    def test_path_to_subdir_still_matches_root(self, projects):
        m = detect_project_intent(
            "改一下 ~/projects/options-trading/scripts/daily.py", projects
        )
        assert m is not None
        assert m.project_id == "options-trading"

    def test_longest_root_wins(self, projects):
        # Add a deeper project root that's a subpath of feishu-bridge to verify
        # the longest-match rule
        deeper = ProjectEntry(
            id="fb-subpkg",
            name="FB Subpackage",
            path="`~/projects/feishu-bridge/feishu_bridge`",
        )
        m = detect_project_intent(
            "改 ~/projects/feishu-bridge/feishu_bridge/runtime.py", projects + [deeper]
        )
        assert m is not None
        assert m.project_id == "fb-subpkg"

    def test_unrelated_path(self, projects):
        # path that does not match any registered root
        m = detect_project_intent("看 /etc/hosts", projects)
        # /etc/hosts won't match any project root
        assert m is None or m.confidence != "high" or m.matched_via != "path_prefix"


# ── name match (medium) ─────────────────────────────────────────────────────


class TestNameMatch:
    def test_chinese_name_substring(self, projects):
        # "Investment Dashboard" 的中文名是 "Investment Dashboard" 在 projects
        # 里, but news has Chinese name "News Briefing 简报"
        m = detect_project_intent("简报今天发了么", projects)
        # "简报" length 2 < 3 minimum, so should NOT match
        # actually "News Briefing 简报" — "News Briefing" length 13, "简报" 2
        # The full name is "News Briefing 简报" (length 14) which IS ≥3
        # but "简报今天发了么" does NOT contain the full name
        assert m is None or m.confidence != "medium"

    def test_full_name_substring(self, projects):
        m = detect_project_intent(
            "Investment Dashboard 后端跑不起来了", projects
        )
        assert m is not None
        assert m.project_id == "investment-dashboard"
        assert m.confidence == "medium"

    def test_partial_name_does_not_match(self, projects):
        # "Investment" alone is part of the name but not the full name
        m = detect_project_intent("Investment 跑不起来", projects)
        assert m is None or m.confidence != "medium"


# ── fuzzy prefix (low) ──────────────────────────────────────────────────────


class TestFuzzyPrefix:
    def test_token_starts_with_id(self, projects):
        # "feishu-bridges" is not == "feishu-bridge" but starts with it
        # But "_id_tokens" treats "feishu-bridges" as one token containing "-"
        m = detect_project_intent("feishu-bridges 复数形式", projects)
        # The regex pattern includes - and _ so "feishu-bridges" is one token
        assert m is not None
        # Should NOT be high (not exact), but low fuzzy
        assert m.project_id == "feishu-bridge"
        assert m.confidence == "low"

    def test_too_short_id_skipped(self, projects):
        # "news" has len 4 — at the threshold. So "newsletter" should match low.
        m = detect_project_intent("newsletter 怎么订阅", projects)
        assert m is not None
        assert m.project_id == "news"
        assert m.confidence == "low"


# ── none confidence ─────────────────────────────────────────────────────────


class TestNoMatch:
    def test_chitchat(self, projects):
        assert detect_project_intent("今天天气真不错啊", projects) is None

    def test_empty_message(self, projects):
        assert detect_project_intent("", projects) is None
        assert detect_project_intent("   ", projects) is None

    def test_empty_projects(self):
        assert detect_project_intent("feishu-bridge", []) is None
        assert detect_project_intent("feishu-bridge", iter([])) is None

    def test_message_with_unrelated_path(self, projects):
        m = detect_project_intent(
            "看看 /tmp/some/random/file 是干啥的", projects
        )
        # No id literal, no path prefix match, no name match
        # No token in the message starts with any registry id ≥4 chars
        assert m is None


# ── priority ────────────────────────────────────────────────────────────────


class TestPriority:
    def test_path_beats_id_when_different(self, projects):
        """A concrete path is more specific user intent than a bare id mention."""
        m = detect_project_intent(
            "feishu-bridge 怎么处理 ~/projects/options-trading/x.py", projects
        )
        # Both present; the path is the more specific reference → options-trading
        assert m is not None
        assert m.project_id == "options-trading"
        assert m.confidence == "high"
        assert m.matched_via == "path_prefix"

    def test_path_beats_name(self, projects):
        m = detect_project_intent(
            "Investment Dashboard 改 ~/projects/feishu-bridge/x.py", projects
        )
        # Path matches feishu-bridge (high), name matches investment-dashboard (medium)
        # path_prefix runs in priority after id_token; id_token finds nothing here
        # so path_prefix wins → feishu-bridge
        assert m is not None
        assert m.project_id == "feishu-bridge"
        assert m.confidence == "high"

    def test_name_beats_fuzzy(self, projects):
        # Name match in message; no id literal / path
        m = detect_project_intent("Investment Dashboard 状态", projects)
        assert m is not None
        assert m.confidence == "medium"


# ── history trigger words ───────────────────────────────────────────────────


class TestHistoryTrigger:
    @pytest.mark.parametrize("msg", [
        "我上次部署的时候",
        "之前用过什么命令",
        "以前怎么搞的",
        "曾经我们试过",
        "忘了具体步骤",
        "想不起来了",
        "部署在哪台机器",
        "历史决策是啥",
        "用过哪个工具",
        "deploy how",
        "history of changes",
    ])
    def test_trigger_words_detected(self, msg):
        assert has_history_trigger(msg) is True

    @pytest.mark.parametrize("msg", [
        "今天天气真好",
        "你好",
        "hello world",
        "",
        None,
    ])
    def test_no_trigger(self, msg):
        assert has_history_trigger(msg) is False

    def test_all_trigger_words_in_list(self):
        # Sanity: list isn't accidentally empty
        assert len(HISTORY_TRIGGER_WORDS) >= 10
