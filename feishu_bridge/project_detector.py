"""Heuristic project intent detector for Stage 2 memory-system-fix.

Pure function — does NOT mutate ``ThreadProjects`` and does NOT trigger any
side effects. Worker layer interprets the returned :class:`ProjectMatch` and
decides whether to append a reply suffix (D6: "identify only, never auto-bind").

Confidence taxonomy (matches design.md §决策矩阵):

* ``high``   — message contains a path prefix that resolves to a registered
               project root, OR the project id as a standalone token.
               Path prefix is checked first: a concrete file path is the
               strongest user-intent signal (and supports longest-root
               tie-breaking).
* ``medium`` — message contains the project's display name (often Chinese)
               as a contiguous substring.
* ``low``    — fuzzy: message contains a token that *starts with* a registry
               id of ≥4 chars (suffix match within a longer word).
* ``none``   — no match. Worker decides whether to add suffix based on whether
               the message uses history/deployment trigger words.

Matching is case-insensitive for ASCII; Chinese name match is exact substring.
When multiple candidates score the same, the longest id wins.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from feishu_bridge.state_thread_projects import ProjectEntry, normalize_path

log = logging.getLogger(__name__)


# History / deployment trigger words (R2/R3) — worker uses these to decide
# whether a `none`-confidence match still warrants a binding-prompt suffix.
HISTORY_TRIGGER_WORDS = (
    "上次", "之前", "以前", "曾经", "忘了", "想不起来",
    "部署", "历史", "决策", "用过", "怎么跑", "deploy", "history",
)


@dataclass(frozen=True)
class ProjectMatch:
    project_id: str
    confidence: str   # one of high / medium / low / none
    matched_via: str  # debug breadcrumb: 'id_token' | 'path_prefix' | 'name' | 'fuzzy_prefix'


# ── matchers ────────────────────────────────────────────────────────────────


_ID_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*")


def _id_tokens(text: str) -> set[str]:
    """Extract identifier-like tokens (alphanumeric + ``-_``) lowercased."""
    return {m.group(0).lower() for m in _ID_TOKEN_RE.finditer(text)}


def _match_id_literal(text_tokens: set[str], entries: list[ProjectEntry]) -> Optional[ProjectMatch]:
    """High-confidence: project id appears as a standalone token in the message."""
    best: Optional[ProjectEntry] = None
    for e in entries:
        if e.id.lower() in text_tokens:
            if best is None or len(e.id) > len(best.id):
                best = e
    if best:
        return ProjectMatch(project_id=best.id, confidence="high", matched_via="id_token")
    return None


def _match_path_prefix(text: str, entries: list[ProjectEntry]) -> Optional[ProjectMatch]:
    """High-confidence: message contains a path that resolves into a registered project root.

    Tries normalized prefix match so that both ``~/projects/foo`` and the
    absolute form match the same registry entry.
    """
    # Look for path-like substrings using a simple regex covering POSIX paths
    # and ``~/`` shorthand. The pattern intentionally stops at whitespace and
    # common Chinese punctuation to avoid swallowing trailing prose.
    path_re = re.compile(r"(~/[^\s，。、；：（）()]+|/[^\s，。、；：（）()]{2,})")
    candidates = [normalize_path(m.group(0)) for m in path_re.finditer(text)]
    candidates = [c for c in candidates if c]
    if not candidates:
        return None

    # Build normalized registry roots once
    roots: list[tuple[str, ProjectEntry]] = []
    for e in entries:
        norm = normalize_path(e.path)
        if norm:
            roots.append((norm, e))

    best: Optional[ProjectEntry] = None
    best_len = -1
    for cand in candidates:
        for root, e in roots:
            # Either cand equals root, or cand starts with root + os.sep
            if cand == root or cand.startswith(root + os.sep):
                if len(root) > best_len:
                    best = e
                    best_len = len(root)
    if best:
        return ProjectMatch(project_id=best.id, confidence="high", matched_via="path_prefix")
    return None


def _match_name(text: str, entries: list[ProjectEntry]) -> Optional[ProjectMatch]:
    """Medium-confidence: project display name (often Chinese) as substring."""
    best: Optional[ProjectEntry] = None
    for e in entries:
        name = (e.name or "").strip()
        # Skip 1- or 2-character names — false positive risk too high.
        if len(name) < 3:
            continue
        if name and name in text:
            if best is None or len(name) > len((best.name or "").strip()):
                best = e
    if best:
        return ProjectMatch(project_id=best.id, confidence="medium", matched_via="name")
    return None


def _match_fuzzy_prefix(
    text_tokens: set[str], entries: list[ProjectEntry]
) -> Optional[ProjectMatch]:
    """Low-confidence: a message token starts with a project id ≥4 chars.

    Catches forms like ``feishu-bridges`` or hyphenated derivatives without
    triggering on incidental short overlaps.
    """
    best: Optional[ProjectEntry] = None
    for e in entries:
        ident = e.id.lower()
        if len(ident) < 4:
            continue
        for tok in text_tokens:
            if tok == ident:
                continue  # already covered by id-literal pass
            if tok.startswith(ident) and len(tok) > len(ident):
                if best is None or len(ident) > len(best.id):
                    best = e
                break
    if best:
        return ProjectMatch(project_id=best.id, confidence="low", matched_via="fuzzy_prefix")
    return None


# ── public api ──────────────────────────────────────────────────────────────


def detect_project_intent(
    message: str, projects: Iterable[ProjectEntry]
) -> Optional[ProjectMatch]:
    """Return the best-confidence project match for ``message``, or None.

    Priority order: path prefix > id literal > name > fuzzy prefix.
    """
    text = message or ""
    if not text.strip():
        return None
    entries = [e for e in projects if e and e.id]
    if not entries:
        return None

    tokens = _id_tokens(text)

    for matcher in (
        lambda: _match_path_prefix(text, entries),
        lambda: _match_id_literal(tokens, entries),
        lambda: _match_name(text, entries),
        lambda: _match_fuzzy_prefix(tokens, entries),
    ):
        result = matcher()
        if result is not None:
            return result
    return None


def has_history_trigger(message: str) -> bool:
    """Return True iff ``message`` contains a history/deployment trigger word.

    Worker uses this for the R2/R3 narrow rule: when ``detect_project_intent``
    returns None, only append a binding-suggestion suffix when the user is
    asking a history-ish question (so we don't pester casual chitchat).
    """
    if not message:
        return False
    return any(w in message for w in HISTORY_TRIGGER_WORDS)
