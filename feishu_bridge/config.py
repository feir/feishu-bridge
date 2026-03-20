"""Configuration discovery for Feishu Bridge.

Resolution order:
  1. Explicit path (--config argument)
  2. $FEISHU_BRIDGE_CONFIG environment variable
  3. ~/.config/feishu-bridge/config.json (XDG standard)

If no config exists and stdin is a TTY, an interactive setup wizard runs.
"""

import json
import logging
import os
import shutil
import sys
from pathlib import Path

log = logging.getLogger("feishu-bridge")

_XDG_CONFIG_PATH = Path.home() / ".config" / "feishu-bridge" / "config.json"


def _prompt(label: str) -> str:
    """Read one line from stdin; exit cleanly on EOF (Ctrl-D)."""
    try:
        return input(label)
    except EOFError:
        print("\n  已取消。", file=sys.stderr)
        raise SystemExit(1)


def _validate_credential(value: str, name: str) -> str:
    """Reject empty or multi-line credential values."""
    value = value.strip()
    if not value:
        print(f"  {name} 不能为空", file=sys.stderr)
        raise SystemExit(1)
    if "\n" in value or "\r" in value or "\t" in value:
        print(f"  {name} 不能包含换行或制表符", file=sys.stderr)
        raise SystemExit(1)
    return value


def _interactive_setup(bot_name: str) -> str:
    """Guide the user through first-time config creation. Returns config path."""
    print("\n✦ Feishu Bridge 首次配置向导\n")
    print("  请先在飞书开放平台创建机器人：")
    print("  https://open.feishu.cn/page/openclaw?form=multiAgent\n")

    app_id = _validate_credential(_prompt("  App ID: "), "App ID")
    app_secret = _validate_credential(_prompt("  App Secret: "), "App Secret")

    default_ws = str(
        Path.home() / ".local" / "share" / "feishu-bridge" / "workspaces" / bot_name
    )
    ws_input = _prompt(f"  工作目录 [{default_ws}]: ").strip()
    workspace = ws_input or default_ws

    # Agent type selection
    agent_type_input = _prompt("  Agent 类型 [claude/codex] (claude): ").strip().lower()
    agent_type = agent_type_input if agent_type_input in ("claude", "codex") else "claude"
    agent_cmd = agent_type  # "claude" or "codex"

    if not shutil.which(agent_cmd):
        print(f"\n  ⚠️  命令 '{agent_cmd}' 未在 PATH 中找到。"
              f"请安装后重试或在配置中设置绝对路径。", file=sys.stderr)
        raise SystemExit(1)

    agent_cfg = {"type": agent_type, "command": agent_cmd, "timeout_seconds": 300}

    config = {
        "bots": [
            {
                "name": bot_name,
                "app_id": "${FEISHU_APP_ID}",
                "app_secret": "${FEISHU_APP_SECRET}",
                "workspace": workspace,
                "allowed_users": ["*"],
            }
        ],
        "agent": agent_cfg,
    }

    config_dir = _XDG_CONFIG_PATH.parent
    config_dir.mkdir(parents=True, exist_ok=True)

    # Credentials → .env with atomic 0o600 permissions (no TOCTOU race)
    env_file = config_dir / ".env"
    env_content = f"FEISHU_APP_ID={app_id}\nFEISHU_APP_SECRET={app_secret}\n"
    fd = os.open(str(env_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(env_content)
    os.chmod(str(env_file), 0o600)  # enforce perms even if file pre-existed

    # Load into current process so ${VAR} substitution works immediately
    os.environ.setdefault("FEISHU_APP_ID", app_id)
    os.environ.setdefault("FEISHU_APP_SECRET", app_secret)

    _XDG_CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    )
    print(f"\n  凭证已写入 {env_file}")
    print(f"  配置已写入 {_XDG_CONFIG_PATH}\n")
    return str(_XDG_CONFIG_PATH)


def resolve_config_path(explicit: str | None = None,
                        bot_name: str | None = None) -> str:
    """Find config file using the discovery chain.

    Args:
        explicit: Path passed via --config CLI argument (highest priority).
        bot_name: Bot name from --bot, used for interactive setup.

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

    # 4. Interactive setup (TTY only)
    if sys.stdin.isatty() and bot_name:
        return _interactive_setup(bot_name)

    # No config found and not interactive
    log.error(
        "No config file found. Provide one via:\n"
        "  --config <path>\n"
        "  $FEISHU_BRIDGE_CONFIG environment variable\n"
        "  %s",
        _XDG_CONFIG_PATH,
    )
    raise SystemExit(1)
