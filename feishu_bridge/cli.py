#!/usr/bin/env python3
"""
Feishu Bridge CLI — bot messaging entry point.

Provides send-message, send-image, send-audio for bot-initiated messages.
For documents, spreadsheets, wiki, calendar, tasks, mail, etc.,
use the official Lark CLI: ~/.local/bin/lark <command>

Usage:
    feishu-cli send-message --chat-id oc_xxx --text "Hello"
    feishu-cli send-image --chat-id oc_xxx --file photo.png
    feishu-cli send-audio --chat-id oc_xxx --file voice.opus
    feishu-cli prompt [--summary]
"""

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _load_config():
    """Load app credentials from bridge config."""
    _env_file = Path.home() / ".config" / "feishu-bridge" / ".env"
    if _env_file.is_file():
        with open(_env_file) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    os.environ.setdefault(_k.strip(), _v.strip())

    bot_name = os.environ.get("FEISHU_BOT_NAME")

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

    for bot in config.get("bots", []):
        if bot_name and bot.get("name") != bot_name:
            continue
        app_id = os.path.expandvars(bot.get("app_id", bot.get("feishu_app_id", "")))
        app_secret = os.path.expandvars(bot.get("app_secret", bot.get("feishu_app_secret", "")))
        if app_id and app_secret:
            return {"app_id": app_id, "app_secret": app_secret}

    if config.get("bots"):
        bot = config["bots"][0]
        app_id = os.path.expandvars(bot.get("app_id", bot.get("feishu_app_id", "")))
        app_secret = os.path.expandvars(bot.get("app_secret", bot.get("feishu_app_secret", "")))
        if app_id and app_secret:
            return {"app_id": app_id, "app_secret": app_secret}

    print(json.dumps({"error": "No bot config found"}))
    sys.exit(1)


def _build_lark_client(config=None):
    """Build a lark_oapi Client from config."""
    import lark_oapi as lark
    if config is None:
        config = _load_config()
    return config, lark.Client.builder() \
        .app_id(config["app_id"]) \
        .app_secret(config["app_secret"]) \
        .domain(lark.FEISHU_DOMAIN) \
        .log_level(lark.LogLevel.WARNING) \
        .build()


def _output(result):
    """Print result as JSON."""
    if result is None:
        print(json.dumps({"error": "Auth failed — authorization card sent"}))
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, default=str))


def main():
    parser = argparse.ArgumentParser(
        description="Feishu Bridge CLI — bot messaging",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- IM (bot messages) ---
    p = sub.add_parser("send-message",
                       help="Send a bot message to a chat (no user auth needed)")
    p.add_argument("--chat-id", required=True,
                   help="Feishu chat_id (e.g. oc_xxx)")
    p.add_argument("--text", help="Plain text message content")
    p.add_argument("--msg-type", default="text",
                   help="Message type: text, interactive, post (default: text)")
    p.add_argument("--content",
                   help="Raw JSON content string (for non-text msg types)")

    p = sub.add_parser("prompt",
                       help="Output LLM system prompt for feishu-cli usage")
    p.add_argument("--summary", action="store_true",
                   help="Output short summary instead of full reference")

    p = sub.add_parser("send-audio",
                       help="Upload audio file and send as audio message")
    p.add_argument("--chat-id", required=True,
                   help="Feishu chat_id (e.g. oc_xxx)")
    p.add_argument("--file", required=True,
                   help="Path to audio file (opus preferred, wav accepted)")
    p.add_argument("--duration", type=int,
                   help="Audio duration in milliseconds (auto-detected if omitted)")

    p = sub.add_parser("send-image",
                       help="Upload image file and send as image message")
    p.add_argument("--chat-id", required=True,
                   help="Feishu chat_id (e.g. oc_xxx)")
    p.add_argument("--file", required=True,
                   help="Path to image file (png, jpg, etc.)")

    args = parser.parse_args()

    # --- No-auth commands ---
    if args.command == "prompt":
        filename = "cli_prompt_summary.md" if args.summary else "cli_prompt.md"
        prompt_path = SCRIPT_DIR / "data" / filename
        if not prompt_path.exists():
            print(f"Error: {filename} not found", file=sys.stderr)
            sys.exit(1)
        text = prompt_path.read_text()
        cli_abs = os.path.abspath(sys.argv[0])
        text = text.replace("feishu-cli", cli_abs)
        print(text, end="")
        return

    # --- Bot-only commands (no user auth needed) ---
    if args.command == "send-message":
        if args.text and args.content:
            _output({"error": "--text and --content are mutually exclusive"})
            sys.exit(1)
        if args.text and args.msg_type != "text":
            _output({"error": "--text can only be used with --msg-type text"})
            sys.exit(1)

        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest, CreateMessageRequestBody,
            )

            config, client = _build_lark_client()

            if args.text:
                content = json.dumps({"text": args.text})
            elif args.content:
                content = args.content
            else:
                _output({"error": "Either --text or --content is required"})
                sys.exit(1)

            body = CreateMessageRequestBody.builder() \
                .receive_id(args.chat_id) \
                .msg_type(args.msg_type) \
                .content(content) \
                .build()
            req = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(body) \
                .build()
            resp = client.im.v1.message.create(req)
            if resp.success():
                mid = resp.data.message_id if resp.data else None
                _output({"message_id": mid})
            else:
                _output({"error": resp.msg, "code": resp.code})
                sys.exit(1)
        except SystemExit:
            raise
        except Exception as e:
            _output({"error": str(e)})
            sys.exit(1)
        return

    if args.command == "send-audio":
        try:
            from lark_oapi.api.im.v1 import (
                CreateFileRequest, CreateFileRequestBody,
                CreateMessageRequest, CreateMessageRequestBody,
            )

            file_path = Path(args.file)
            if not file_path.exists():
                _output({"error": f"File not found: {args.file}"})
                sys.exit(1)

            config, client = _build_lark_client()

            suffix = file_path.suffix.lower()
            file_type = "opus" if suffix in (".opus", ".ogg") else "stream"
            msg_type = "audio" if file_type == "opus" else "file"

            with open(file_path, "rb") as f:
                body = CreateFileRequestBody.builder() \
                    .file_type(file_type) \
                    .file_name(file_path.name) \
                    .file(f)
                if args.duration:
                    body = body.duration(args.duration)
                body = body.build()

                upload_req = CreateFileRequest.builder() \
                    .request_body(body) \
                    .build()
                upload_resp = client.im.v1.file.create(upload_req)

            if not upload_resp.success():
                _output({"error": f"Upload failed: {upload_resp.msg}",
                         "code": upload_resp.code})
                sys.exit(1)

            file_key = upload_resp.data.file_key
            content = json.dumps({"file_key": file_key})

            msg_body = CreateMessageRequestBody.builder() \
                .receive_id(args.chat_id) \
                .msg_type(msg_type) \
                .content(content) \
                .build()
            msg_req = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(msg_body) \
                .build()
            msg_resp = client.im.v1.message.create(msg_req)

            if msg_resp.success():
                mid = msg_resp.data.message_id if msg_resp.data else None
                _output({"message_id": mid, "file_key": file_key,
                         "msg_type": msg_type})
            else:
                _output({"error": f"Send failed: {msg_resp.msg}",
                         "code": msg_resp.code})
                sys.exit(1)
        except SystemExit:
            raise
        except Exception as e:
            _output({"error": str(e)})
            sys.exit(1)
        return

    if args.command == "send-image":
        try:
            from lark_oapi.api.im.v1 import (
                CreateImageRequest, CreateImageRequestBody,
                CreateMessageRequest, CreateMessageRequestBody,
            )

            file_path = Path(args.file)
            if not file_path.exists():
                _output({"error": f"File not found: {args.file}"})
                sys.exit(1)

            config, client = _build_lark_client()

            with open(file_path, "rb") as f:
                body = CreateImageRequestBody.builder() \
                    .image_type("message") \
                    .image(f) \
                    .build()
                upload_req = CreateImageRequest.builder() \
                    .request_body(body) \
                    .build()
                upload_resp = client.im.v1.image.create(upload_req)

            if not upload_resp.success():
                _output({"error": f"Image upload failed: {upload_resp.msg}",
                         "code": upload_resp.code})
                sys.exit(1)

            image_key = upload_resp.data.image_key
            content = json.dumps({"image_key": image_key})

            msg_body = CreateMessageRequestBody.builder() \
                .receive_id(args.chat_id) \
                .msg_type("image") \
                .content(content) \
                .build()
            msg_req = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(msg_body) \
                .build()
            msg_resp = client.im.v1.message.create(msg_req)

            if msg_resp.success():
                mid = msg_resp.data.message_id if msg_resp.data else None
                _output({"message_id": mid, "image_key": image_key})
            else:
                _output({"error": f"Send failed: {msg_resp.msg}",
                         "code": msg_resp.code})
                sys.exit(1)
        except SystemExit:
            raise
        except Exception as e:
            _output({"error": str(e)})
            sys.exit(1)
        return


if __name__ == "__main__":
    main()
