"""Shared path helpers for background-task state."""

from __future__ import annotations

import os
from pathlib import Path


def bg_home() -> Path:
    """Return the bg-task home directory.

    Defaults to ``~/.feishu-bridge`` for backward compatibility. Operators
    running multiple bridge instances on one host must set
    ``FEISHU_BRIDGE_BG_HOME`` per instance so their SQLite DB and wake socket
    do not collide.
    """
    configured = os.environ.get("FEISHU_BRIDGE_BG_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".feishu-bridge"
