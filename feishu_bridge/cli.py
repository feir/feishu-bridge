#!/usr/bin/env python3
"""
Unified CLI entry point for Feishu API operations.

Called by Claude via Bash tool. Reads credentials from bridge config
and user token from auth file (passed via FEISHU_AUTH_FILE env var).

Usage:
    feishu-cli <command> [args...]
    feishu-cli search-docs --query "quarterly report"
    feishu-cli delete-doc --token doxcnXXX --confirm doxcn
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

from feishu_bridge.api.docs import FeishuDocs
from feishu_bridge.api.sheets import FeishuSheets
from feishu_bridge.api.wiki import FeishuWiki
from feishu_bridge.api.comments import FeishuComments
from feishu_bridge.api.calendar import FeishuCalendar
from feishu_bridge.api.search import FeishuSearch
from feishu_bridge.api.bitable import FeishuBitable
from feishu_bridge.api.tasks import FeishuTasks
from feishu_bridge.api.drive import FeishuDrive


def _parse_due(value: str) -> str:
    """Parse a due date string into Unix milliseconds for the Feishu Task API.

    Accepts:
      - Human-readable: "2026-03-31", "2026-03-31 23:59", "2026-03-31 23:59:00"
        (interpreted as **local timezone**; date-only defaults to 23:59:59)
      - Unix seconds (10 digits): "1775001599" → auto-converted to ms
      - Unix milliseconds (13 digits): "1775001599000" → passed through

    Returns: string of Unix timestamp in milliseconds.
    Raises ValueError on unparseable input.
    """
    from datetime import datetime, timezone

    value = value.strip()

    # Pure numeric → detect seconds (10 digits) vs milliseconds (13 digits)
    if value.isdigit():
        n = len(value)
        if n <= 10:
            return str(int(value) * 1000)  # seconds → ms
        if n == 13:
            return value  # already ms
        raise ValueError(
            f"Ambiguous numeric timestamp ({n} digits): {value!r}. "
            "Use 10-digit (seconds) or 13-digit (milliseconds).")

    # Try human-readable date formats (local timezone)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            # If no time specified, default to end of day
            if fmt == "%Y-%m-%d":
                dt = dt.replace(hour=23, minute=59, second=59)
            # Use local timezone so the date displays correctly in user's Feishu
            dt = dt.astimezone()
            return str(int(dt.timestamp() * 1000))
        except ValueError:
            continue

    raise ValueError(f"Cannot parse due date: {value!r}. "
                     "Use YYYY-MM-DD, 'YYYY-MM-DD HH:MM', or Unix timestamp.")


def _safe_parse_due(value: str | None) -> str | None:
    """Parse --due value, returning None if absent or exiting with JSON error."""
    if not value:
        return None
    try:
        return _parse_due(value)
    except ValueError as e:
        _output({"error": str(e)})
        sys.exit(1)


def _load_auth():
    """Load auth from FEISHU_AUTH_FILE env var."""
    auth_path = os.environ.get("FEISHU_AUTH_FILE")
    if not auth_path:
        print(json.dumps({"error": "FEISHU_AUTH_FILE not set"}))
        sys.exit(1)

    try:
        with open(auth_path) as f:
            auth = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"Failed to read auth file: {e}"}))
        sys.exit(1)

    return auth


def _load_config():
    """Load app credentials from bridge config."""
    bot_name = os.environ.get("FEISHU_BOT_NAME")

    # Use shared config discovery chain
    from feishu_bridge.config import resolve_config_path
    try:
        config_path = resolve_config_path()
    except SystemExit:
        print(json.dumps({"error": "No config file found. Set $FEISHU_BRIDGE_CONFIG or create ~/.config/feishu-bridge/config.json"}))
        sys.exit(1)

    try:
        with open(config_path) as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"Failed to read config: {e}"}))
        sys.exit(1)

    # Find the matching bot config
    for bot in config.get("bots", []):
        if bot_name and bot.get("name") != bot_name:
            continue
        app_id = os.path.expandvars(bot.get("app_id", bot.get("feishu_app_id", "")))
        app_secret = os.path.expandvars(bot.get("app_secret", bot.get("feishu_app_secret", "")))
        if app_id and app_secret:
            return {"app_id": app_id, "app_secret": app_secret}

    # Fallback: use first bot
    if config.get("bots"):
        bot = config["bots"][0]
        app_id = os.path.expandvars(bot.get("app_id", bot.get("feishu_app_id", "")))
        app_secret = os.path.expandvars(bot.get("app_secret", bot.get("feishu_app_secret", "")))
        if app_id and app_secret:
            return {"app_id": app_id, "app_secret": app_secret}

    print(json.dumps({"error": "No bot config found"}))
    sys.exit(1)


def _init_module(cls, config, user_token=None, lark_client=None):
    """Initialize a FeishuAPI module with app credentials.

    When user_token is provided, it is used directly (no auth flow).
    When user_token is None and lark_client is set, the module can
    trigger on-demand OAuth via Device Flow (sends auth card to chat).
    """
    return cls(config["app_id"], config["app_secret"],
               lark_client=lark_client, token_override=user_token)


def _output(result):
    """Print result as JSON."""
    if result is None:
        print(json.dumps({"error": "Auth failed — authorization card sent"}))
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, default=str))


def _confirm_guard(args, token_value: str, resource_name: str):
    """Verify --confirm matches token prefix for delete safety."""
    confirm = getattr(args, "confirm", None)
    if not confirm:
        print(json.dumps({
            "error": f"Delete requires --confirm <{resource_name}_prefix>. "
                     f"The token starts with: {token_value[:6]}..."
        }))
        sys.exit(1)

    if not token_value.startswith(confirm):
        print(json.dumps({
            "error": f"--confirm value '{confirm}' does not match token prefix. "
                     f"Token starts with: {token_value[:6]}..."
        }))
        sys.exit(1)


def _safe_json_loads(value: str, param_name: str):
    """Parse JSON with structured error output."""
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError) as e:
        print(json.dumps({"error": f"Invalid JSON for {param_name}: {e}"}))
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Feishu CLI — unified entry for Feishu API operations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- Doc ---
    p = sub.add_parser("read-doc", help="Read a document as Markdown")
    p.add_argument("--token", required=True, help="Document token or URL")

    p = sub.add_parser("create-doc", help="Create a new document")
    p.add_argument("--title", required=True)
    p.add_argument("--markdown", default="")
    p.add_argument("--folder-token")
    p.add_argument("--wiki-space")

    p = sub.add_parser("update-doc", help="Update document content")
    p.add_argument("--token", required=True)
    p.add_argument("--markdown")
    _UPDATE_MODES = ("overwrite", "append", "replace_range", "replace_all",
                     "insert_before", "insert_after", "delete_range")
    p.add_argument("--mode", required=True, choices=_UPDATE_MODES)
    p.add_argument("--selection", help="Text selection (ellipsis format)")
    p.add_argument("--selection-by-title", help="Section heading selection")
    p.add_argument("--new-title")

    p = sub.add_parser("delete-doc", help="Delete a document")
    p.add_argument("--token", required=True)
    p.add_argument("--confirm", required=True, help="Token prefix for safety")

    # --- Sheet ---
    p = sub.add_parser("read-sheet", help="Read spreadsheet data")
    p.add_argument("--token", required=True, help="Spreadsheet token")
    p.add_argument("--range", required=True, help="A1 notation range")

    p = sub.add_parser("sheet-info", help="Get spreadsheet metadata")
    p.add_argument("--token", required=True)

    p = sub.add_parser("write-sheet", help="Write data to spreadsheet")
    p.add_argument("--token", required=True)
    p.add_argument("--range", required=True)
    p.add_argument("--values", required=True, help="JSON 2D array")

    p = sub.add_parser("append-sheet", help="Append rows to spreadsheet")
    p.add_argument("--token", required=True)
    p.add_argument("--range", required=True)
    p.add_argument("--values", required=True, help="JSON 2D array")

    p = sub.add_parser("create-sheet", help="Create a new spreadsheet")
    p.add_argument("--title", required=True)
    p.add_argument("--folder-token")

    p = sub.add_parser("delete-sheet", help="Delete a spreadsheet")
    p.add_argument("--token", required=True)
    p.add_argument("--confirm", required=True)

    # --- Wiki ---
    p = sub.add_parser("list-wiki-spaces", help="List wiki spaces")
    p.add_argument("--page-size", type=int, default=20)
    p.add_argument("--page-token")

    p = sub.add_parser("list-wiki-nodes", help="List nodes in a wiki space")
    p.add_argument("--space-id", required=True)
    p.add_argument("--parent-node-token")
    p.add_argument("--page-size", type=int, default=20)
    p.add_argument("--page-token")

    p = sub.add_parser("get-wiki-node", help="Get wiki node info")
    p.add_argument("--token", required=True, help="Wiki node token")

    p = sub.add_parser("create-wiki-node", help="Create a wiki node")
    p.add_argument("--space-id", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--obj-type", default="doc")
    p.add_argument("--parent-node-token")

    p = sub.add_parser("delete-wiki-node", help="Delete a wiki node")
    p.add_argument("--space-id", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--confirm", required=True)

    # --- Comments ---
    p = sub.add_parser("list-comments", help="List file comments")
    p.add_argument("--file-token", required=True)
    p.add_argument("--file-type", default="docx")
    p.add_argument("--is-solved", choices=["true", "false"])
    p.add_argument("--page-size", type=int, default=20)
    p.add_argument("--page-token")

    p = sub.add_parser("add-comment", help="Add a comment")
    p.add_argument("--file-token", required=True)
    p.add_argument("--file-type", required=True)
    p.add_argument("--content", required=True)

    p = sub.add_parser("reply-comment", help="Reply to a comment")
    p.add_argument("--file-token", required=True)
    p.add_argument("--file-type", required=True)
    p.add_argument("--comment-id", required=True)
    p.add_argument("--content", required=True)

    p = sub.add_parser("resolve-comment", help="Resolve a comment")
    p.add_argument("--file-token", required=True)
    p.add_argument("--file-type", required=True)
    p.add_argument("--comment-id", required=True)

    p = sub.add_parser("delete-comment", help="Delete a comment")
    p.add_argument("--file-token", required=True)
    p.add_argument("--file-type", required=True)
    p.add_argument("--comment-id", required=True)
    p.add_argument("--confirm", required=True)

    # --- Calendar ---
    p = sub.add_parser("list-calendars", help="List calendars")
    p.add_argument("--page-size", type=int, default=50)
    p.add_argument("--page-token")

    p = sub.add_parser("list-events", help="List calendar events")
    p.add_argument("--calendar-id", required=True)
    p.add_argument("--start-time", required=True, help="RFC3339 timestamp")
    p.add_argument("--end-time", required=True, help="RFC3339 timestamp")
    p.add_argument("--timezone", default="Asia/Shanghai", help="IANA timezone for naive times")
    p.add_argument("--page-size", type=int, default=50)
    p.add_argument("--page-token")

    p = sub.add_parser("get-event", help="Get event details")
    p.add_argument("--calendar-id", required=True)
    p.add_argument("--event-id", required=True)

    p = sub.add_parser("create-event", help="Create calendar event")
    p.add_argument("--calendar-id", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--start-time", required=True)
    p.add_argument("--end-time", required=True)
    p.add_argument("--description")
    p.add_argument("--attendees", help="JSON array of attendee objects")
    p.add_argument("--timezone", default="Asia/Shanghai", help="IANA timezone for naive times and display")

    p = sub.add_parser("update-event", help="Update calendar event")
    p.add_argument("--calendar-id", required=True)
    p.add_argument("--event-id", required=True)
    p.add_argument("--summary")
    p.add_argument("--description")
    p.add_argument("--start-time")
    p.add_argument("--end-time")
    p.add_argument("--timezone", default="Asia/Shanghai", help="IANA timezone for naive times and display")

    p = sub.add_parser("delete-event", help="Delete calendar event")
    p.add_argument("--calendar-id", required=True)
    p.add_argument("--event-id", required=True)
    p.add_argument("--confirm", required=True)

    p = sub.add_parser("reply-event", help="RSVP to calendar event")
    p.add_argument("--calendar-id", required=True)
    p.add_argument("--event-id", required=True)
    p.add_argument("--status", required=True, choices=["accept", "decline", "tentative"])

    p = sub.add_parser("list-event-instances", help="List instances of a recurring event")
    p.add_argument("--calendar-id", required=True)
    p.add_argument("--event-id", required=True)
    p.add_argument("--start-time", required=True, help="RFC3339 timestamp")
    p.add_argument("--end-time", required=True, help="RFC3339 timestamp (max 40-day window)")
    p.add_argument("--timezone", default="Asia/Shanghai", help="IANA timezone for naive times")
    p.add_argument("--page-size", type=int, default=50)
    p.add_argument("--page-token")

    p = sub.add_parser("list-attendees", help="List event attendees")
    p.add_argument("--calendar-id", required=True)
    p.add_argument("--event-id", required=True)
    p.add_argument("--page-size", type=int, default=50)
    p.add_argument("--page-token")

    p = sub.add_parser("create-attendees", help="Add attendees to an event")
    p.add_argument("--calendar-id", required=True)
    p.add_argument("--event-id", required=True)
    p.add_argument("--attendees", required=True,
                   help='JSON array, e.g. [{"type":"user","user_id":"ou_xxx"}]')

    p = sub.add_parser("delete-attendees", help="Remove attendees from an event")
    p.add_argument("--calendar-id", required=True)
    p.add_argument("--event-id", required=True)
    p.add_argument("--attendee-ids", required=True,
                   help="JSON array of attendee_id strings")
    p.add_argument("--confirm", required=True)

    p = sub.add_parser("list-freebusy", help="Query free/busy for 1-10 users")
    p.add_argument("--user-ids", required=True,
                   help="JSON array of user open_ids (max 10)")
    p.add_argument("--start-time", required=True, help="RFC3339 timestamp")
    p.add_argument("--end-time", required=True, help="RFC3339 timestamp")
    p.add_argument("--timezone", default="Asia/Shanghai", help="IANA timezone for naive times")

    # --- Search ---
    p = sub.add_parser("search-docs", help="Search documents")
    p.add_argument("--query", required=True)
    p.add_argument("--type")
    p.add_argument("--page-size", type=int, default=20)
    p.add_argument("--page-token")

    p = sub.add_parser("search-messages", help="Search messages")
    p.add_argument("--query", required=True)
    p.add_argument("--chat-id")
    p.add_argument("--page-size", type=int, default=20)
    p.add_argument("--page-token")

    p = sub.add_parser("list-messages", help="List chat messages")
    p.add_argument("--container-id", required=True, help="Chat ID")
    p.add_argument("--start-time")
    p.add_argument("--end-time")
    p.add_argument("--page-size", type=int, default=20)
    p.add_argument("--page-token")

    p = sub.add_parser("list-files", help="List Drive files")
    p.add_argument("--folder-token")
    p.add_argument("--page-size", type=int, default=50)
    p.add_argument("--page-token")

    # --- Bitable ---
    p = sub.add_parser("list-bitable-records", help="List bitable records")
    p.add_argument("--app-token", required=True)
    p.add_argument("--table-id", required=True)
    p.add_argument("--filter")
    p.add_argument("--page-size", type=int, default=100)
    p.add_argument("--page-token")

    p = sub.add_parser("get-bitable-record", help="Get a bitable record")
    p.add_argument("--app-token", required=True)
    p.add_argument("--table-id", required=True)
    p.add_argument("--record-id", required=True)

    p = sub.add_parser("create-bitable-records", help="Create bitable records")
    p.add_argument("--app-token", required=True)
    p.add_argument("--table-id", required=True)
    p.add_argument("--records", required=True, help="JSON array of records")

    p = sub.add_parser("update-bitable-records", help="Update bitable records")
    p.add_argument("--app-token", required=True)
    p.add_argument("--table-id", required=True)
    p.add_argument("--records", required=True, help="JSON array of records")

    p = sub.add_parser("delete-bitable-records", help="Delete bitable records")
    p.add_argument("--app-token", required=True)
    p.add_argument("--table-id", required=True)
    p.add_argument("--record-ids", required=True, help="JSON array of record IDs")
    p.add_argument("--confirm", required=True)

    p = sub.add_parser("create-bitable-app", help="Create a new bitable")
    p.add_argument("--name", required=True)
    p.add_argument("--folder-token")

    p = sub.add_parser("create-bitable-table", help="Create a table in bitable")
    p.add_argument("--app-token", required=True)
    p.add_argument("--name", required=True)

    p = sub.add_parser("delete-bitable-table", help="Delete a bitable table")
    p.add_argument("--app-token", required=True)
    p.add_argument("--table-id", required=True)
    p.add_argument("--confirm", required=True)

    p = sub.add_parser("list-bitable-fields", help="List bitable fields")
    p.add_argument("--app-token", required=True)
    p.add_argument("--table-id", required=True)


    # --- Drive Upload ---
    p = sub.add_parser("upload-file", help="Upload a local file to Drive")
    p.add_argument("--file", required=True, help="Local file path")
    p.add_argument("--folder-token", help="Target folder token (default: root)")
    p.add_argument("--file-name", help="Override file name")

    p = sub.add_parser("upload-url", help="Download from URL and upload to Drive")
    p.add_argument("--url", required=True, help="Source URL")
    p.add_argument("--folder-token", help="Target folder token (default: root)")
    p.add_argument("--file-name", help="Override file name")

    # --- Tasks ---
    p = sub.add_parser("list-tasks", help="List tasks visible to user")
    p.add_argument("--completed", choices=["true", "false"])
    p.add_argument("--page-size", type=int, default=50)
    p.add_argument("--page-token")

    p = sub.add_parser("get-task", help="Get a task by GUID")
    p.add_argument("--guid", required=True, help="Task GUID")

    p = sub.add_parser("list-tasklists", help="List task lists")
    p.add_argument("--page-size", type=int, default=50)
    p.add_argument("--page-token")

    p = sub.add_parser("get-tasklist", help="Get a task list by GUID")
    p.add_argument("--guid", required=True, help="Tasklist GUID")

    p = sub.add_parser("list-tasklist-tasks", help="List tasks in a task list")
    p.add_argument("--guid", required=True, help="Tasklist GUID")
    p.add_argument("--completed", choices=["true", "false"])
    p.add_argument("--page-size", type=int, default=50)
    p.add_argument("--page-token")

    p = sub.add_parser("complete-task", help="Mark a task as completed")
    p.add_argument("--guid", required=True, help="Task GUID")

    p = sub.add_parser("list-subtasks", help="List subtasks of a task")
    p.add_argument("--guid", required=True, help="Parent task GUID")
    p.add_argument("--page-size", type=int, default=50)
    p.add_argument("--page-token")

    p = sub.add_parser("create-task", help="Create a new task")
    p.add_argument("--summary", required=True, help="Task title")
    p.add_argument("--description", help="Task description")
    p.add_argument("--due", help="Due date (UTC): YYYY-MM-DD, 'YYYY-MM-DD HH:MM', or Unix timestamp")
    p.add_argument("--tasklist-guid", help="Add to a specific task list")
    p.add_argument("--section-guid", help="Section within the task list")

    p = sub.add_parser("create-subtask", help="Create a subtask")
    p.add_argument("--parent-guid", required=True, help="Parent task GUID")
    p.add_argument("--summary", required=True, help="Subtask title")
    p.add_argument("--description", help="Subtask description")
    p.add_argument("--due", help="Due date (UTC): YYYY-MM-DD, 'YYYY-MM-DD HH:MM', or Unix timestamp")

    p = sub.add_parser("update-task", help="Update a task's fields")
    p.add_argument("--guid", required=True, help="Task GUID")
    p.add_argument("--summary", help="New title")
    p.add_argument("--description", help="New description")
    p.add_argument("--due", help="New due date (UTC): YYYY-MM-DD, 'YYYY-MM-DD HH:MM', or Unix timestamp")
    p.add_argument("--completed-at", help="Completion timestamp (ms), 'now', or '0' to uncomplete")

    p = sub.add_parser("create-tasklist", help="Create a new task list")
    p.add_argument("--name", required=True, help="Task list name (max 100 chars)")

    p = sub.add_parser("update-tasklist", help="Rename a task list")
    p.add_argument("--guid", required=True, help="Tasklist GUID")
    p.add_argument("--name", required=True, help="New name")

    p = sub.add_parser("delete-tasklist", help="Delete a task list")
    p.add_argument("--guid", required=True, help="Tasklist GUID")
    p.add_argument("--confirm", required=True, help="GUID prefix for safety")

    p = sub.add_parser("add-task-to-tasklist", help="Add a task to a task list")
    p.add_argument("--task-guid", required=True, help="Task GUID")
    p.add_argument("--tasklist-guid", required=True, help="Tasklist GUID")

    p = sub.add_parser("remove-task-from-tasklist", help="Remove a task from a task list")
    p.add_argument("--task-guid", required=True, help="Task GUID")
    p.add_argument("--tasklist-guid", required=True, help="Tasklist GUID")

    args = parser.parse_args()

    # Load auth and config
    auth = _load_auth()
    config = _load_config()
    chat_id = auth.get("chat_id", "")
    sender_id = auth.get("sender_id", "")
    _user_token = auth.get("user_access_token")

    # Always build lark_client so modules can fall back to on-demand OAuth
    # when the pre-authed token lacks required scopes.
    _lark_client = None
    try:
        import lark_oapi as lark
        _lark_client = lark.Client.builder() \
            .app_id(config["app_id"]) \
            .app_secret(config["app_secret"]) \
            .domain(lark.FEISHU_DOMAIN) \
            .log_level(lark.LogLevel.WARNING) \
            .build()
    except Exception:
        pass  # Graceful degradation — auth will fail with clear error

    # When lark_client is available, don't use token_override — let modules
    # do full scope-aware auth via ensure_user_token().  The pre-authed token
    # from the auth file may lack scopes added after the session started.
    if _lark_client:
        _user_token = None

    cmd = args.command

    # --- Dispatch ---

    # Doc commands
    if cmd == "read-doc":
        mod = _init_module(FeishuDocs, config, _user_token, _lark_client)
        _output(mod.fetch(chat_id, sender_id, doc_id=args.token))

    elif cmd == "create-doc":
        mod = _init_module(FeishuDocs, config, _user_token, _lark_client)
        _output(mod.create(chat_id, sender_id, title=args.title,
                           markdown=args.markdown,
                           folder_token=args.folder_token,
                           wiki_space=args.wiki_space))

    elif cmd == "update-doc":
        need_sel = args.mode in ('replace_range', 'insert_before',
                                 'insert_after', 'delete_range')
        has_sel = bool(args.selection)
        has_title = bool(getattr(args, 'selection_by_title', None))
        if need_sel and not has_sel and not has_title:
            _output({"error": f"--mode {args.mode} requires --selection or --selection-by-title"})
            sys.exit(1)
        if has_sel and has_title:
            _output({"error": "--selection and --selection-by-title are mutually exclusive"})
            sys.exit(1)
        if args.mode not in ('delete_range', 'replace_all') and not args.markdown:
            _output({"error": f"--mode {args.mode} requires --markdown"})
            sys.exit(1)
        if args.mode in ('overwrite', 'append') and (has_sel or has_title):
            log.warning("--selection/--selection-by-title ignored for mode '%s'",
                        args.mode)
        mod = _init_module(FeishuDocs, config, _user_token, _lark_client)
        _output(mod.update(chat_id, sender_id, doc_id=args.token,
                           markdown=args.markdown or "",
                           mode=args.mode,
                           selection=args.selection,
                           selection_by_title=getattr(args, 'selection_by_title', None),
                           new_title=args.new_title))

    elif cmd == "delete-doc":
        _confirm_guard(args, args.token, "doc_token")
        mod = _init_module(FeishuDocs, config, _user_token, _lark_client)
        _output(mod.delete(chat_id, sender_id, doc_token=args.token))

    # Sheet commands
    elif cmd == "read-sheet":
        mod = _init_module(FeishuSheets, config, _user_token, _lark_client)
        _output(mod.read(chat_id, sender_id,
                         spreadsheet_token=args.token,
                         range_=getattr(args, "range")))

    elif cmd == "sheet-info":
        mod = _init_module(FeishuSheets, config, _user_token, _lark_client)
        _output(mod.info(chat_id, sender_id,
                         spreadsheet_token=args.token))

    elif cmd == "write-sheet":
        mod = _init_module(FeishuSheets, config, _user_token, _lark_client)
        values = _safe_json_loads(args.values, "--values")
        _output(mod.write(chat_id, sender_id,
                          spreadsheet_token=args.token,
                          range_=getattr(args, "range"),
                          values=values))

    elif cmd == "append-sheet":
        mod = _init_module(FeishuSheets, config, _user_token, _lark_client)
        values = _safe_json_loads(args.values, "--values")
        _output(mod.append(chat_id, sender_id,
                           spreadsheet_token=args.token,
                           range_=getattr(args, "range"),
                           values=values))

    elif cmd == "create-sheet":
        mod = _init_module(FeishuSheets, config, _user_token, _lark_client)
        _output(mod.create(chat_id, sender_id, title=args.title,
                           folder_token=args.folder_token))

    elif cmd == "delete-sheet":
        _confirm_guard(args, args.token, "sheet_token")
        mod = _init_module(FeishuSheets, config, _user_token, _lark_client)
        _output(mod.delete(chat_id, sender_id,
                           spreadsheet_token=args.token))

    # Wiki commands
    elif cmd == "list-wiki-spaces":
        mod = _init_module(FeishuWiki, config, _user_token, _lark_client)
        _output(mod.list_spaces(chat_id, sender_id,
                                page_size=args.page_size,
                                page_token=args.page_token))

    elif cmd == "list-wiki-nodes":
        mod = _init_module(FeishuWiki, config, _user_token, _lark_client)
        _output(mod.list_nodes(chat_id, sender_id,
                               space_id=args.space_id,
                               parent_node_token=args.parent_node_token,
                               page_size=args.page_size,
                               page_token=args.page_token))

    elif cmd == "get-wiki-node":
        mod = _init_module(FeishuWiki, config, _user_token, _lark_client)
        _output(mod.get_node(chat_id, sender_id,
                             node_token=args.token))

    elif cmd == "create-wiki-node":
        mod = _init_module(FeishuWiki, config, _user_token, _lark_client)
        _output(mod.create_node(chat_id, sender_id,
                                space_id=args.space_id,
                                title=args.title,
                                obj_type=args.obj_type,
                                parent_node_token=args.parent_node_token))

    elif cmd == "delete-wiki-node":
        _confirm_guard(args, args.token, "node_token")
        mod = _init_module(FeishuWiki, config, _user_token, _lark_client)
        _output(mod.delete_node(chat_id, sender_id,
                                space_id=args.space_id,
                                node_token=args.token))

    # Comment commands
    elif cmd == "list-comments":
        mod = _init_module(FeishuComments, config, _user_token, _lark_client)
        is_solved = None
        if args.is_solved:
            is_solved = args.is_solved == "true"
        _output(mod.list_comments(chat_id, sender_id,
                                  file_token=args.file_token,
                                  file_type=args.file_type,
                                  is_solved=is_solved,
                                  page_size=args.page_size,
                                  page_token=args.page_token))

    elif cmd == "add-comment":
        mod = _init_module(FeishuComments, config, _user_token, _lark_client)
        _output(mod.add_comment(chat_id, sender_id,
                                file_token=args.file_token,
                                file_type=args.file_type,
                                content=args.content))

    elif cmd == "reply-comment":
        mod = _init_module(FeishuComments, config, _user_token, _lark_client)
        _output(mod.reply_comment(chat_id, sender_id,
                                  file_token=args.file_token,
                                  file_type=args.file_type,
                                  comment_id=args.comment_id,
                                  content=args.content))

    elif cmd == "resolve-comment":
        mod = _init_module(FeishuComments, config, _user_token, _lark_client)
        _output(mod.resolve_comment(chat_id, sender_id,
                                    file_token=args.file_token,
                                    file_type=args.file_type,
                                    comment_id=args.comment_id))

    elif cmd == "delete-comment":
        _confirm_guard(args, args.comment_id, "comment_id")
        mod = _init_module(FeishuComments, config, _user_token, _lark_client)
        _output(mod.delete_comment(chat_id, sender_id,
                                   file_token=args.file_token,
                                   file_type=args.file_type,
                                   comment_id=args.comment_id))

    # Calendar commands
    elif cmd == "list-calendars":
        mod = _init_module(FeishuCalendar, config, _user_token, _lark_client)
        _output(mod.list_calendars(chat_id, sender_id,
                                   page_size=args.page_size,
                                   page_token=args.page_token))

    elif cmd == "list-events":
        mod = _init_module(FeishuCalendar, config, _user_token, _lark_client)
        _output(mod.list_events(chat_id, sender_id,
                                calendar_id=args.calendar_id,
                                start_time=args.start_time,
                                end_time=args.end_time,
                                page_size=args.page_size,
                                page_token=args.page_token,
                                timezone=args.timezone))

    elif cmd == "get-event":
        mod = _init_module(FeishuCalendar, config, _user_token, _lark_client)
        _output(mod.get_event(chat_id, sender_id,
                              calendar_id=args.calendar_id,
                              event_id=args.event_id))

    elif cmd == "create-event":
        mod = _init_module(FeishuCalendar, config, _user_token, _lark_client)
        attendees = None
        if args.attendees:
            attendees = _safe_json_loads(args.attendees, "--attendees")
        _output(mod.create_event(chat_id, sender_id,
                                 calendar_id=args.calendar_id,
                                 summary=args.summary,
                                 start_time=args.start_time,
                                 end_time=args.end_time,
                                 description=args.description,
                                 attendees=attendees,
                                 timezone=args.timezone))

    elif cmd == "update-event":
        mod = _init_module(FeishuCalendar, config, _user_token, _lark_client)
        kwargs = {}
        for k in ("summary", "description", "start_time", "end_time"):
            v = getattr(args, k.replace("-", "_"), None)
            if v is not None:
                kwargs[k] = v
        _output(mod.update_event(chat_id, sender_id,
                                 calendar_id=args.calendar_id,
                                 event_id=args.event_id,
                                 timezone=args.timezone,
                                 **kwargs))

    elif cmd == "delete-event":
        _confirm_guard(args, args.event_id, "event_id")
        mod = _init_module(FeishuCalendar, config, _user_token, _lark_client)
        _output(mod.delete_event(chat_id, sender_id,
                                 calendar_id=args.calendar_id,
                                 event_id=args.event_id))

    elif cmd == "reply-event":
        mod = _init_module(FeishuCalendar, config, _user_token, _lark_client)
        _output(mod.reply_event(chat_id, sender_id,
                                calendar_id=args.calendar_id,
                                event_id=args.event_id,
                                status=args.status))

    elif cmd == "list-event-instances":
        mod = _init_module(FeishuCalendar, config, _user_token, _lark_client)
        _output(mod.list_event_instances(chat_id, sender_id,
                                         calendar_id=args.calendar_id,
                                         event_id=args.event_id,
                                         start_time=args.start_time,
                                         end_time=args.end_time,
                                         page_size=args.page_size,
                                         page_token=args.page_token,
                                         timezone=args.timezone))

    elif cmd == "list-attendees":
        mod = _init_module(FeishuCalendar, config, _user_token, _lark_client)
        _output(mod.list_attendees(chat_id, sender_id,
                                   calendar_id=args.calendar_id,
                                   event_id=args.event_id,
                                   page_size=args.page_size,
                                   page_token=args.page_token))

    elif cmd == "create-attendees":
        mod = _init_module(FeishuCalendar, config, _user_token, _lark_client)
        attendees = _safe_json_loads(args.attendees, "--attendees")
        _output(mod.create_attendees(chat_id, sender_id,
                                     calendar_id=args.calendar_id,
                                     event_id=args.event_id,
                                     attendees=attendees))

    elif cmd == "delete-attendees":
        _confirm_guard(args, args.event_id, "event_id")
        mod = _init_module(FeishuCalendar, config, _user_token, _lark_client)
        attendee_ids = _safe_json_loads(args.attendee_ids, "--attendee-ids")
        _output(mod.delete_attendees(chat_id, sender_id,
                                     calendar_id=args.calendar_id,
                                     event_id=args.event_id,
                                     attendee_ids=attendee_ids))

    elif cmd == "list-freebusy":
        mod = _init_module(FeishuCalendar, config, _user_token, _lark_client)
        user_ids = _safe_json_loads(args.user_ids, "--user-ids")
        _output(mod.list_freebusy(chat_id, sender_id,
                                  user_ids=user_ids,
                                  start_time=args.start_time,
                                  end_time=args.end_time,
                                  timezone=args.timezone))

    # Search commands
    elif cmd == "search-docs":
        mod = _init_module(FeishuSearch, config, _user_token, _lark_client)
        _output(mod.search_docs(chat_id, sender_id,
                                query=args.query,
                                docs_type=args.type,
                                page_size=args.page_size,
                                page_token=args.page_token))

    elif cmd == "search-messages":
        mod = _init_module(FeishuSearch, config, _user_token, _lark_client)
        _output(mod.search_messages(chat_id, sender_id,
                                    query=args.query,
                                    target_chat_id=args.chat_id,
                                    page_size=args.page_size,
                                    page_token=args.page_token))

    elif cmd == "list-messages":
        mod = _init_module(FeishuSearch, config, _user_token, _lark_client)
        _output(mod.list_messages(chat_id, sender_id,
                                  container_id=args.container_id,
                                  start_time=args.start_time,
                                  end_time=args.end_time,
                                  page_size=args.page_size,
                                  page_token=args.page_token))

    elif cmd == "list-files":
        mod = _init_module(FeishuSearch, config, _user_token, _lark_client)
        _output(mod.list_files(chat_id, sender_id,
                               folder_token=args.folder_token,
                               page_size=args.page_size,
                               page_token=args.page_token))

    # Bitable commands
    elif cmd == "list-bitable-records":
        mod = _init_module(FeishuBitable, config, _user_token, _lark_client)
        _output(mod.list_records(chat_id, sender_id,
                                 app_token=args.app_token,
                                 table_id=args.table_id,
                                 filter_=args.filter,
                                 page_size=args.page_size,
                                 page_token=args.page_token))

    elif cmd == "get-bitable-record":
        mod = _init_module(FeishuBitable, config, _user_token, _lark_client)
        _output(mod.get_record(chat_id, sender_id,
                               app_token=args.app_token,
                               table_id=args.table_id,
                               record_id=args.record_id))

    elif cmd == "create-bitable-records":
        mod = _init_module(FeishuBitable, config, _user_token, _lark_client)
        records = _safe_json_loads(args.records, "--records")
        _output(mod.create_records(chat_id, sender_id,
                                   app_token=args.app_token,
                                   table_id=args.table_id,
                                   records=records))

    elif cmd == "update-bitable-records":
        mod = _init_module(FeishuBitable, config, _user_token, _lark_client)
        records = _safe_json_loads(args.records, "--records")
        _output(mod.update_records(chat_id, sender_id,
                                   app_token=args.app_token,
                                   table_id=args.table_id,
                                   records=records))

    elif cmd == "delete-bitable-records":
        record_ids = _safe_json_loads(args.record_ids, "--record-ids")
        if not record_ids:
            print(json.dumps({"error": "record_ids is empty"}))
            sys.exit(1)
        _confirm_guard(args, str(record_ids[0]), "record_id")
        mod = _init_module(FeishuBitable, config, _user_token, _lark_client)
        _output(mod.delete_records(chat_id, sender_id,
                                   app_token=args.app_token,
                                   table_id=args.table_id,
                                   record_ids=record_ids))

    elif cmd == "create-bitable-app":
        mod = _init_module(FeishuBitable, config, _user_token, _lark_client)
        _output(mod.create_app(chat_id, sender_id,
                               name=args.name,
                               folder_token=args.folder_token))

    elif cmd == "create-bitable-table":
        mod = _init_module(FeishuBitable, config, _user_token, _lark_client)
        _output(mod.create_table(chat_id, sender_id,
                                 app_token=args.app_token,
                                 name=args.name))

    elif cmd == "delete-bitable-table":
        _confirm_guard(args, args.table_id, "table_id")
        mod = _init_module(FeishuBitable, config, _user_token, _lark_client)
        _output(mod.delete_table(chat_id, sender_id,
                                 app_token=args.app_token,
                                 table_id=args.table_id))

    elif cmd == "list-bitable-fields":
        mod = _init_module(FeishuBitable, config, _user_token, _lark_client)
        _output(mod.list_fields(chat_id, sender_id,
                                app_token=args.app_token,
                                table_id=args.table_id))


    # Task commands
    elif cmd == "list-tasks":
        mod = _init_module(FeishuTasks, config, _user_token, _lark_client)
        completed = None
        if args.completed:
            completed = args.completed == "true"
        _output(mod.list_tasks(chat_id, sender_id,
                               completed=completed,
                               page_size=args.page_size,
                               page_token=args.page_token))

    elif cmd == "get-task":
        mod = _init_module(FeishuTasks, config, _user_token, _lark_client)
        _output(mod.get_task(chat_id, sender_id, task_guid=args.guid))

    elif cmd == "list-tasklists":
        mod = _init_module(FeishuTasks, config, _user_token, _lark_client)
        _output(mod.list_tasklists(chat_id, sender_id,
                                   page_size=args.page_size,
                                   page_token=args.page_token))

    elif cmd == "get-tasklist":
        mod = _init_module(FeishuTasks, config, _user_token, _lark_client)
        _output(mod.get_tasklist(chat_id, sender_id,
                                 tasklist_guid=args.guid))

    elif cmd == "list-tasklist-tasks":
        mod = _init_module(FeishuTasks, config, _user_token, _lark_client)
        completed = None
        if args.completed:
            completed = args.completed == "true"
        _output(mod.get_tasklist_tasks(chat_id, sender_id,
                                       tasklist_guid=args.guid,
                                       completed=completed,
                                       page_size=args.page_size,
                                       page_token=args.page_token))

    elif cmd == "complete-task":
        mod = _init_module(FeishuTasks, config, _user_token, _lark_client)
        _output(mod.complete_task(chat_id, sender_id, task_guid=args.guid))

    elif cmd == "list-subtasks":
        mod = _init_module(FeishuTasks, config, _user_token, _lark_client)
        _output(mod.list_subtasks(chat_id, sender_id,
                                  task_guid=args.guid,
                                  page_size=args.page_size,
                                  page_token=args.page_token))

    elif cmd == "create-task":
        mod = _init_module(FeishuTasks, config, _user_token, _lark_client)
        due_ms = _safe_parse_due(args.due)
        _output(mod.create_task(chat_id, sender_id,
                                summary=args.summary,
                                description=args.description,
                                due_timestamp=due_ms,
                                tasklist_guid=args.tasklist_guid,
                                section_guid=args.section_guid))

    elif cmd == "create-subtask":
        mod = _init_module(FeishuTasks, config, _user_token, _lark_client)
        due_ms = _safe_parse_due(args.due)
        _output(mod.create_subtask(chat_id, sender_id,
                                   parent_guid=args.parent_guid,
                                   summary=args.summary,
                                   description=args.description,
                                   due_timestamp=due_ms))

    elif cmd == "update-task":
        mod = _init_module(FeishuTasks, config, _user_token, _lark_client)
        due_ms = _safe_parse_due(args.due)
        # Handle --completed-at: 'now' → current ms, '0' → uncomplete, else parse
        completed_at = None
        if args.completed_at:
            if args.completed_at == "now":
                completed_at = str(int(time.time() * 1000))
            elif args.completed_at == "0":
                completed_at = "0"
            else:
                completed_at = _safe_parse_due(args.completed_at)
        _output(mod.update_task(chat_id, sender_id,
                                task_guid=args.guid,
                                summary=args.summary,
                                description=args.description,
                                due_timestamp=due_ms,
                                completed_at=completed_at))



    elif cmd == "create-tasklist":
        mod = _init_module(FeishuTasks, config, _user_token, _lark_client)
        _output(mod.create_tasklist(chat_id, sender_id, name=args.name))

    elif cmd == "update-tasklist":
        mod = _init_module(FeishuTasks, config, _user_token, _lark_client)
        _output(mod.update_tasklist(chat_id, sender_id,
                                    tasklist_guid=args.guid, name=args.name))

    elif cmd == "delete-tasklist":
        _confirm_guard(args, args.guid, "tasklist_guid")
        mod = _init_module(FeishuTasks, config, _user_token, _lark_client)
        _output(mod.delete_tasklist(chat_id, sender_id,
                                    tasklist_guid=args.guid))

    elif cmd == "add-task-to-tasklist":
        mod = _init_module(FeishuTasks, config, _user_token, _lark_client)
        _output(mod.add_task_to_tasklist(chat_id, sender_id,
                                         task_guid=args.task_guid,
                                         tasklist_guid=args.tasklist_guid))

    elif cmd == "remove-task-from-tasklist":
        mod = _init_module(FeishuTasks, config, _user_token, _lark_client)
        _output(mod.remove_task_from_tasklist(chat_id, sender_id,
                                              task_guid=args.task_guid,
                                              tasklist_guid=args.tasklist_guid))

    # Drive upload commands
    elif cmd == "upload-file":
        mod = _init_module(FeishuDrive, config, _user_token, _lark_client)
        _output(mod.upload_file(chat_id, sender_id,
                                file_path=args.file,
                                folder_token=args.folder_token,
                                file_name=args.file_name))

    elif cmd == "upload-url":
        mod = _init_module(FeishuDrive, config, _user_token, _lark_client)
        _output(mod.upload_from_url(chat_id, sender_id,
                                    url=args.url,
                                    folder_token=args.folder_token,
                                    file_name=args.file_name))

    else:
        _output({"error": f"Unknown command: {cmd}"})
        sys.exit(1)


if __name__ == "__main__":
    main()
