#!/bin/bash
# Feishu Bridge launcher for launchd
# Sources .env and runs the bridge with venv python

set -a
source "$HOME/.claude/.env"
set +a

exec "$HOME/.claude/scripts/venv/bin/python3" \
    "$HOME/.claude/scripts/feishu_bridge.py" \
    --bot "${1:-claude-code}"
