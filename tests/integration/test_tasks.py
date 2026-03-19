#!/usr/bin/env python3
"""
Quick test for Feishu Task API with auto-auth.

Run: python3 -m pytest tests/integration/test_tasks.py -v --no-header -s

Requires env vars:
  FEISHU_APP_ID, FEISHU_APP_SECRET,
  FEISHU_TEST_CHAT_ID, FEISHU_TEST_USER_OPEN_ID
"""

import logging
import os
import sys

import pytest

try:
    import lark_oapi as lark
except ImportError:
    pytest.skip("lark-oapi not installed", allow_module_level=True)

from feishu_bridge.api.tasks import FeishuTasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
CHAT_ID = os.environ.get("FEISHU_TEST_CHAT_ID", "")
USER_OPEN_ID = os.environ.get("FEISHU_TEST_USER_OPEN_ID", "")

pytestmark = pytest.mark.skipif(
    not all([APP_ID, APP_SECRET, CHAT_ID, USER_OPEN_ID]),
    reason="Missing FEISHU_* env vars for integration testing",
)


@pytest.fixture(scope="module")
def tasks_client():
    lark_client = lark.Client.builder() \
        .app_id(APP_ID) \
        .app_secret(APP_SECRET) \
        .domain(lark.FEISHU_DOMAIN) \
        .log_level(lark.LogLevel.WARNING) \
        .build()
    return FeishuTasks(APP_ID, APP_SECRET, lark_client)


def test_list_tasklists(tasks_client):
    result = tasks_client.list_tasklists(CHAT_ID, USER_OPEN_ID)
    assert "error" not in result or result["error"] != "auth_failed"
    assert "items" in result


def test_list_active_tasks(tasks_client):
    result = tasks_client.list_tasks(CHAT_ID, USER_OPEN_ID, completed=False)
    assert "items" in result


def test_summary(tasks_client):
    summary = tasks_client.summary(CHAT_ID, USER_OPEN_ID)
    assert isinstance(summary, str)
    assert len(summary) > 0
