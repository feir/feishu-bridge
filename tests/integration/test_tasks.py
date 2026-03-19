#!/usr/bin/env python3
"""
Quick test for Feishu Task API with auto-auth.

Run: python3 ~/.claude/scripts/test_feishu_tasks.py

This will:
1. Initialize FeishuAuth + FeishuTasks with the bridge's app credentials
2. Try to list task lists (triggers OAuth card if no cached token)
3. Print results
"""

import logging
import os
import sys

# Add scripts dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.expanduser("~/.claude/.env"))

import lark_oapi as lark  # noqa: E402
from feishu_tasks import FeishuTasks  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

# --- Config from env ---
APP_ID = os.environ["FEISHU_APP_ID"]
APP_SECRET = os.environ["FEISHU_APP_SECRET"]

# Chat ID and user open_id — fill these in for testing
# You can find these from the bridge logs or Feishu webhook events
CHAT_ID = os.environ.get("FEISHU_TEST_CHAT_ID", "")
USER_OPEN_ID = os.environ.get("FEISHU_TEST_USER_OPEN_ID", "")


def main():
    if not CHAT_ID or not USER_OPEN_ID:
        print("Error: set FEISHU_TEST_CHAT_ID and FEISHU_TEST_USER_OPEN_ID env vars")
        print("  You can find these in the bridge logs (journalctl -u feishu-bridge@claude-code)")
        sys.exit(1)

    # Build lark client (same as bridge)
    lark_client = lark.Client.builder() \
        .app_id(APP_ID) \
        .app_secret(APP_SECRET) \
        .domain(lark.FEISHU_DOMAIN) \
        .log_level(lark.LogLevel.WARNING) \
        .build()

    tasks = FeishuTasks(APP_ID, APP_SECRET, lark_client)

    print("\n--- Testing Feishu Tasks API ---")
    print(f"App ID: {APP_ID[:8]}...")
    print(f"Chat ID: {CHAT_ID[:8]}...")
    print(f"User: {USER_OPEN_ID[:8]}...")
    print()

    # Test 1: List task lists
    print("1. Listing task lists...")
    try:
        result = tasks.list_tasklists(CHAT_ID, USER_OPEN_ID)
        if "error" in result:
            print(f"   Auth failed: {result['error']}")
            print("   (Check the Feishu chat for an authorization card)")
            return
        items = result.get("items", [])
        print(f"   Found {len(items)} task list(s)")
        for tl in items:
            print(f"   • {tl.get('name', '?')} (guid={tl.get('guid', '?')[:12]}...)")
    except Exception as e:
        print(f"   Error: {e}")
        return

    # Test 2: List active tasks
    print("\n2. Listing active tasks...")
    try:
        result = tasks.list_tasks(CHAT_ID, USER_OPEN_ID, completed=False)
        items = result.get("items", [])
        print(f"   Found {len(items)} active task(s)")
        for t in items[:10]:
            print(f"   • {t.get('summary', '?')}")
    except Exception as e:
        print(f"   Error: {e}")
        return

    # Test 3: Summary
    print("\n3. Full summary:")
    print(tasks.summary(CHAT_ID, USER_OPEN_ID))


if __name__ == "__main__":
    main()
