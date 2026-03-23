#!/bin/bash
# auto-version.sh — CalVer (YYYY.MM.DD[.N]) bump for feishu-bridge
# Called by session-done-commit.sh before staging, or manually.
# Prints the new version to stdout.
set -euo pipefail
cd "$(dirname "$0")/.."

INIT_FILE="feishu_bridge/__init__.py"
TOML_FILE="pyproject.toml"

# Current version from __init__.py
CURRENT=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$INIT_FILE")

# Today's CalVer base
TODAY=$(date +%Y.%m.%d)

if [ "$CURRENT" = "$TODAY" ]; then
  NEW="${TODAY}.1"
elif [[ "$CURRENT" == "${TODAY}."* ]]; then
  N="${CURRENT##*.}"
  NEW="${TODAY}.$((N + 1))"
else
  NEW="$TODAY"
fi

# Update both sources of truth
sed -i "s/^__version__ = .*/__version__ = \"$NEW\"/" "$INIT_FILE"
sed -i "s/^version = .*/version = \"$NEW\"/" "$TOML_FILE"

echo "$NEW"
