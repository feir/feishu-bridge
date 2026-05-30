#!/bin/bash
# release.sh — create an annotated tag for the CURRENT version with release
# notes built from commit subjects, then push.
#
# Expects auto-version.sh to have already bumped __init__.py + pyproject.toml
# and the bump to be committed. Reads the version from __init__.py.
#
# Usage:
#   scripts/release.sh                    # auto-generate notes from commits
#   scripts/release.sh "headline text"    # prepend a human-supplied headline
#
# Env:
#   RELEASE_NO_PUSH=1         — create tag locally, don't push (no publish)
#   RELEASE_ALLOW_UNPUSHED=1  — allow tagging a commit not yet on origin/<branch>
set -euo pipefail
cd "$(dirname "$0")/.."

HEADLINE="${1:-}"
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' feishu_bridge/__init__.py)
[ -z "$VERSION" ] && { echo "release: cannot read version" >&2; exit 1; }
TAG="v${VERSION}"

# Idempotent: if the tag already exists locally, do nothing.
if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  echo "release: ${TAG} already exists, skipping" >&2
  exit 0
fi

# Refuse to tag a commit that isn't on the remote branch yet. The tag push
# triggers PyPI publish + GitHub Release; if origin/<branch> doesn't yet contain
# this commit, we'd publish before the branch reflects the release. scripts/ship.sh
# pushes the branch first; direct callers must too (override: RELEASE_ALLOW_UNPUSHED=1).
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "${RELEASE_ALLOW_UNPUSHED:-0}" != "1" ] \
   && ! git merge-base --is-ancestor HEAD "origin/${BRANCH}" 2>/dev/null; then
  echo "release: HEAD is not on origin/${BRANCH} — push the branch first (or use scripts/ship.sh)" >&2
  exit 1
fi

LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || true)
RANGE="${LAST_TAG:+${LAST_TAG}..}HEAD"
ALL_COMMITS=$(git log --no-merges --format='%s' "$RANGE" || true)

if [ -z "$ALL_COMMITS" ]; then
  echo "release: no commits since ${LAST_TAG:-repo-root}, skipping" >&2
  exit 0
fi

COMMITS=$(echo "$ALL_COMMITS" | grep -vE '^chore: bump version to ' || true)

NOTES_FILE=$(mktemp /tmp/release-notes-XXXXXX.md)
trap 'rm -f "$NOTES_FILE"' EXIT

{
  echo "feishu-bridge ${TAG}"
  echo
  [ -n "$HEADLINE" ] && { echo "$HEADLINE"; echo; }
} > "$NOTES_FILE"

# Release notes are the commit subjects since the last tag — deterministic, no
# external tools. When only version-bump commits exist, list those verbatim.
echo "## Changes" >> "$NOTES_FILE"
if [ -z "$COMMITS" ]; then
  echo "$ALL_COMMITS" | sed 's/^/- /' >> "$NOTES_FILE"
else
  echo "$COMMITS" | sed 's/^/- /' >> "$NOTES_FILE"
fi

git tag -a "$TAG" -F "$NOTES_FILE"

if [ "${RELEASE_NO_PUSH:-0}" != "1" ]; then
  git push origin "$TAG" >/dev/null 2>&1 || {
    echo "release: failed to push ${TAG}" >&2
    exit 1
  }
fi

echo "$TAG"
