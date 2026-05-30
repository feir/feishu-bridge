#!/bin/bash
# ship.sh — one-command release in the correct order.
#
# Feature commits must already be committed (clean tree). This script then:
#   1. bumps the version (+ syncs uv.lock)         — auto-version.sh
#   2. commits the bump
#   3. pushes the BRANCH first                      — branch reflects the release
#   4. tags + pushes the tag                        — release.sh → CI test + publish
#
# Pushing the branch before the tag is the whole point: the tag push triggers
# PyPI publish + GitHub Release, so origin/<branch> must already contain the
# release commit. The pre-push hook's version check is bypassed here because
# THIS is the sanctioned bump-then-push flow (SKIP_VERSION_CHECK=1).
#
# Usage:
#   scripts/ship.sh                     # notes from commit subjects
#   scripts/ship.sh "headline text"     # prepend a human headline
#
# Env (forwarded to release.sh): RELEASE_NO_PUSH=1, RELEASE_ALLOW_UNPUSHED=1
set -euo pipefail
cd "$(dirname "$0")/.."

# 1. Preconditions.
if [ -n "$(git status --porcelain)" ]; then
  echo "ship: working tree not clean — commit feature work first" >&2
  git status --short >&2
  exit 1
fi
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" = "HEAD" ]; then
  echo "ship: detached HEAD — checkout a branch first" >&2
  exit 1
fi

# 2. Bump version (auto-version.sh also syncs uv.lock).
NEW=$(scripts/auto-version.sh)
echo "ship: version → ${NEW}" >&2

# 3. Commit the bump (matches the subject release.sh filters out of notes).
git add feishu_bridge/__init__.py pyproject.toml uv.lock
git commit -q -m "chore: bump version to ${NEW}"

# 4. Push the branch FIRST (sanctioned flow — bypass the version-check hook).
SKIP_VERSION_CHECK=1 git push origin "$BRANCH"

# 5. Tag + push the tag → triggers CI (tests gate the publish).
exec scripts/release.sh "$@"
