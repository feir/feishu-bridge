#!/bin/bash
# release.sh — create an annotated tag for the CURRENT version with
# LLM-written release notes, then push.
#
# Expects auto-version.sh to have already bumped __init__.py + pyproject.toml
# and the bump to be committed. Reads the version from __init__.py.
#
# Usage:
#   scripts/release.sh                    # auto-generate notes from commits
#   scripts/release.sh "headline text"    # prepend a human-supplied headline
#
# Env:
#   RELEASE_NO_LLM=1   — skip LLM, use raw commit subjects
#   RELEASE_NO_PUSH=1  — create tag locally only
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

if [ -z "$COMMITS" ]; then
  echo "## Changes" >> "$NOTES_FILE"
  echo "$ALL_COMMITS" | sed 's/^/- /' >> "$NOTES_FILE"
elif [ "${RELEASE_NO_LLM:-0}" = "1" ] || ! command -v claude >/dev/null 2>&1; then
  echo "## Changes" >> "$NOTES_FILE"
  echo "$COMMITS" | sed 's/^/- /' >> "$NOTES_FILE"
else
  PROMPT=$(cat <<PROMPT_END
你是 feishu-bridge 项目的发版记录助手。根据下面的 commit subject 列表，
为 ${TAG} 写一份中文 release notes（Markdown，≤150 字），结构如下：

## 亮点
- 1-3 条最重要的用户可见变化（新功能 / 行为变化 / 破坏性变更）

## 修复与改进
- 其他修复、重构、测试、文档（每条一行，合并相关项）

规则：
- 不要复述版本号或日期
- 合并相关的 commit，不要逐条复述
- 语气平实，避免营销感
- 只输出 Markdown，不要解释或总结

Commit subjects:
${COMMITS}
PROMPT_END
)
  LLM_OUT=$(printf '%s' "$PROMPT" \
    | claude -p --model haiku --setting-sources "" 2>/dev/null \
    || true)
  if [ -n "$LLM_OUT" ]; then
    printf '%s\n' "$LLM_OUT" >> "$NOTES_FILE"
  else
    echo "## Changes" >> "$NOTES_FILE"
    echo "$COMMITS" | sed 's/^/- /' >> "$NOTES_FILE"
  fi
fi

git tag -a "$TAG" -F "$NOTES_FILE"

if [ "${RELEASE_NO_PUSH:-0}" != "1" ]; then
  git push origin "$TAG" >/dev/null 2>&1 || {
    echo "release: failed to push ${TAG}" >&2
    exit 1
  }
fi

echo "$TAG"
