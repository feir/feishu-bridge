"""Unit tests for interactive-cards feature (P0 URL sidebar + P1 markers)."""

from unittest.mock import patch

from feishu_bridge.ui import (
    extract_urls,
    to_sidebar_url,
    _url_label,
    _build_url_buttons,
    strip_action_markers,
    parse_action_markers,
    _build_action_buttons,
    build_cardkit_final_card,
    rebuild_card_with_selection,
    _card_cache,
    _get_git_label,
)


# ---------------------------------------------------------------------------
# extract_urls
# ---------------------------------------------------------------------------

class TestExtractUrls:
    def test_basic(self):
        urls = extract_urls("See https://github.com/foo and more")
        assert urls == ["https://github.com/foo"]

    def test_dedup(self):
        urls = extract_urls("https://a.com https://a.com https://b.com")
        assert urls == ["https://a.com", "https://b.com"]

    def test_excludes_image_urls(self):
        content = "Text ![alt](https://img.com/pic.png) and https://real.com"
        urls = extract_urls(content)
        assert urls == ["https://real.com"]

    def test_max_three(self):
        content = " ".join(f"https://{i}.com" for i in range(5))
        urls = extract_urls(content)
        assert len(urls) == 3

    def test_strips_trailing_punctuation(self):
        urls = extract_urls("Visit https://example.com.")
        assert urls == ["https://example.com"]

    def test_paren_preceded_url_excluded(self):
        """URLs preceded by ( are excluded (markdown link pattern)."""
        urls = extract_urls("(https://example.com)")
        assert urls == []

    def test_space_preceded_paren_url(self):
        urls = extract_urls("see https://example.com) more")
        assert urls == ["https://example.com"]

    def test_empty(self):
        assert extract_urls("no urls here") == []

    def test_http_ignored(self):
        """Only https:// URLs are extracted."""
        assert extract_urls("http://insecure.com") == []


# ---------------------------------------------------------------------------
# _url_label / to_sidebar_url
# ---------------------------------------------------------------------------

class TestUrlLabel:
    def test_short_url(self):
        label = _url_label("https://example.com/foo")
        assert label == "example.com/foo"

    def test_long_url_truncated(self):
        label = _url_label("https://very-long-hostname.example.com/very-long-path-segment-here")
        assert len(label) <= 40
        assert label.endswith("…")

    def test_root_path(self):
        label = _url_label("https://example.com/")
        assert label == "example.com"


class TestToSidebarUrl:
    def test_format(self):
        result = to_sidebar_url("https://github.com/foo")
        assert "mode=sidebar-semi" in result
        assert "applink.feishu.cn" in result
        assert "https%3A%2F%2Fgithub.com%2Ffoo" in result


# ---------------------------------------------------------------------------
# strip_action_markers
# ---------------------------------------------------------------------------

class TestStripActionMarkers:
    def test_strips_confirm(self):
        text = 'Hello <!-- feishu:confirm {"question":"ok?"} --> World'
        result = strip_action_markers(text)
        assert "feishu:" not in result
        assert "Hello" in result
        assert "World" in result

    def test_strips_choices(self):
        text = 'Pick one <!-- feishu:choices ["A","B"] -->'
        result = strip_action_markers(text)
        assert result.strip() == "Pick one"

    def test_no_markers(self):
        text = "Just plain text"
        assert strip_action_markers(text) == text

    def test_multiline_marker(self):
        text = 'Start <!-- feishu:ask\n{"question":"q","options":[]} \n--> End'
        result = strip_action_markers(text)
        assert "feishu:" not in result
        assert "Start" in result and "End" in result

    def test_preserves_normal_html_comments(self):
        text = "Hello <!-- not feishu --> World"
        result = strip_action_markers(text)
        assert "<!-- not feishu -->" in result


# ---------------------------------------------------------------------------
# parse_action_markers
# ---------------------------------------------------------------------------

class TestParseActionMarkers:
    def test_confirm(self):
        content = 'Text <!-- feishu:confirm {"question":"ok?"} -->'
        clean, markers = parse_action_markers(content)
        assert "feishu:" not in clean
        assert len(markers) == 1
        assert markers[0]["type"] == "confirm"
        assert markers[0]["payload"]["question"] == "ok?"

    def test_choices(self):
        content = '<!-- feishu:choices ["A","B","C"] -->'
        clean, markers = parse_action_markers(content)
        assert len(markers) == 1
        assert markers[0]["payload"] == ["A", "B", "C"]

    def test_ask(self):
        content = '<!-- feishu:ask {"question":"q","options":[{"label":"X"}]} -->'
        clean, markers = parse_action_markers(content)
        assert markers[0]["type"] == "ask"
        assert markers[0]["payload"]["options"][0]["label"] == "X"

    def test_multiple_markers(self):
        content = ('<!-- feishu:confirm {"question":"a"} --> '
                   '<!-- feishu:choices ["X"] -->')
        clean, markers = parse_action_markers(content)
        assert len(markers) == 2

    def test_malformed_json_skipped(self):
        content = '<!-- feishu:confirm {bad json} -->'
        clean, markers = parse_action_markers(content)
        assert len(markers) == 0
        assert "feishu:" not in clean

    def test_no_markers(self):
        clean, markers = parse_action_markers("plain text")
        assert clean == "plain text"
        assert markers == []


# ---------------------------------------------------------------------------
# _build_action_buttons
# ---------------------------------------------------------------------------

class TestBuildActionButtons:
    def test_confirm_generates_two_buttons(self):
        markers = [{"type": "confirm", "payload": {"question": "ok?"}}]
        buttons = _build_action_buttons(markers, "oc_123", "cli_abc")
        assert len(buttons) == 2
        assert "确认" in buttons[0]["text"]["content"]
        assert "取消" in buttons[1]["text"]["content"]

    def test_choices(self):
        markers = [{"type": "choices", "payload": ["A", "B", "C"]}]
        buttons = _build_action_buttons(markers, "oc_123", "cli_abc")
        assert len(buttons) == 3
        assert buttons[0]["value"]["label"] == "A"
        assert buttons[0]["value"]["chat_id"] == "oc_123"

    def test_ask_with_options(self):
        markers = [{"type": "ask", "payload": {
            "options": [{"label": "X"}, {"label": "Y"}]
        }}]
        buttons = _build_action_buttons(markers, "oc_123", "cli_abc")
        assert len(buttons) == 2

    def test_suppressed_without_chat_id(self):
        markers = [{"type": "confirm", "payload": {"question": "ok?"}}]
        assert _build_action_buttons(markers, None, "cli_abc") == []
        assert _build_action_buttons(markers, "oc_123", None) == []

    def test_empty_markers(self):
        assert _build_action_buttons([], "oc_123", "cli_abc") == []


# ---------------------------------------------------------------------------
# _build_url_buttons
# ---------------------------------------------------------------------------

class TestBuildUrlButtons:
    def test_generates_buttons(self):
        buttons = _build_url_buttons("Visit https://github.com/foo")
        assert len(buttons) == 1
        assert buttons[0]["tag"] == "button"
        assert "sidebar-semi" in buttons[0]["multi_url"]["pc_url"]
        # Mobile gets raw URL
        assert buttons[0]["multi_url"]["android_url"] == "https://github.com/foo"

    def test_no_urls(self):
        assert _build_url_buttons("no urls") == []


# ---------------------------------------------------------------------------
# build_cardkit_final_card integration
# ---------------------------------------------------------------------------

class TestBuildCardkitFinalCard:
    def test_with_markers_and_urls(self):
        card = build_cardkit_final_card(
            'Hello <!-- feishu:confirm {"question":"ok?"} --> https://github.com/x',
            chat_id="oc_1", bot_id="cli_1")
        elements = card["body"]["elements"]
        # markdown + action column_set + url column_set + footer
        assert len(elements) == 4
        assert elements[0]["tag"] == "markdown"
        assert elements[1]["tag"] == "column_set"  # action buttons
        assert elements[2]["tag"] == "column_set"  # url buttons

    def test_without_chat_id_no_action_buttons(self):
        card = build_cardkit_final_card(
            'Hello <!-- feishu:confirm {"question":"ok?"} --> https://github.com/x')
        elements = card["body"]["elements"]
        # markdown + url column_set + footer (no action buttons)
        assert len(elements) == 3

    def test_no_markers_no_urls(self):
        card = build_cardkit_final_card("Just text")
        elements = card["body"]["elements"]
        # markdown + footer only
        assert len(elements) == 2

    def test_markers_stripped_from_content(self):
        card = build_cardkit_final_card(
            'Before <!-- feishu:choices ["A"] --> After')
        md_content = card["body"]["elements"][0]["content"]
        assert "feishu:" not in md_content
        assert "Before" in md_content and "After" in md_content

    def test_footer_includes_model_name(self):
        card = build_cardkit_final_card("Hi", model_name="claude-opus-4-6")
        footer = card["body"]["elements"][-1]["content"]
        assert "opus-4-6" in footer
        # Should not contain the "claude-" prefix
        assert "claude-" not in footer

    @patch("feishu_bridge.ui._get_git_label", return_value="main*")
    def test_footer_includes_git_label(self, mock_git):
        card = build_cardkit_final_card("Hi", workspace="/tmp/fake")
        footer = card["body"]["elements"][-1]["content"]
        assert "main*" in footer

    def test_footer_full(self):
        """All footer fields present: ✅ · model · elapsed · tokens · git."""
        with patch("feishu_bridge.ui._get_git_label", return_value="dev"):
            card = build_cardkit_final_card(
                "Hi", elapsed_s=90, total_tokens=12345,
                model_name="claude-sonnet-4-6", workspace="/tmp/fake")
        footer = card["body"]["elements"][-1]["content"]
        assert "✅" in footer
        assert "sonnet-4-6" in footer
        assert "dev" in footer
        assert "1m30s" in footer
        assert "12.3k tokens" in footer

    def test_footer_no_model_no_workspace(self):
        """Footer gracefully omits model/git when not provided."""
        card = build_cardkit_final_card("Hi", elapsed_s=5)
        footer = card["body"]["elements"][-1]["content"]
        assert "✅" in footer
        assert "5.0s" in footer
        # No model or git info
        parts = footer.split(" · ")
        assert len(parts) == 2  # status + elapsed


# ---------------------------------------------------------------------------
# _get_git_label
# ---------------------------------------------------------------------------

class TestGetGitLabel:
    @patch("feishu_bridge.ui.subprocess.run")
    def test_clean_repo(self, mock_run):
        from subprocess import CompletedProcess
        mock_run.side_effect = [
            CompletedProcess([], 0, stdout="main\n", stderr=""),
            CompletedProcess([], 0, stdout="", stderr=""),
        ]
        assert _get_git_label("/tmp") == "main"

    @patch("feishu_bridge.ui.subprocess.run")
    def test_dirty_repo(self, mock_run):
        from subprocess import CompletedProcess
        mock_run.side_effect = [
            CompletedProcess([], 0, stdout="feat/ui\n", stderr=""),
            CompletedProcess([], 0, stdout=" M file.py\n", stderr=""),
        ]
        assert _get_git_label("/tmp") == "feat/ui*"

    @patch("feishu_bridge.ui.subprocess.run")
    def test_not_a_git_repo(self, mock_run):
        from subprocess import CompletedProcess
        mock_run.return_value = CompletedProcess([], 128, stdout="", stderr="not a repo")
        assert _get_git_label("/tmp") is None

    @patch("feishu_bridge.ui.subprocess.run", side_effect=OSError("no git"))
    def test_exception_returns_none(self, mock_run):
        assert _get_git_label("/tmp") is None


# ---------------------------------------------------------------------------
# Card cache + rebuild_card_with_selection
# ---------------------------------------------------------------------------

class TestCardCacheAndRebuild:
    def setup_method(self):
        _card_cache.clear()

    def test_card_ref_injected_into_buttons(self):
        """Action buttons include card_ref in their value."""
        card = build_cardkit_final_card(
            'Pick <!-- feishu:choices ["A","B"] -->',
            chat_id="oc_1", bot_id="cli_1")
        action_col = card["body"]["elements"][1]
        buttons = action_col["columns"][0]["elements"]
        assert len(buttons) == 2
        ref_a = buttons[0]["value"]["card_ref"]
        ref_b = buttons[1]["value"]["card_ref"]
        assert ref_a == ref_b  # same card_ref on all buttons
        assert len(ref_a) == 12

    def test_card_cached_on_build(self):
        """Building a card with action buttons populates the cache."""
        _card_cache.clear()
        card = build_cardkit_final_card(
            'Pick <!-- feishu:choices ["A","B"] -->',
            chat_id="oc_1", bot_id="cli_1")
        assert len(_card_cache) == 1
        ref = list(_card_cache.keys())[0]
        _, cached = _card_cache[ref]
        # Cached card should match structure
        assert cached["schema"] == "2.0"
        assert len(cached["body"]["elements"]) == len(card["body"]["elements"])

    def test_no_cache_without_action_buttons(self):
        """Cards without action buttons should not be cached."""
        _card_cache.clear()
        build_cardkit_final_card("Plain text")
        assert len(_card_cache) == 0

    def test_rebuild_preserves_content(self):
        """Rebuilt card keeps original markdown content and footer."""
        card = build_cardkit_final_card(
            'Hello world <!-- feishu:choices ["Yes","No"] -->',
            chat_id="oc_1", bot_id="cli_1")
        ref = card["body"]["elements"][1]["columns"][0]["elements"][0]["value"]["card_ref"]

        rebuilt = rebuild_card_with_selection(ref, "Yes")
        assert rebuilt is not None
        # Original markdown content preserved
        assert "Hello world" in rebuilt["body"]["elements"][0]["content"]
        # Footer preserved
        assert rebuilt["body"]["elements"][-1]["text_size"] == "notation"

    def test_rebuild_highlights_selected(self):
        """Selected button gets checkmark prefix + primary_filled."""
        card = build_cardkit_final_card(
            'Q <!-- feishu:choices ["Alpha","Beta"] -->',
            chat_id="oc_1", bot_id="cli_1")
        ref = card["body"]["elements"][1]["columns"][0]["elements"][0]["value"]["card_ref"]

        rebuilt = rebuild_card_with_selection(ref, "Alpha")
        buttons = rebuilt["body"]["elements"][1]["columns"][0]["elements"]
        # Selected
        assert buttons[0]["text"]["content"] == "✅ Alpha"
        assert buttons[0]["type"] == "primary_filled"
        assert buttons[0]["disabled"] is True
        assert "value" not in buttons[0]
        # Unselected
        assert buttons[1]["text"]["content"] == "Beta"  # no prefix
        assert buttons[1]["type"] == "default"
        assert buttons[1]["disabled"] is True

    def test_rebuild_sets_update_multi(self):
        """Rebuilt card has update_multi config for callback response."""
        card = build_cardkit_final_card(
            'Q <!-- feishu:confirm {} -->',
            chat_id="oc_1", bot_id="cli_1")
        ref = card["body"]["elements"][1]["columns"][0]["elements"][0]["value"]["card_ref"]

        rebuilt = rebuild_card_with_selection(ref, "确认")
        assert rebuilt["config"] == {"update_multi": True}

    def test_rebuild_confirm_no_double_emoji(self):
        """Confirm buttons already have ✅/❌ — rebuild must not double-prefix."""
        card = build_cardkit_final_card(
            'Sure? <!-- feishu:confirm {} -->',
            chat_id="oc_1", bot_id="cli_1")
        ref = card["body"]["elements"][1]["columns"][0]["elements"][0]["value"]["card_ref"]

        rebuilt = rebuild_card_with_selection(ref, "确认")
        buttons = rebuilt["body"]["elements"][1]["columns"][0]["elements"]
        assert buttons[0]["text"]["content"] == "✅ 确认"  # not "✅ ✅ 确认"
        assert buttons[1]["text"]["content"] == "❌ 取消"  # unchanged

    def test_rebuild_cache_miss(self):
        """Missing card_ref returns None."""
        assert rebuild_card_with_selection("nonexistent", "X") is None

    def test_rebuild_consumes_cache_entry(self):
        """Cache entry is removed after rebuild (one-time use)."""
        card = build_cardkit_final_card(
            'Q <!-- feishu:choices ["A"] -->',
            chat_id="oc_1", bot_id="cli_1")
        ref = card["body"]["elements"][1]["columns"][0]["elements"][0]["value"]["card_ref"]

        assert rebuild_card_with_selection(ref, "A") is not None
        assert rebuild_card_with_selection(ref, "A") is None  # consumed

    def test_rebuild_expired_entry(self):
        """Expired cache entries return None."""
        import time as _time
        build_cardkit_final_card(
            'Q <!-- feishu:choices ["A"] -->',
            chat_id="oc_1", bot_id="cli_1")
        ref = list(_card_cache.keys())[0]
        # Manually expire
        _, cached_card = _card_cache[ref]
        _card_cache[ref] = (_time.time() - 1, cached_card)

        assert rebuild_card_with_selection(ref, "A") is None
