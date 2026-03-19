"""Configuration discovery for Feishu Bridge.

Resolution order:
  1. Explicit path (--config argument)
  2. $FEISHU_BRIDGE_CONFIG environment variable
  3. ~/.config/feishu-bridge/config.json (XDG standard)
"""

import logging
import os
from pathlib import Path

log = logging.getLogger("feishu-bridge")

_XDG_CONFIG_PATH = Path.home() / ".config" / "feishu-bridge" / "config.json"


def resolve_config_path(explicit: str | None = None) -> str:
    """Find config file using the discovery chain.

    Args:
        explicit: Path passed via --config CLI argument (highest priority).

    Returns:
        Resolved config file path.

    Raises:
        SystemExit: If no config file is found.
    """
    # 1. Explicit --config
    if explicit:
        p = Path(explicit).expanduser()
        if p.exists():
            return str(p)
        log.error("Config file not found: %s", explicit)
        raise SystemExit(1)

    # 2. Environment variable
    env_path = os.environ.get("FEISHU_BRIDGE_CONFIG")
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists():
            return str(p)
        log.error("$FEISHU_BRIDGE_CONFIG points to non-existent file: %s", env_path)
        raise SystemExit(1)

    # 3. XDG standard path
    if _XDG_CONFIG_PATH.exists():
        return str(_XDG_CONFIG_PATH)

    # No config found
    log.error(
        "No config file found. Provide one via:\n"
        "  --config <path>\n"
        "  $FEISHU_BRIDGE_CONFIG environment variable\n"
        "  %s",
        _XDG_CONFIG_PATH,
    )
    raise SystemExit(1)
