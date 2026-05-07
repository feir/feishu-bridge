# Feishu CLI

You have access to Feishu (飞书) APIs via `/Users/feir/.local/bin/feishu-cli <command> [args]`.

## Command Categories
- **Messaging** — send bot messages, images, and audio to chats

Run `feishu-cli prompt` to load the **complete command reference** with all arguments, formats, and usage details.
Run `feishu-cli <command> --help` for a single command's usage.
All output is JSON. Delete commands require `--confirm <prefix>` safety guard.

## Available Commands

### Messaging (Bot)
- `send-message --chat-id <id> --text <text>` — Send a bot text message to a chat (no user auth needed)
- `send-message --chat-id <id> --msg-type interactive --content '<json>'` — Send a card/post message (raw JSON content)
- `send-image --chat-id <id> --file <path>` — Upload and send an image (png, jpg, etc.)
- `send-audio --chat-id <id> --file <path> [--duration <ms>]` — Upload and send audio (opus preferred, wav accepted)

## For Other Feishu Operations

For documents, spreadsheets, wiki, calendar, tasks, bitable, mail, drive, and search, use the official Lark CLI:

```bash
~/.local/bin/lark <command> [args]
```

Run `lark --help` for available command categories. Key commands:
- `lark docs +fetch --doc <token>` — Read a document
- `lark docs +create --title <title> --body <markdown>` — Create a document
- `lark docs +update --doc <token> ...` — Update a document
- `lark sheets +read --token <token> --range <A1>` — Read spreadsheet
- `lark mail +search --query <keyword>` — Search mail
- `lark mail +triage` — List recent emails
- `lark calendar events +list` — List calendar events
- `lark tasks +list` — List tasks

## Important Notes
- feishu-cli output is JSON
- For multi-step operations, chain lark CLI calls
- Use `--help` on any command for full argument details
