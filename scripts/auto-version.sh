#!/bin/bash
# auto-version.sh — CalVer (YYYY.MM.DD[.N]) bump for feishu-bridge
# Called by session-done-commit.sh before staging, or manually.
# Prints the new version to stdout.
set -euo pipefail
cd "$(dirname "$0")/.."

INIT_FILE="feishu_bridge/__init__.py"
TOML_FILE="pyproject.toml"

# Current version from __init__.py (single source of truth for bump base)
CURRENT=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$INIT_FILE")
TOML_CURRENT=$(sed -n 's/^version = "\(.*\)"/\1/p' "$TOML_FILE")

# Refuse to bump from an inconsistent base — otherwise the bump computes off
# the wrong number (e.g. 2026.05.28 → 2026.05.28.1 while pyproject is already
# 2026.05.29, producing a regression that PyPI's version comparator ignores).
if [ "$CURRENT" != "$TOML_CURRENT" ]; then
  echo "auto-version: version mismatch — __init__.py=$CURRENT pyproject.toml=$TOML_CURRENT" >&2
  echo "auto-version: align both files manually, then re-run" >&2
  exit 1
fi

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
sed -i '' "s/^__version__ = .*/__version__ = \"$NEW\"/" "$INIT_FILE"
sed -i '' "s/^version = .*/version = \"$NEW\"/" "$TOML_FILE"

echo "$NEW"
