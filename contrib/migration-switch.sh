#!/bin/bash
# migration-switch.sh — Atomic switchover from flat scripts to pip package.
#
# The new systemd unit (feishu-bridge@.service) has already been installed
# and daemon-reload'd. This script simply restarts both instances, which
# picks up the new ExecStart (entry point from pip install -e).
#
# Usage:
#   systemd-run --user --no-block bash ~/feishu-bridge/contrib/migration-switch.sh
#   (or: at now <<< 'bash ~/feishu-bridge/contrib/migration-switch.sh')
#
# IMPORTANT: Do NOT run this from within a feishu-bridge Claude session —
# it will kill the parent process. Use systemd-run or at(1) instead.

set -euo pipefail

LOG_TAG="feishu-bridge-migration"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [$LOG_TAG] $*"; }

BOTS=("claude-code-remote" "openclaw-workspace")

# --- Phase 1: Restart each bot instance ---
for bot in "${BOTS[@]}"; do
    unit="feishu-bridge@${bot}.service"
    log "Restarting $unit ..."
    systemctl --user restart "$unit"
    sleep 2

    if systemctl --user is-active --quiet "$unit"; then
        log "✓ $unit is active"
    else
        log "✗ $unit failed to start! Rolling back..."
        # Show failure reason
        systemctl --user status "$unit" --no-pager -l 2>&1 | tail -20
        log "Check: journalctl --user -u $unit -n 50"
        exit 1
    fi
done

# --- Phase 2: Health check ---
log "Waiting 5s for stabilization..."
sleep 5

ALL_OK=true
for bot in "${BOTS[@]}"; do
    unit="feishu-bridge@${bot}.service"
    if systemctl --user is-active --quiet "$unit"; then
        log "✓ $unit still running after soak"
    else
        log "✗ $unit crashed during soak"
        ALL_OK=false
    fi
done

if $ALL_OK; then
    log "=== Migration complete. Both bots running via pip package. ==="
    log "Next steps:"
    log "  1. Send a test message to verify"
    log "  2. Clean up old scripts from ~/.claude/scripts/"
    log "  3. Remove backup: ~/.config/systemd/user/feishu-bridge@.service.bak"
else
    log "=== Migration FAILED. Check journal logs. ==="
    exit 1
fi
