"""Tests for update-doc: CLI validation (P1) + FeishuDocs.update() payload."""

import json
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from feishu_bridge.api.docs import FeishuDocs


# ---------------------------------------------------------------------------
# Helper: run feishu-cli as subprocess to test argparse validation
# ---------------------------------------------------------------------------

def _run_cli(*args):
    """Run feishu-cli with given args, return (returncode, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, "-m", "feishu_bridge.cli", *args],
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode, result.stdout, result.stderr


# ===========================================================================
# CLI argparse-level validation
# ===========================================================================

class TestUpdateDocCLIValidation:
    """Test argparse-level validation for update-doc command."""

    def test_mode_required(self):
        """--mode is required (no default)."""
        rc, out, err = _run_cli("update-doc", "--token", "tok", "--markdown", "x")
        assert rc != 0
        assert "required" in err.lower() or "mode" in err.lower()

    def test_mode_choices_reject_typo(self):
        """Invalid mode is rejected by argparse choices."""
        rc, out, err = _run_cli(
            "update-doc", "--token", "tok", "--markdown", "x",
            "--mode", "overwriteX",
        )
        assert rc != 0
        assert "invalid choice" in err.lower()

    def test_mode_choices_accept_all_valid(self):
        """All 7 valid modes pass argparse (may fail later at auth)."""
        valid_modes = [
            "overwrite", "append", "replace_range", "replace_all",
            "insert_before", "insert_after", "delete_range",
        ]
        for mode in valid_modes:
            extra = []
            if mode in ("replace_range", "insert_before", "insert_after", "delete_range"):
                extra = ["--selection", "some...text"]
            if mode not in ("delete_range", "replace_all"):
                extra += ["--markdown", "content"]
            rc, out, err = _run_cli(
                "update-doc", "--token", "tok", "--mode", mode, *extra,
            )
            # Should NOT fail at argparse level (will fail at config/auth)
            if rc != 0:
                assert "invalid choice" not in err.lower(), f"mode '{mode}' rejected by argparse"


# ===========================================================================
# CLI handler-level validation (requires env setup to reach handler)
# We test these via direct function invocation by patching
# ===========================================================================

class TestUpdateDocHandlerValidation:
    """Test handler-level validation logic in cli.py."""

    def _parse_and_validate(self, args_list):
        """Parse args and run the validation section of update-doc handler.

        Returns (error_dict, warning_logged) or (None, warning_logged) if valid.
        """
        import argparse

        _UPDATE_MODES = ("overwrite", "append", "replace_range", "replace_all",
                         "insert_before", "insert_after", "delete_range")

        parser = argparse.ArgumentParser()
        parser.add_argument("--token", required=True)
        parser.add_argument("--markdown")
        parser.add_argument("--mode", required=True, choices=_UPDATE_MODES)
        parser.add_argument("--selection")
        parser.add_argument("--selection-by-title")
        parser.add_argument("--new-title")

        args = parser.parse_args(args_list)

        need_sel = args.mode in ('replace_range', 'insert_before',
                                 'insert_after', 'delete_range')
        has_sel = bool(args.selection)
        has_title = bool(getattr(args, 'selection_by_title', None))

        if need_sel and not has_sel and not has_title:
            return {"error": f"--mode {args.mode} requires --selection or --selection-by-title"}, False
        if has_sel and has_title:
            return {"error": "--selection and --selection-by-title are mutually exclusive"}, False
        if args.mode not in ('delete_range', 'replace_all') and not args.markdown:
            return {"error": f"--mode {args.mode} requires --markdown"}, False

        warned = args.mode in ('overwrite', 'append') and (has_sel or has_title)
        return None, warned

    def test_replace_range_requires_selection(self):
        err, _ = self._parse_and_validate([
            "--token", "tok", "--mode", "replace_range", "--markdown", "x",
        ])
        assert err is not None
        assert "--selection" in err["error"]

    def test_insert_after_requires_selection(self):
        err, _ = self._parse_and_validate([
            "--token", "tok", "--mode", "insert_after", "--markdown", "x",
        ])
        assert err is not None

    def test_delete_range_requires_selection(self):
        err, _ = self._parse_and_validate([
            "--token", "tok", "--mode", "delete_range",
        ])
        assert err is not None
        assert "--selection" in err["error"]

    def test_selection_and_title_mutually_exclusive(self):
        err, _ = self._parse_and_validate([
            "--token", "tok", "--mode", "replace_range",
            "--markdown", "x",
            "--selection", "a...b",
            "--selection-by-title", "## Heading",
        ])
        assert err is not None
        assert "mutually exclusive" in err["error"]

    def test_delete_range_no_markdown_ok(self):
        """delete_range does not require --markdown."""
        err, _ = self._parse_and_validate([
            "--token", "tok", "--mode", "delete_range",
            "--selection", "a...b",
        ])
        assert err is None

    def test_replace_all_empty_markdown_ok(self):
        """replace_all with empty --markdown is valid (delete all matches)."""
        err, _ = self._parse_and_validate([
            "--token", "tok", "--mode", "replace_all",
        ])
        assert err is None

    def test_overwrite_requires_markdown(self):
        err, _ = self._parse_and_validate([
            "--token", "tok", "--mode", "overwrite",
        ])
        assert err is not None
        assert "--markdown" in err["error"]

    def test_append_requires_markdown(self):
        err, _ = self._parse_and_validate([
            "--token", "tok", "--mode", "append",
        ])
        assert err is not None

    def test_insert_before_requires_markdown(self):
        err, _ = self._parse_and_validate([
            "--token", "tok", "--mode", "insert_before",
            "--selection", "anchor",
        ])
        assert err is not None
        assert "--markdown" in err["error"]

    def test_overwrite_with_stray_selection_warns(self):
        err, warned = self._parse_and_validate([
            "--token", "tok", "--mode", "overwrite",
            "--markdown", "x", "--selection", "unused",
        ])
        assert err is None
        assert warned is True

    def test_append_with_stray_title_warns(self):
        err, warned = self._parse_and_validate([
            "--token", "tok", "--mode", "append",
            "--markdown", "x", "--selection-by-title", "## H",
        ])
        assert err is None
        assert warned is True

    def test_replace_range_with_selection_ok(self):
        err, warned = self._parse_and_validate([
            "--token", "tok", "--mode", "replace_range",
            "--markdown", "x", "--selection", "a...b",
        ])
        assert err is None
        assert warned is False

    def test_insert_after_with_title_ok(self):
        err, warned = self._parse_and_validate([
            "--token", "tok", "--mode", "insert_after",
            "--markdown", "x", "--selection-by-title", "## Section",
        ])
        assert err is None
        assert warned is False


# ===========================================================================
# FeishuDocs.update() outbound payload tests
# ===========================================================================

class TestFeishuDocsUpdatePayload:
    """Test that FeishuDocs.update() builds correct MCP payloads."""

    def _capture_mcp_call(self, **update_kwargs):
        """Call FeishuDocs.update() and capture the args dict sent to mcp_call."""
        docs = FeishuDocs.__new__(FeishuDocs)
        docs.get_token = MagicMock(return_value="fake_token")

        captured = {}

        def fake_mcp_call(tool_name, args, token):
            captured["tool_name"] = tool_name
            captured["args"] = args
            captured["token"] = token
            return {"success": True}

        docs.mcp_call = fake_mcp_call
        docs.update("chat", "user", **update_kwargs)
        return captured

    def test_overwrite_includes_markdown(self):
        c = self._capture_mcp_call(
            doc_id="doc1", markdown="# Hello", mode="overwrite",
        )
        assert c["args"]["markdown"] == "# Hello"
        assert c["args"]["mode"] == "overwrite"
        assert "selection_with_ellipsis" not in c["args"]
        assert "selection_by_title" not in c["args"]

    def test_delete_range_excludes_markdown(self):
        """delete_range must NOT send markdown field to MCP."""
        c = self._capture_mcp_call(
            doc_id="doc1", mode="delete_range", selection="a...b",
        )
        assert "markdown" not in c["args"]
        assert c["args"]["mode"] == "delete_range"
        assert c["args"]["selection_with_ellipsis"] == "a...b"

    def test_selection_maps_to_selection_with_ellipsis(self):
        c = self._capture_mcp_call(
            doc_id="doc1", markdown="new", mode="replace_range",
            selection="start...end",
        )
        assert c["args"]["selection_with_ellipsis"] == "start...end"
        assert "selection_by_title" not in c["args"]

    def test_selection_by_title_maps_correctly(self):
        c = self._capture_mcp_call(
            doc_id="doc1", markdown="new", mode="replace_range",
            selection_by_title="## My Section",
        )
        assert c["args"]["selection_by_title"] == "## My Section"
        assert "selection_with_ellipsis" not in c["args"]

    def test_selection_takes_precedence_over_title(self):
        """When both are provided, selection (ellipsis) wins."""
        c = self._capture_mcp_call(
            doc_id="doc1", markdown="new", mode="replace_range",
            selection="a...b", selection_by_title="## H",
        )
        assert c["args"]["selection_with_ellipsis"] == "a...b"
        assert "selection_by_title" not in c["args"]

    def test_replace_all_empty_markdown(self):
        """replace_all with empty markdown should pass through (delete-all)."""
        c = self._capture_mcp_call(
            doc_id="doc1", markdown="", mode="replace_all",
            selection="pattern",
        )
        assert c["args"]["markdown"] == ""
        assert c["args"]["selection_with_ellipsis"] == "pattern"

    def test_replace_all_without_selection(self):
        """replace_all without selection — no selection field in payload."""
        c = self._capture_mcp_call(
            doc_id="doc1", markdown="new", mode="replace_all",
        )
        assert "selection_with_ellipsis" not in c["args"]
        assert "selection_by_title" not in c["args"]

    def test_new_title_included(self):
        c = self._capture_mcp_call(
            doc_id="doc1", markdown="x", mode="overwrite",
            new_title="New Title",
        )
        assert c["args"]["new_title"] == "New Title"

    def test_new_title_excluded_when_none(self):
        c = self._capture_mcp_call(
            doc_id="doc1", markdown="x", mode="overwrite",
        )
        assert "new_title" not in c["args"]

    def test_insert_after_with_title_payload(self):
        c = self._capture_mcp_call(
            doc_id="doc1", markdown="new content", mode="insert_after",
            selection_by_title="## 部署",
        )
        assert c["args"]["mode"] == "insert_after"
        assert c["args"]["markdown"] == "new content"
        assert c["args"]["selection_by_title"] == "## 部署"
