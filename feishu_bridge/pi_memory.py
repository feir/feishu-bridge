"""Per-session persistent memory for the Pi runner.

Ownership model (see .specs/changes/pi-runner-ux-v1/design.md, Item 3):

  WRITER: only pi, via its native ``write``/``edit`` tools. Turns within one
          scope are serialized by the bridge (one in-flight turn per chat), so
          there is a single writer with no concurrency.
  READER: only the bridge (this module). Reads are unlocked and best-effort,
          mirroring ``SessionJournal.read``. The bridge NEVER writes, prunes,
          or replaces this file — that is what removes the multi-writer race
          flagged in plan-review (CRITICAL). There is deliberately no write/
          prune function in this module.

Scope identity: keyed by the runner ``tag`` (``bot:chat:thread`` from
``SessionMap.format_key``), which already encodes (bot_id, chat_id, thread_id).
We hash the tag for the filename — no need to decode it back.

The injected copy is soft-capped to ``MAX_INJECT_BYTES`` (tail kept); the file
on disk is never modified by this cap. Pi keeps the file itself within budget
via the write protocol embedded in the injected prompt.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from feishu_bridge.paths import bridge_home

# Soft cap on the injected copy only (bytes). The on-disk file is untouched.
MAX_INJECT_BYTES = 8192


def _root() -> Path:
    return bridge_home() / "feishu-bridge" / "pi-memory"


def memory_path(tag: str) -> Path:
    """Absolute path of the per-session memory file for ``tag``.

    The tag (``bot:chat:thread``) is hashed so distinct sessions — including
    different threads of the same chat — map to distinct files.
    """
    digest = hashlib.sha1((tag or "").encode("utf-8")).hexdigest()
    return _root() / f"{digest}.md"


def safe_read(tag: str) -> str:
    """Read the memory file (unlocked, best-effort). Missing/unreadable → ""."""
    try:
        path = memory_path(tag)
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def soft_tail_cap(text: str, max_bytes: int = MAX_INJECT_BYTES) -> str:
    """Truncate the *injected copy* to the last ``max_bytes`` bytes.

    Keeps the tail (most recent) and never touches the source file.
    """
    if not text:
        return ""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    tail = data[-max_bytes:].decode("utf-8", errors="ignore")
    return "…(older memory truncated)\n" + tail


def build_injection(tag: str) -> str:
    """Return the 'Persistent memory' system-prompt section for this session.

    Always includes the write protocol + absolute path so the agent knows
    where to persist facts, even when the file is currently empty. Never
    raises — returns "" only if the path itself cannot be resolved.
    """
    try:
        path = memory_path(tag)
        existing = soft_tail_cap(safe_read(tag)).strip()
        body = existing if existing else "(本会话暂无持久记忆)"
        return (
            "\n\n## Persistent memory (this chat)\n"
            f"{body}\n\n"
            "当用户要求记住持久事实/偏好时：先用 read 读取下面这个文件，"
            "保留既有内容，再用 edit 增量更新（不要整体覆盖），"
            f"保持精简（< {MAX_INJECT_BYTES} 字节）。文件绝对路径：\n{path}"
        )
    except Exception:
        return ""
