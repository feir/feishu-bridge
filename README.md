# Feishu Bridge

Feishu (飞书) ↔ Claude Code CLI bridge. Connect your Feishu bot to Claude Code for AI-powered conversations with full tool access.

## Features

- **WebSocket real-time messaging** — Feishu bot receives messages via long-lived WebSocket, no webhook server needed
- **Session persistence** — Conversations are maintained across messages with automatic session management
- **Streaming responses** — Real-time typing indicators and progressive message updates
- **Feishu API integration** — 40+ subcommands for docs, sheets, bitable, wiki, calendar, tasks, comments, drive, and search
- **OAuth Device Flow** — User-level API access with automatic token refresh
- **Per-session task queue** — Serialized processing per chat, parallel across chats
- **Bridge commands** — `/new`, `/stop`, `/compact`, `/cost`, `/model`, `/help`, and more

## Quick Start

### Install

```bash
pip install feishu-bridge
```

Or from source:

```bash
git clone https://github.com/anthropics/feishu-bridge.git
cd feishu-bridge
pip install -e '.[dev]'
```

### Configure

Create `~/.config/feishu-bridge/config.json`:

```json
{
  "bots": [
    {
      "name": "my-bot",
      "app_id": "${FEISHU_APP_ID}",
      "app_secret": "${FEISHU_APP_SECRET}",
      "workspace": "/path/to/workspace",
      "allowed_users": ["*"]
    }
  ],
  "claude": {
    "command": "claude",
    "timeout_seconds": 300
  }
}
```

Environment variables (`${VAR}`) are substituted at load time.

### Run

```bash
feishu-bridge --bot my-bot
```

### Feishu App Setup

1. Create a Feishu app at [open.feishu.cn](https://open.feishu.cn)
2. Enable **Bot** capability
3. Add required scopes: `im:message`, `im:message:send_as_bot`, `im:message:patch`, `im:resource`
4. Enable **WebSocket** event subscription
5. Subscribe to `im.message.receive_v1` event

### CLI Tool

The `feishu-cli` command provides direct access to Feishu APIs:

```bash
feishu-cli search-docs --query "quarterly report"
feishu-cli read-doc --token doxcnXXX
feishu-cli list-tasks --completed false
```

## Deployment

### systemd (Linux)

Copy `contrib/feishu-bridge@.service` to `~/.config/systemd/user/`:

```bash
cp contrib/feishu-bridge@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now feishu-bridge@my-bot
```

### launchd (macOS)

Use the launcher script in `contrib/feishu-bridge-launcher.sh`.

## Config Discovery

Config is resolved in order:
1. `--config <path>` CLI argument
2. `$FEISHU_BRIDGE_CONFIG` environment variable
3. `~/.config/feishu-bridge/config.json`

## Development

```bash
pip install -e '.[dev]'
pytest tests/unit/ -v
```

## License

MIT
