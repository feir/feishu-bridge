#!/usr/bin/env python3
"""Unit tests for Feishu Mail module and CLI integration."""

import json
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from feishu_bridge.api.mail import FeishuMail, MAX_ATTACHMENT_SIZE, MAX_TOTAL_ATTACHMENT_SIZE
from feishu_bridge.api.client import FeishuAPIError


# ---------------------------------------------------------------------------
# FeishuMail API tests
# ---------------------------------------------------------------------------

class TestFeishuMailInit:
    """Test mail module instantiation and attributes."""

    def test_scopes_defined(self):
        assert len(FeishuMail.SCOPES) == 9
        assert all(s.startswith("mail:") for s in FeishuMail.SCOPES)

    def test_base_path(self):
        assert FeishuMail.BASE_PATH == "/open-apis/mail/v1"


class TestSendMessage:
    """Test send_message logic (mocked HTTP)."""

    def _make_mail(self):
        m = object.__new__(FeishuMail)
        m.get_token = MagicMock(return_value="fake-token")
        m.request = MagicMock(return_value={"message_id": "msg1"})
        return m

    def test_basic_send(self):
        m = self._make_mail()
        result = m.send_message(
            "chat", "user",
            to=[{"mail_address": "a@b.com"}],
            subject="Hi",
            body_html="<p>Hello</p>",
        )
        assert result == {"message_id": "msg1"}
        call_args = m.request.call_args
        payload = call_args.kwargs.get("json_body") or call_args[1].get("json_body")
        assert payload["subject"] == "Hi"
        assert payload["to"] == [{"mail_address": "a@b.com"}]
        assert "dedupe_key" in payload
        assert len(payload["dedupe_key"]) == 36  # UUID format

    def test_dedupe_key_unique_per_call(self):
        m = self._make_mail()
        m.send_message("c", "u", to=[{"mail_address": "a@b.com"}],
                       subject="1", body_html="x")
        key1 = m.request.call_args.kwargs["json_body"]["dedupe_key"]
        m.send_message("c", "u", to=[{"mail_address": "a@b.com"}],
                       subject="2", body_html="y")
        key2 = m.request.call_args.kwargs["json_body"]["dedupe_key"]
        assert key1 != key2

    def test_from_address(self):
        m = self._make_mail()
        m.send_message("c", "u", to=[{"mail_address": "a@b.com"}],
                       subject="S", body_html="B",
                       from_address="alias@example.com",
                       from_name="Alias")
        payload = m.request.call_args.kwargs["json_body"]
        assert payload["head_from"]["mail_address"] == "alias@example.com"
        assert payload["head_from"]["name"] == "Alias"

    def test_body_plain(self):
        m = self._make_mail()
        m.send_message("c", "u", to=[{"mail_address": "a@b.com"}],
                       subject="S", body_plain="plain text")
        payload = m.request.call_args.kwargs["json_body"]
        assert payload["body_plain_text"] == "plain text"
        assert "body_html" not in payload

    def test_attachment_not_found(self):
        m = self._make_mail()
        with pytest.raises(ValueError, match="Attachment not found"):
            m.send_message("c", "u", to=[{"mail_address": "a@b.com"}],
                           subject="S", body_html="B",
                           attachment_paths=["/nonexistent/file.pdf"])

    def test_attachment_too_large(self):
        m = self._make_mail()
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x" * (MAX_ATTACHMENT_SIZE + 1))
            path = f.name
        try:
            with pytest.raises(ValueError, match="too large"):
                m.send_message("c", "u", to=[{"mail_address": "a@b.com"}],
                               subject="S", body_html="B",
                               attachment_paths=[path])
        finally:
            os.unlink(path)

    def test_attachment_encoding(self):
        """Verify base64url encoding (no + / =)."""
        m = self._make_mail()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            # Write bytes that produce +/= in standard base64
            f.write(b"\xff\xfe\xfd")
            path = f.name
        try:
            m.send_message("c", "u", to=[{"mail_address": "a@b.com"}],
                           subject="S", body_html="B",
                           attachment_paths=[path])
            payload = m.request.call_args.kwargs["json_body"]
            body = payload["attachments"][0]["body"]
            assert "+" not in body
            assert "/" not in body
            assert "=" not in body
            assert payload["attachments"][0]["filename"] == os.path.basename(path)
            # Round-trip: decode back to original bytes
            import base64
            pad = 4 - len(body) % 4
            decoded = base64.urlsafe_b64decode(body + "=" * (pad % 4))
            assert decoded == b"\xff\xfe\xfd"
        finally:
            os.unlink(path)

    def test_total_attachment_size_exceeded(self):
        """Three files each under 25MB but combined > 50MB."""
        m = self._make_mail()
        files = []
        try:
            for _ in range(3):
                f = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
                # 18MB each — individually OK (<25MB), combined 54MB > 50MB
                f.write(b"x" * (18 * 1024 * 1024))
                f.close()
                files.append(f.name)
            with pytest.raises(ValueError, match="Total attachment size"):
                m.send_message("c", "u", to=[{"mail_address": "a@b.com"}],
                               subject="S", body_html="B",
                               attachment_paths=files)
        finally:
            for path in files:
                os.unlink(path)

    def test_no_body_raises(self):
        """send_message() rejects calls with neither body_html nor body_plain."""
        m = self._make_mail()
        with pytest.raises(ValueError, match="body_html or body_plain"):
            m.send_message("c", "u", to=[{"mail_address": "a@b.com"}],
                           subject="S")

    def test_auth_returns_none(self):
        m = object.__new__(FeishuMail)
        m.get_token = MagicMock(return_value=None)
        result = m.send_message("c", "u", to=[{"mail_address": "a@b.com"}],
                                subject="S", body_html="B")
        assert result is None


class TestFolderResolution:
    """Test _resolve_folder_id logic."""

    def _make_mail_with_folders(self, folders):
        m = object.__new__(FeishuMail)
        m.request = MagicMock(return_value={"folders": folders})
        return m

    def test_resolve_by_name_case_insensitive(self):
        m = self._make_mail_with_folders([
            {"id": "f1", "name": "INBOX"},
            {"id": "f2", "name": "Sent"},
        ])
        assert m._resolve_folder_id("tok", "inbox") == "f1"
        assert m._resolve_folder_id("tok", "SENT") == "f2"

    def test_literal_fallback(self):
        m = self._make_mail_with_folders([
            {"id": "f1", "name": "INBOX"},
        ])
        # "unknown_id" doesn't match any name, used as literal
        assert m._resolve_folder_id("tok", "unknown_id") == "unknown_id"

    def test_api_error_propagates(self):
        m = object.__new__(FeishuMail)
        m.request = MagicMock(
            side_effect=FeishuAPIError(403, "Forbidden", "/folders")
        )
        with pytest.raises(FeishuAPIError, match="403"):
            m._resolve_folder_id("tok", "INBOX")


# ---------------------------------------------------------------------------
# CLI argparse tests
# ---------------------------------------------------------------------------

class TestMailCLIArgs:
    """Test CLI argument parsing for mail commands."""

    @pytest.fixture(autouse=True)
    def _patch_env(self, tmp_path, monkeypatch):
        """Set up minimal auth/config for CLI."""
        # Create auth file
        auth_file = tmp_path / "auth.json"
        auth_file.write_text(json.dumps({
            "chat_id": "chat1", "sender_id": "user1",
            "user_access_token": "tok1",
        }))
        monkeypatch.setenv("FEISHU_AUTH_FILE", str(auth_file))

        # Create config
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "bots": [{"name": "test", "app_id": "a1", "app_secret": "s1"}],
        }))
        monkeypatch.setenv("FEISHU_BOT_NAME", "test")
        monkeypatch.setenv("FEISHU_CONFIG_FILE", str(config_file))

    def _parse(self, args_list):
        """Import and run argparse without dispatching."""
        from feishu_bridge.cli import main
        import argparse
        with pytest.raises(SystemExit):
            # We just want to test parsing; capture the parse result
            with patch("sys.argv", ["feishu-cli"] + args_list + ["--help"]):
                main()

    def test_send_mail_requires_to_and_subject(self):
        """Verify send-mail has required args."""
        with pytest.raises(SystemExit):
            with patch("sys.argv", ["feishu-cli", "send-mail"]):
                from feishu_bridge.cli import main
                main()

    def test_delete_mail_rule_confirm_guard(self):
        """Verify delete-mail-rule requires --confirm."""
        with pytest.raises(SystemExit):
            with patch("sys.argv", ["feishu-cli", "delete-mail-rule",
                                    "--rule-id", "42"]):
                from feishu_bridge.cli import main
                main()


class TestConfirmGuardIntRuleId:
    """Test _confirm_guard behavior with int rule_id."""

    def test_int_rule_id_prefix_match(self):
        from feishu_bridge.cli import _confirm_guard

        class FakeArgs:
            confirm = "4"

        # Should not raise — "42" starts with "4"
        _confirm_guard(FakeArgs(), "42", "rule_id")

    def test_int_rule_id_prefix_mismatch(self, capsys):
        from feishu_bridge.cli import _confirm_guard

        class FakeArgs:
            confirm = "5"

        with pytest.raises(SystemExit):
            _confirm_guard(FakeArgs(), "42", "rule_id")
