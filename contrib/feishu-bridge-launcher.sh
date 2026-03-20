#!/bin/bash
# Feishu Bridge launcher for launchd (macOS)
# Sources .env and runs the bridge

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

set -a
[ -f "$HOME/.config/feishu-bridge/.env" ] && source "$HOME/.config/feishu-bridge/.env"
set +a

exec feishu-bridge --bot "${1:-claude-code}"
