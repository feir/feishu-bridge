#!/usr/bin/env python3
"""Feishu Bridge — Feishu <-> Claude Code CLI bridge.

Single-layer architecture: Feishu WebSocket -> this process -> claude -p.
CLAUDE.md, rules, skills, hooks all preserved (same claude binary, same cwd).

Usage:
    feishu-bridge --bot claude-code [--config path/to/config.json]
"""

import argparse
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import lark_oapi as lark

# Regex for detecting Feishu document/wiki/sheet/bitable URLs in messages.
# Captures (type, token) from URLs like https://xxx.feishu.cn/wiki/TOKEN
_FEISHU_URL_RE = re.compile(
    r'https?://[a-zA-Z0-9_-]+\.feishu\.cn/'
    r'(?P<type>wiki|docx?|sheets|base)'
    r'/(?P<token>[A-Za-z0-9_-]+)',
)

from feishu_bridge.parsers import (
    download_image,
    fetch_card_content,
    fetch_forward_messages,
    fetch_quoted_message,
    parse_interactive_content,
    parse_post_content,
)
from feishu_bridge.commands import BridgeCommandHandler
from feishu_bridge.runtime import (
    BaseRunner,
    ChatTaskQueue,
    ClaudeRunner,
    CodexRunner,
    DEDUP_MAX,
    DEDUP_TTL,
    DEFAULT_TIMEOUT,
    MessageDedup,
    QUEUE_MAX,
    SessionMap,
    SessionQueueFull,
    materialize_data_files,
    _resource_stack,
)
from feishu_bridge.ui import (
    ResponseHandle,
    rebuild_card_with_selection,
    remove_queued_reaction,
    remove_typing_indicator,
)
from feishu_bridge.worker import (
    format_task_detail_bridge as _bridge_worker_format_task_detail,
    idle_compact_mgr,
    process_message as _bridge_worker_process_message,
    start_media_cleanup_timer,
)
from feishu_bridge.api.client import FeishuAPIError
from feishu_bridge.api.tasks import FeishuTasks
from feishu_bridge.api.docs import FeishuDocs
from feishu_bridge.api.sheets import FeishuSheets
from feishu_bridge.api.bitable import FeishuBitable
from feishu_bridge.api.wiki import FeishuWiki
from feishu_bridge.api.comments import FeishuComments
from feishu_bridge.api.calendar import FeishuCalendar
from feishu_bridge.api.search import FeishuSearch

_FEISHU_SERVICES_OK = True  # All deps are declared in pyproject.toml

# ============================================================
# Constants
# ============================================================

WORKER_COUNT = 4

log = logging.getLogger("feishu-bridge")

# Known bridge commands (exact-match whitelist for gate exemption).
_BRIDGE_CMD_EXACT = frozenset({
    "/help", "/new", "/clear", "/reset", "/stop", "/cancel",
    "/compact", "/model", "/agent", "/provider", "/status", "/btw", "/update",
    "/restart-all", "/restart",
})


def _strip_mentions(raw_text: str, mentions) -> str:
    """Remove @mention placeholder keys from text."""
    for mention in (mentions or []):
        mk = getattr(mention, 'key', '')
        if mk:
            raw_text = raw_text.replace(mk, '')
    return raw_text.strip()


def _is_bridge_command(text: str) -> bool:
    """Check if text is a known bridge command (exact first-token match).

    Only /feishu- uses prefix matching (covers /feishu-tasks, /feishu-doc, etc.).
    All other commands require exact match to prevent gate bypass via crafted prefixes.
    """
    t = text.lstrip().lower()
    first_token = t.split(None, 1)[0] if t else ""
    return first_token in _BRIDGE_CMD_EXACT or first_token.startswith("/feishu-")


def _reject_not_owner(client, chat_id, thread_id, message_id):
    """Send rejection for non-owner destructive command attempt."""
    try:
        ResponseHandle(client, chat_id, thread_id, message_id).deliver(
            "该命令仅限 bot 管理员使用。")
    except Exception:
        log.warning("Failed to send owner-reject message", exc_info=True)


# ============================================================
# Config
# ============================================================

_RUNNER_CLASSES: dict[str, type[BaseRunner]] = {
    "claude": ClaudeRunner,
    "codex": CodexRunner,
}


def _normalize_prompt_config(prompt_cfg: object, *, fill_defaults: bool) -> dict[str, object]:
    """Normalize bridge-controlled prompt injection settings."""
    raw = prompt_cfg if isinstance(prompt_cfg, dict) else {}
    normalized: dict[str, object] = {}

    if fill_defaults or "safety" in raw:
        safety = str(raw.get("safety", "full")).strip().lower()
        normalized["safety"] = safety if safety in {"full", "minimal", "off"} else "full"
    if fill_defaults or "feishu_cli" in raw:
        normalized["feishu_cli"] = bool(raw.get("feishu_cli", True))
    if fill_defaults or "cron_mgr" in raw:
        normalized["cron_mgr"] = bool(raw.get("cron_mgr", True))
    if "setting_sources" in raw:
        normalized["setting_sources"] = str(raw["setting_sources"])
    return normalized


def _normalize_provider_models(provider_cfg: dict) -> dict[str, str]:
    """Return optional per-agent default models for a provider profile."""
    raw = provider_cfg.get("models")
    if not isinstance(raw, dict):
        return {}
    return {
        str(agent_type).strip().lower(): str(model).strip()
        for agent_type, model in raw.items()
        if str(agent_type).strip() and str(model).strip()
    }


def _normalize_agent_args(agent_cfg: dict) -> dict[str, list[str]]:
    """Return optional per-agent CLI args in normalized form."""
    raw = agent_cfg.get("args_by_type")
    args_by_type = raw.copy() if isinstance(raw, dict) else {}
    current_type = agent_cfg.get("type")
    current_args = agent_cfg.get("args")
    if current_type and current_args is not None:
        args_by_type.setdefault(current_type, current_args)

    normalized: dict[str, list[str]] = {}
    for agent_type, values in args_by_type.items():
        key = str(agent_type).strip().lower()
        if not key:
            continue
        if isinstance(values, str):
            normalized[key] = [values]
        elif isinstance(values, list):
            normalized[key] = [str(v) for v in values if str(v).strip()]
    return normalized


def _normalize_agent_env(agent_cfg: dict) -> dict[str, dict[str, str]]:
    """Return optional per-agent environment overrides in normalized form."""
    raw = agent_cfg.get("env_by_type")
    env_by_type = raw.copy() if isinstance(raw, dict) else {}
    current_type = agent_cfg.get("type")
    current_env = agent_cfg.get("env")
    if current_type and isinstance(current_env, dict):
        env_by_type.setdefault(current_type, current_env)

    normalized: dict[str, dict[str, str]] = {}
    for agent_type, values in env_by_type.items():
        key = str(agent_type).strip().lower()
        if not key or not isinstance(values, dict):
            continue
        normalized[key] = {
            str(k): str(v) for k, v in values.items() if str(k).strip()
        }
    return normalized


def _normalize_provider_profiles(agent_cfg: dict) -> dict[str, dict]:
    """Return normalized provider profiles keyed by profile name."""
    raw = agent_cfg.get("providers")
    profiles = raw.copy() if isinstance(raw, dict) else {}
    normalized: dict[str, dict] = {"default": {}}
    for name, cfg in profiles.items():
        key = str(name).strip().lower()
        if not key or not isinstance(cfg, dict):
            continue
        normalized[key] = {
            "commands": _normalize_agent_commands(cfg),
            "args_by_type": _normalize_agent_args(cfg),
            "env_by_type": _normalize_agent_env(cfg),
            "models": _normalize_provider_models(cfg),
            "prompt": _normalize_prompt_config(cfg.get("prompt"), fill_defaults=False),
        }
    return normalized


def _normalize_agent_commands(agent_cfg: dict) -> dict[str, str]:
    """Return optional per-agent command overrides in normalized form."""
    raw = agent_cfg.get("commands")
    commands = raw.copy() if isinstance(raw, dict) else {}
    current_type = agent_cfg.get("type")
    current_cmd = agent_cfg.get("command")
    if current_type and current_cmd:
        commands.setdefault(current_type, current_cmd)
    return {
        str(k).strip().lower(): str(v).strip()
        for k, v in commands.items()
        if str(k).strip() and str(v).strip()
    }


def resolve_agent_command(agent_cfg: dict, agent_type: str) -> tuple[str | None, str]:
    """Resolve the CLI command for an agent type."""
    commands = _normalize_agent_commands(agent_cfg)
    configured = commands.get(agent_type, agent_type)
    return shutil.which(configured), configured


def resolve_provider_name(agent_cfg: dict) -> str:
    """Resolve the active provider profile name."""
    provider = str(agent_cfg.get("provider", "default")).strip().lower() or "default"
    profiles = _normalize_provider_profiles(agent_cfg)
    return provider if provider in profiles else "default"


def _provider_profile(agent_cfg: dict, provider_name: str | None = None) -> dict:
    """Return normalized provider profile for the active or specified provider."""
    provider = provider_name or resolve_provider_name(agent_cfg)
    return _normalize_provider_profiles(agent_cfg).get(provider, {})


def resolve_effective_agent_command(agent_cfg: dict, agent_type: str) -> tuple[str | None, str]:
    """Resolve the CLI command for an agent type under the active provider."""
    configured = _provider_profile(agent_cfg).get("commands", {}).get(agent_type)
    if configured:
        return shutil.which(configured), configured
    return resolve_agent_command(agent_cfg, agent_type)


def resolve_agent_args(agent_cfg: dict, agent_type: str) -> list[str]:
    """Resolve extra CLI args for an agent type."""
    provider_args = _provider_profile(agent_cfg).get("args_by_type", {}).get(agent_type)
    if provider_args is not None:
        return provider_args
    return _normalize_agent_args(agent_cfg).get(agent_type, [])


def resolve_agent_env(agent_cfg: dict, agent_type: str) -> dict[str, str]:
    """Resolve fixed environment overrides for an agent type."""
    env = dict(_normalize_agent_env(agent_cfg).get(agent_type, {}))
    env.update(_provider_profile(agent_cfg).get("env_by_type", {}).get(agent_type, {}))
    return env


def resolve_agent_model(agent_cfg: dict, agent_type: str) -> str | None:
    """Resolve provider-specific default model for an agent type."""
    return _provider_profile(agent_cfg).get("models", {}).get(agent_type)


def resolve_prompt_config(agent_cfg: dict) -> dict[str, object]:
    """Resolve bridge-controlled prompt injection settings."""
    prompt_cfg = dict(_normalize_prompt_config(agent_cfg.get("prompt"), fill_defaults=True))
    prompt_cfg.update(_provider_profile(agent_cfg).get("prompt", {}))
    return _normalize_prompt_config(prompt_cfg, fill_defaults=True)


def build_extra_prompts(agent_cfg: dict) -> list[str]:
    """Build bridge-managed system prompt fragments for the active provider."""
    prompt_cfg = resolve_prompt_config(agent_cfg)
    extra_prompts: list[str] = []

    if prompt_cfg.get("feishu_cli", True):
        cli_abs = shutil.which("feishu-cli")
        if cli_abs:
            try:
                cli_result = subprocess.run(
                    [cli_abs, "prompt", "--summary"],
                    capture_output=True, text=True, timeout=5,
                )
                if cli_result.returncode == 0 and cli_result.stdout.strip():
                    extra_prompts.append(cli_result.stdout)
            except (subprocess.TimeoutExpired, OSError):
                pass

    if prompt_cfg.get("cron_mgr", True):
        cron_mgr_abs = shutil.which("cron-mgr")
        if cron_mgr_abs:
            try:
                cron_result = subprocess.run(
                    [cron_mgr_abs, "prompt"],
                    capture_output=True, text=True, timeout=5,
                )
                if cron_result.returncode == 0 and cron_result.stdout.strip():
                    cron_text = cron_result.stdout.replace("cron-mgr", cron_mgr_abs)
                    extra_prompts.append(cron_text)
            except (subprocess.TimeoutExpired, OSError):
                pass

    return extra_prompts


def session_identity(agent_cfg: dict) -> str:
    """Return the identity marker used for session compatibility."""
    agent_type = str(agent_cfg.get("type", "")).strip().lower()
    provider = resolve_provider_name(agent_cfg)
    return agent_type if provider == "default" else f"{agent_type}:{provider}"




def load_config(config_path: str, bot_name: str) -> dict:
    """Load config JSON, substitute ${VAR} env vars, validate, return config."""
    raw = Path(config_path).read_text()

    # Substitute ${VAR} patterns
    def _sub(m):
        val = os.environ.get(m.group(1))
        return val if val is not None else m.group(0)

    resolved = re.sub(r'\$\{(\w+)\}', _sub, raw)

    # Fail-fast on unresolved placeholders
    remaining = re.findall(r'\$\{(\w+)\}', resolved)
    if remaining:
        log.error("Unresolved env vars in config: %s", remaining)
        sys.exit(1)

    config = json.loads(resolved)

    # Find the bot entry
    bot = None
    for b in config.get("bots", []):
        if b["name"] == bot_name:
            bot = b
            break

    if not bot:
        available = [b["name"] for b in config.get("bots", [])]
        log.error("Bot '%s' not found. Available: %s", bot_name, available)
        sys.exit(1)

    # Validate required bot fields
    for field in ("app_id", "app_secret", "workspace"):
        if not bot.get(field):
            log.error("Bot '%s' missing required field: %s", bot_name, field)
            sys.exit(1)

    # Parse allowed_users from CSV string -> list
    if isinstance(bot.get("allowed_users"), str):
        bot["allowed_users"] = [
            s.strip() for s in bot["allowed_users"].split(",") if s.strip()
        ]

    # Parse allowed_chats from CSV string -> list (if needed)
    if isinstance(bot.get("allowed_chats"), str):
        bot["allowed_chats"] = [
            s.strip() for s in bot["allowed_chats"].split(",") if s.strip()
        ]

    # Validate allowed_users
    users = bot.get("allowed_users", [])
    if not isinstance(users, list) or len(users) == 0:
        log.error("allowed_users must be a non-empty list, got: %s", users)
        sys.exit(1)
    if not all(isinstance(x, str) and x for x in users):
        log.error("allowed_users entries must be non-empty strings")
        sys.exit(1)

    # Migrate legacy "claude" key → "agent" (backward compat)
    if "claude" in config and "agent" not in config:
        claude_legacy = config.pop("claude")
        config["agent"] = {"type": "claude", **claude_legacy}
        log.info("Migrated config: 'claude' key → 'agent' with type=claude")

    agent_cfg = config.get("agent", {"type": "claude", "command": "claude"})
    agent_type = agent_cfg.get("type")
    if not agent_type:
        log.error("agent.type is required (claude or codex)")
        sys.exit(1)
    if agent_type not in _RUNNER_CLASSES:
        log.error("Unknown agent type '%s'. Supported: %s",
                  agent_type, list(_RUNNER_CLASSES.keys()))
        sys.exit(1)

    # Resolve agent CLI command
    default_cmd = "claude" if agent_type == "claude" else agent_type
    agent_cfg.setdefault("command", default_cmd)
    agent_cfg["provider"] = resolve_provider_name(agent_cfg)
    agent_cfg["commands"] = _normalize_agent_commands(agent_cfg)
    agent_cfg["args_by_type"] = _normalize_agent_args(agent_cfg)
    agent_cfg["env_by_type"] = _normalize_agent_env(agent_cfg)
    agent_cfg["prompt"] = _normalize_prompt_config(agent_cfg.get("prompt"), fill_defaults=True)
    agent_cfg["providers"] = _normalize_provider_profiles(agent_cfg)
    resolved_cmd, agent_cmd = resolve_effective_agent_command(agent_cfg, agent_type)
    if not resolved_cmd:
        log.error(
            "Agent command '%s' not found in PATH. "
            "Set absolute path in config or update PATH.", agent_cmd
        )
        sys.exit(1)
    agent_cfg["_resolved_command"] = resolved_cmd
    log.info("Agent CLI (%s): %s", agent_type, resolved_cmd)

    # ------------------------------------------------------------------
    # group_policy validation
    # ------------------------------------------------------------------
    _VALID_MODES = {"owner-only", "mention-all", "auto-reply", "disabled"}
    gp = bot.get("group_policy")
    if gp is not None:
        dm = gp.get("default_mode")
        if not dm:
            log.error("group_policy requires 'default_mode' field")
            sys.exit(1)
        if dm not in _VALID_MODES:
            log.error("group_policy.default_mode '%s' invalid; must be one of %s",
                      dm, _VALID_MODES)
            sys.exit(1)
        owner = gp.get("owner")
        allowed = bot.get("allowed_users", ["*"])

        # owner-only without owner
        if dm == "owner-only" and not owner:
            log.warning("group_policy.default_mode is 'owner-only' but 'owner' "
                        "is not set — all owner-only groups will reject messages")

        # mention-all / auto-reply with restricted allowed_users
        if dm in ("mention-all", "auto-reply") and allowed != ["*"]:
            log.warning("group_policy.default_mode '%s' with restricted "
                        "allowed_users — non-listed users will be silently "
                        "rejected before group gate", dm)

        # owner-only with restricted allowed_users that doesn't include owner
        if dm == "owner-only" and owner and allowed != ["*"] and owner not in allowed:
            log.warning("group_policy owner '%s' not in allowed_users — "
                        "owner messages will be rejected before gate", owner)

        # Per-group overrides validation
        for gid, gcfg in gp.get("groups", {}).items():
            gmode = gcfg.get("mode")
            if not gmode or gmode not in _VALID_MODES:
                log.warning("group_policy.groups['%s'] has invalid mode '%s', "
                            "will fall back to default_mode '%s'", gid, gmode, dm)
                gcfg["mode"] = dm  # normalize to default
            resolved = gcfg.get("mode", dm)
            if resolved in ("mention-all", "auto-reply") and allowed != ["*"]:
                log.warning("group '%s' mode '%s' with restricted allowed_users "
                            "— non-listed users silently rejected", gid, resolved)
            if resolved == "owner-only" and not owner:
                log.warning("group '%s' mode 'owner-only' but owner not set "
                            "— group messages will be rejected", gid)
            if resolved == "owner-only" and owner and allowed != ["*"] and owner not in allowed:
                log.warning("group '%s' mode 'owner-only' but owner '%s' not in "
                            "allowed_users — owner messages will be rejected before gate", gid, owner)

    # Log config (mask secrets)
    masked = {**bot, "app_secret": "***"}
    log.info("Bot config: %s", json.dumps(masked, ensure_ascii=False))

    all_bot_names = [b["name"] for b in config.get("bots", []) if b.get("name")]
    return {"bot": bot, "agent": agent_cfg, "dedup": config.get("dedup", {}),
            "todo_auto_drive": config.get("todo_auto_drive", True),
            "all_bot_names": all_bot_names}

def create_runner(agent_cfg: dict, bot_cfg: dict,
                  extra_prompts: list[str]) -> BaseRunner:
    """Factory: create the appropriate Runner based on agent.type."""
    agent_type = agent_cfg["type"]
    runner_cls = _RUNNER_CLASSES[agent_type]  # validated in load_config()
    model = resolve_agent_model(agent_cfg, agent_type) or bot_cfg.get("model")
    prompt_cfg = resolve_prompt_config(agent_cfg)
    return runner_cls(
        command=agent_cfg["_resolved_command"],
        model=model,
        workspace=bot_cfg["workspace"],
        timeout=agent_cfg.get("timeout_seconds", DEFAULT_TIMEOUT),
        max_budget_usd=agent_cfg.get("max_budget_usd"),
        extra_system_prompts=extra_prompts,
        extra_cli_args=resolve_agent_args(agent_cfg, agent_type),
        fixed_env=resolve_agent_env(agent_cfg, agent_type),
        safety_prompt_mode=str(prompt_cfg.get("safety", "full")),
        setting_sources=prompt_cfg.get("setting_sources"),
    )


# ============================================================
# Message Processing (Worker)
# ============================================================

def _format_task_detail_bridge(task: dict) -> str:
    return _bridge_worker_format_task_detail(task)


def process_message(item: dict, bot_config: dict, lark_client,
                    session_map: SessionMap, runner: BaseRunner,
                    feishu_tasks=None, feishu_docs=None, feishu_sheets=None):
    """Compatibility wrapper for the worker pipeline implementation."""
    return _bridge_worker_process_message(
        item=item,
        bot_config=bot_config,
        lark_client=lark_client,
        session_map=session_map,
        runner=runner,
        feishu_tasks=feishu_tasks,
        feishu_docs=feishu_docs,
        feishu_sheets=feishu_sheets,
        feishu_api_error_cls=FeishuAPIError,
        response_handle_cls=ResponseHandle,
        download_image_fn=download_image,
        fetch_card_content_fn=fetch_card_content,
        fetch_forward_messages_fn=fetch_forward_messages,
        fetch_quoted_message_fn=fetch_quoted_message,
        remove_typing_indicator_fn=remove_typing_indicator,
    )


# ============================================================
# FeishuBot
# ============================================================

class FeishuBot:
    """Feishu WebSocket bot with non-blocking message handling."""

    def __init__(self, config: dict):
        self.bot_config = config["bot"]
        self.agent_config = config["agent"]
        self.dedup_config = config.get("dedup", {})

        self.bot_id = self.bot_config["name"]
        self.app_id = self.bot_config["app_id"]
        self.app_secret = self.bot_config["app_secret"]
        self.workspace = self.bot_config["workspace"]
        self.allowed_users = self.bot_config.get("allowed_users", ["*"])
        self.allowed_chats = self.bot_config.get("allowed_chats", ["*"])
        self._todo_auto_drive = config.get("todo_auto_drive", True)
        self._all_bot_names = config.get("all_bot_names", [self.bot_id])
        self.bot_open_id: str | None = None  # set by main() via fetch_bot_info

        # Group policy (None = legacy/compat mode: process all messages)
        gp = self.bot_config.get("group_policy")
        if gp:
            self._group_default_mode = gp.get("default_mode", "auto-reply")
            self._group_owner = gp.get("owner")
            self._group_overrides: dict = gp.get("groups", {})
        else:
            self._group_default_mode = None
            self._group_owner = None
            self._group_overrides = {}
        self._session_cost: dict[str, dict] = {}  # sid -> last {usage, model_usage, total_cost_usd}
        self._session_map_path = (
            Path(self.workspace) / "state" / "feishu-bridge" / f"sessions-{self.bot_id}.json"
        )

        # Components
        self.dedup = MessageDedup(
            ttl=self.dedup_config.get("ttl_seconds", DEDUP_TTL),
            max_entries=self.dedup_config.get("max_entries", DEDUP_MAX),
        )
        self.session_map = SessionMap(
            self._session_map_path,
            agent_type=session_identity(self.agent_config),
        )
        self._extra_prompts = build_extra_prompts(self.agent_config)
        self.runner = create_runner(
            self.agent_config, self.bot_config,
            self._extra_prompts,
        )
        self.command_handler = BridgeCommandHandler(self)

        # Quota poller (claude.ai API, cookie-based auth)
        from feishu_bridge.quota import QuotaPoller
        quota_cfg = config.get("quota", {})
        self._quota_poller = QuotaPoller(
            cookie_path=quota_cfg.get("cookie_path"),
            poll_interval=quota_cfg.get("poll_interval", 300),
            org_uuid=quota_cfg.get("org_uuid"),
        )
        self._quota_poller.start()

        # Work queue: ChatTaskQueue manages per-session FIFO,
        # _work_queue is the shared worker pool queue
        self._work_queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._chat_queue = ChatTaskQueue(self._work_queue)
        idle_compact_mgr.bind(self._chat_queue.enqueue)
        self._io_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="io")

        # Lark REST client (for sending messages)
        self.lark_client = lark.Client.builder() \
            .app_id(self.app_id) \
            .app_secret(self.app_secret) \
            .domain(lark.FEISHU_DOMAIN) \
            .log_level(lark.LogLevel.WARNING) \
            .build()

        # Feishu API services (with auto-auth via Device Flow OAuth)
        if _FEISHU_SERVICES_OK:
            _args = (self.app_id, self.app_secret, self.lark_client)
            self.feishu_tasks = FeishuTasks(*_args)
            self.feishu_docs = FeishuDocs(*_args)
            self.feishu_sheets = FeishuSheets(*_args)
            self.feishu_bitable = FeishuBitable(*_args)
            self.feishu_wiki = FeishuWiki(*_args)
            self.feishu_comments = FeishuComments(*_args)
            self.feishu_calendar = FeishuCalendar(*_args)
            self.feishu_search = FeishuSearch(*_args)
        else:
            self.feishu_tasks = None
            self.feishu_docs = None
            self.feishu_sheets = None
            self.feishu_bitable = None
            self.feishu_wiki = None
            self.feishu_comments = None
            self.feishu_calendar = None
            self.feishu_search = None
            log.warning("Feishu API services unavailable (missing dependencies)")

    def switch_provider(self, provider_name: str) -> tuple[bool, str]:
        """Hot-swap the active provider profile for the current agent."""
        target = (provider_name or "").strip().lower()
        profiles = self.agent_config.get("providers", {"default": {}})
        if target not in profiles:
            supported = " / ".join(sorted(profiles))
            return False, f"未知 Provider: `{provider_name}`。可选: {supported}"

        current = resolve_provider_name(self.agent_config)
        if current == target:
            return True, f"当前 Provider 已是 `{target}`。"

        next_cfg = dict(self.agent_config)
        next_cfg["provider"] = target
        next_cfg["commands"] = _normalize_agent_commands(next_cfg)
        next_cfg["args_by_type"] = _normalize_agent_args(next_cfg)
        next_cfg["env_by_type"] = _normalize_agent_env(next_cfg)
        next_cfg["providers"] = _normalize_provider_profiles(next_cfg)
        target_type = next_cfg["type"]
        resolved_cmd, configured_cmd = resolve_effective_agent_command(next_cfg, target_type)
        if not resolved_cmd:
            return (
                False,
                f"Agent 命令 `{configured_cmd}` 未在 PATH 中找到，无法切换到 `{target}` provider。",
            )
        next_cfg["command"] = configured_cmd
        next_cfg["_resolved_command"] = resolved_cmd

        next_bot_cfg = dict(self.bot_config)
        next_prompts = build_extra_prompts(next_cfg)
        next_runner = create_runner(next_cfg, next_bot_cfg, next_prompts)

        self.agent_config = next_cfg
        self._extra_prompts = next_prompts
        self.runner = next_runner
        self.session_map = SessionMap(self._session_map_path, agent_type=session_identity(next_cfg))
        self._session_cost.clear()

        log.info("Switched provider for bot %s: %s -> %s (%s/%s)",
                 self.bot_id, current, target, target_type, resolved_cmd)
        return True, f"Provider 已切换为 `{target}`。"

    def switch_agent(self, agent_type: str) -> tuple[bool, str, str | None]:
        """Hot-swap the bot's backend runner."""
        target_type = (agent_type or "").strip().lower()
        if target_type not in _RUNNER_CLASSES:
            supported = " / ".join(sorted(_RUNNER_CLASSES))
            return False, f"未知 Agent 类型: `{agent_type}`。可选: {supported}", None

        current_type = self.agent_config.get("type")
        if current_type == target_type:
            return (
                True,
                f"当前 Agent 已是 `{target_type}`。",
                self.agent_config.get("_resolved_command"),
            )

        next_cfg = dict(self.agent_config)
        next_cfg["type"] = target_type
        next_cfg["commands"] = _normalize_agent_commands(next_cfg)
        next_cfg["args_by_type"] = _normalize_agent_args(next_cfg)
        next_cfg["env_by_type"] = _normalize_agent_env(next_cfg)
        next_cfg["providers"] = _normalize_provider_profiles(next_cfg)

        resolved_cmd, configured_cmd = resolve_effective_agent_command(next_cfg, target_type)
        if not resolved_cmd:
            return (
                False,
                f"Agent 命令 `{configured_cmd}` 未在 PATH 中找到，无法切换到 `{target_type}`。",
                None,
            )

        next_cfg["command"] = configured_cmd
        next_cfg["_resolved_command"] = resolved_cmd

        next_bot_cfg = dict(self.bot_config)
        next_prompts = build_extra_prompts(next_cfg)
        next_runner = create_runner(next_cfg, next_bot_cfg, next_prompts)

        self.agent_config = next_cfg
        self._extra_prompts = next_prompts
        self.runner = next_runner
        self.session_map = SessionMap(self._session_map_path, agent_type=session_identity(next_cfg))
        self._session_cost.clear()

        log.info("Switched agent for bot %s: %s -> %s (%s)",
                 self.bot_id, current_type, target_type, resolved_cmd)
        return True, f"Agent 已切换为 `{target_type}`。", resolved_cmd

    def _check_group_gate(self, chat_type, sender_id, mentions, chat_id) -> bool:
        """Group chat gate policy. Returns True to allow, False to reject."""
        # p2p (DM) — always pass, no group gate
        if chat_type == "p2p":
            return True

        # No group_policy configured — compat mode, pass all
        if self._group_default_mode is None:
            return True

        # Resolve mode for this chat (per-group override or default)
        override = self._group_overrides.get(chat_id)
        mode = (override.get("mode", self._group_default_mode)
                if override else self._group_default_mode)

        if mode == "auto-reply":
            return True

        if mode == "disabled":
            log.debug("Group gate REJECT (disabled): chat=%s", chat_id)
            return False

        # Check if bot was @mentioned
        bot_mentioned = False
        if self.bot_open_id:
            for m in (mentions or []):
                mid = getattr(m, 'id', None)
                if mid and getattr(mid, 'open_id', None) == self.bot_open_id:
                    bot_mentioned = True
                    break

        if mode == "mention-all":
            if not self.bot_open_id:
                # Degradation: pass-through when bot_open_id unknown
                log.warning("Group gate pass-through (mention-all, "
                            "bot_open_id=None): chat=%s", chat_id)
                return True
            if bot_mentioned:
                return True
            log.debug("Group gate REJECT (mention-all, no @bot): "
                      "chat=%s sender=%s", chat_id, sender_id)
            return False

        if mode == "owner-only":
            is_owner = ((sender_id == self._group_owner)
                        if self._group_owner else False)
            if not is_owner:
                log.debug("Group gate REJECT (owner-only, not owner): "
                          "chat=%s sender=%s", chat_id, sender_id)
                return False
            # Owner must also @bot (AND semantics)
            if not self.bot_open_id:
                # Degradation: skip @bot check, keep sender=owner
                log.warning("Group gate pass (owner-only, bot_open_id=None, "
                            "sender=owner): chat=%s", chat_id)
                return True
            if bot_mentioned:
                return True
            log.debug("Group gate REJECT (owner-only, owner but no @bot): "
                      "chat=%s", chat_id)
            return False

        # Unknown mode (should not happen after validation) — reject
        log.warning("Group gate REJECT (unknown mode '%s'): chat=%s",
                    mode, chat_id)
        return False

    def start(self):
        """Start worker threads and WebSocket connection (blocking)."""
        # Record startup time — used to skip replayed events after restart
        self._startup_ms = str(int(time.time() * 1000))
        log.info("Bridge startup timestamp: %s", self._startup_ms)

        # Start workers
        for i in range(WORKER_COUNT):
            t = threading.Thread(
                target=self._worker_loop, daemon=True, name=f"worker-{i}"
            )
            t.start()
        log.info("Started %d worker threads", WORKER_COUNT)

        # Start periodic cleanup of stale downloaded media
        start_media_cleanup_timer(self.workspace)

        # Build event handler (empty encrypt_key/token for WS mode)
        # Register reaction events with no-op handler to suppress SDK ERROR logs
        def _noop_reaction(data):
            pass
        handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._on_message) \
            .register_p2_im_message_reaction_created_v1(_noop_reaction) \
            .register_p2_im_message_reaction_deleted_v1(_noop_reaction) \
            .register_p2_card_action_trigger(self._on_card_action) \
            .build()

        # Start WebSocket (blocking, auto-reconnect)
        ws_client = lark.ws.Client(
            app_id=self.app_id,
            app_secret=self.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
            domain=lark.FEISHU_DOMAIN,
            auto_reconnect=True,
        )

        log.info("Connecting to Feishu WebSocket (bot=%s)...", self.bot_id)
        ws_client.start()  # Blocks forever

    def _on_message(self, data):
        """Event handler — MUST return immediately, zero network I/O."""
        try:
            event = data.event
            msg = event.message
            sender = event.sender

            message_id = msg.message_id
            chat_id = msg.chat_id
            msg_type = msg.message_type
            thread_id = getattr(msg, 'thread_id', None) or None
            parent_id = getattr(msg, 'parent_id', None) or None
            sender_type = getattr(sender, "sender_type", None) if sender else None
            sender_id = (sender.sender_id.open_id
                         if sender and sender.sender_id else None)

            # Dedup (in-memory only)
            if self.dedup.is_duplicate(message_id):
                return

            # Skip messages created before this bridge instance started
            # (prevents restart storm from replayed WebSocket events)
            msg_create_time = getattr(msg, "create_time", None) or "0"
            if msg_create_time < self._startup_ms:
                log.info("Skipping stale message %s (create_time=%s < startup=%s)",
                         message_id, msg_create_time, self._startup_ms)
                return

            # Permission: sender
            if not sender_id:
                log.debug("Rejected message with no sender_id (system)")
                return
            if ("*" not in self.allowed_users
                    and sender_id not in self.allowed_users):
                log.info("Unauthorized sender: %s (type=%s)",
                         sender_id, sender_type)
                return

            # Permission: chat
            if ("*" not in self.allowed_chats
                    and chat_id not in self.allowed_chats):
                log.debug("Unauthorized chat: %s", chat_id)
                return

            # ----------------------------------------------------------
            # Group chat gate (after allowed_chats, before msg parse)
            # ----------------------------------------------------------
            chat_type = getattr(msg, 'chat_type', None)

            try:
                content = json.loads(msg.content)
            except (json.JSONDecodeError, TypeError):
                content = {}

            # Command exemption: known bridge commands skip gate
            _skip_gate = False
            _msg_mentions = getattr(msg, 'mentions', None) or []
            if msg_type == "text":
                _pre_text = _strip_mentions(
                    content.get("text", ""), _msg_mentions)
                if _is_bridge_command(_pre_text):
                    _skip_gate = True
            elif msg_type == "post":
                _pre_text = _strip_mentions(
                    parse_post_content(content), _msg_mentions)
                if _is_bridge_command(_pre_text):
                    _skip_gate = True

            if not _skip_gate:
                if not self._check_group_gate(
                        chat_type, sender_id, _msg_mentions, chat_id):
                    return

            # Lightweight parse (no network I/O)
            text = ""
            image_key = None
            file_key = None
            file_name = None

            _todo_task_id = None  # set by todo branch when auto_drive=True
            _card_message_id = None  # set for interactive msgs needing API re-fetch
            _merge_forward_message_id = None  # set for merge_forward expansion

            if msg_type == "text":
                text = _strip_mentions(
                    content.get("text", ""), _msg_mentions)
                if not text:
                    return

            elif msg_type == "image":
                image_key = content.get("image_key")
                if not image_key:
                    return

            elif msg_type == "file":
                file_key = content.get("file_key")
                file_name = content.get("file_name", "attachment")
                if not file_key:
                    return

            elif msg_type == "post":
                # Rich-text message — use shared parser + strip mentions
                text = _strip_mentions(
                    parse_post_content(content), _msg_mentions)
                if not text:
                    return

            elif msg_type == "todo":
                # Feishu task share — extract task info as context for Claude
                task_id = content.get("task_id", "")
                summary_obj = content.get("summary", {})
                # Extract plain text from rich-text content blocks
                todo_title = ""
                for para in summary_obj.get("content", []):
                    for seg in para:
                        if seg.get("tag") == "text":
                            todo_title += seg.get("text", "")
                todo_title = todo_title.strip() or "未命名任务"
                due = content.get("due_time", "")
                due_str = f"，截止时间: {due}" if due and due != "0000" else ""
                # Fallback text (used when auto_drive=False or API unavailable)
                # NOTE: Must NOT start with "/" to avoid matching bridge commands
                text = (f"[用户分享了一个飞书任务]\n"
                        f"任务: {todo_title}{due_str}\n"
                        f"task_id: {task_id}\n"
                        f"请查看这个任务并提供帮助。")
                # Config guard: auto-drive stores task_id for worker-thread API calls
                if self._todo_auto_drive and task_id:
                    _todo_task_id = task_id

            elif msg_type == "interactive":
                # Forwarded card message — extract text from card elements
                card_title = content.get("title", "")
                card_text = parse_interactive_content(content)
                # Feishu replaces CardKit v2 content with this unhelpful fallback
                _CARD_FALLBACK = "请升级至最新版本客户端，以查看内容"
                if card_text and card_text.strip() != _CARD_FALLBACK:
                    text = card_text
                    if card_title:
                        text = f"[转发卡片: {card_title}]\n{text}"
                else:
                    # Content degraded — worker will re-fetch via API
                    _card_message_id = message_id
                    if card_title:
                        text = f"[用户转发了一条卡片消息: {card_title}]"
                    else:
                        text = "[用户转发了一条卡片消息，内容无法解析]"

            elif msg_type == "merge_forward":
                # Forwarded message bundle — worker will expand via API
                _merge_forward_message_id = message_id
                text = "[用户转发了一条合并消息，正在展开...]"

            else:
                log.info("Unsupported msg_type: %s content=%s",
                         msg_type, msg.content[:200] if msg.content else "")
                return

            # Auto-detect Feishu doc/wiki/sheet/bitable URLs in message text
            _feishu_urls = _FEISHU_URL_RE.findall(text) if text else []
            # _feishu_urls is list of (type, token) tuples

            # Bridge commands (intercept before enqueueing to Claude)
            stripped = text.strip()
            cmd_lower = stripped.lower().split(None, 1)
            cmd = cmd_lower[0] if cmd_lower else ""
            # Preserve original case for arguments
            original_parts = stripped.split(None, 1)
            cmd_arg = original_parts[1] if len(original_parts) > 1 else ""

            bridge_cmd = None
            if cmd == "/restart":
                # Owner guard: destructive command in group chat
                if (self._group_default_mode is not None
                        and chat_type != "p2p"
                        and sender_id != self._group_owner):
                    log.info("Destructive cmd /restart rejected: "
                             "sender %s not owner in group %s",
                             sender_id, chat_id)
                    self._io_executor.submit(
                        _reject_not_owner, self.lark_client,
                        chat_id, thread_id, message_id)
                    return
                # Restart bridge — handled inline (not via queue) because
                # sys.exit() kills workers before they can process the item.
                log.info("Restart requested by user %s in chat %s",
                         sender_id, chat_id)
                try:
                    from feishu_bridge.ui import build_restart_card
                    handle = ResponseHandle(
                        self.lark_client, chat_id, thread_id, message_id,
                    )
                    msg_id = handle._send_card(build_restart_card())
                    if msg_id:
                        # Persist message_id so the new process can patch it
                        state_dir = Path(self.workspace) / "state" / "feishu-bridge"
                        state_dir.mkdir(parents=True, exist_ok=True)
                        restart_file = state_dir / f"restart-{self.bot_id}.json"
                        restart_file.write_text(json.dumps({
                            "message_id": msg_id,
                        }))
                except Exception:
                    log.exception("Failed to send restart confirmation")
                # Give message time to deliver, flush logs, then exit.
                # Non-zero exit triggers restart by both systemd (Restart=on-failure)
                # and launchd (KeepAlive.SuccessfulExit=false).
                def _deferred_exit():
                    logging.shutdown()
                    os._exit(1)
                threading.Timer(0.3, _deferred_exit).start()
                return

            elif cmd == "/restart-all":
                # Owner guard: destructive command in group chat
                if (self._group_default_mode is not None
                        and chat_type != "p2p"
                        and sender_id != self._group_owner):
                    log.info("Destructive cmd /restart-all rejected: "
                             "sender %s not owner in group %s",
                             sender_id, chat_id)
                    self._io_executor.submit(
                        _reject_not_owner, self.lark_client,
                        chat_id, thread_id, message_id)
                    return
                # Restart all bot instances — restart others via systemctl,
                # then exit self (systemd restarts this instance).
                log.info("Restart-all requested by user %s in chat %s",
                         sender_id, chat_id)
                other_bots = [n for n in self._all_bot_names if n != self.bot_id]
                try:
                    from feishu_bridge.ui import build_restart_card
                    handle = ResponseHandle(
                        self.lark_client, chat_id, thread_id, message_id,
                    )
                    label = ", ".join(self._all_bot_names)
                    msg_id = handle._send_card(build_restart_card(
                        f"正在重启所有实例: {label}"))
                    if msg_id:
                        state_dir = Path(self.workspace) / "state" / "feishu-bridge"
                        state_dir.mkdir(parents=True, exist_ok=True)
                        restart_file = state_dir / f"restart-{self.bot_id}.json"
                        restart_file.write_text(json.dumps({
                            "message_id": msg_id,
                        }))
                except Exception:
                    log.exception("Failed to send restart-all confirmation")
                # Restart other instances first
                for name in other_bots:
                    try:
                        subprocess.Popen(
                            ["systemctl", "--user", "restart",
                             f"feishu-bridge@{name}.service"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        log.info("Triggered restart for feishu-bridge@%s", name)
                    except Exception:
                        log.exception("Failed to restart feishu-bridge@%s", name)
                # Then exit self
                def _deferred_exit():
                    logging.shutdown()
                    os._exit(1)
                threading.Timer(0.5, _deferred_exit).start()
                return

            elif cmd in ("/new", "/reset", "/clear"):
                bridge_cmd = "new"
                # Also cancel any running task for this chat
                key = (self.bot_id, chat_id, thread_id)
                tag = SessionMap._key_str(key)
                self.runner.cancel(tag)
                # Session delete deferred to worker under chat lock
            elif cmd in ("/stop", "/cancel"):
                # Owner guard: destructive command in group chat
                if (self._group_default_mode is not None
                        and chat_type != "p2p"
                        and sender_id != self._group_owner):
                    log.info("Destructive cmd %s rejected: "
                             "sender %s not owner in group %s",
                             cmd, sender_id, chat_id)
                    self._io_executor.submit(
                        _reject_not_owner, self.lark_client,
                        chat_id, thread_id, message_id)
                    return
                # Kill active process immediately (non-blocking, safe here)
                key = (self.bot_id, chat_id, thread_id)
                tag = SessionMap._key_str(key)
                cancelled = self.runner.cancel(tag)
                # /stop all: also drain pending queue and cleanup ⏳ reactions
                stop_all = cmd_arg.strip().lower() == "all"
                drained_count = 0
                if stop_all:
                    drained = self._chat_queue.drain(tag)
                    drained_count = len(drained)
                    if drained:
                        def _cleanup_drained(items, client):
                            for d in items:
                                rid = d.get("_queued_reaction_id")
                                mid = d.get("message_id")
                                if rid and mid:
                                    remove_queued_reaction(client, mid, rid)
                        self._io_executor.submit(
                            _cleanup_drained, drained, self.lark_client)
                bridge_cmd = "stop"
                cmd_arg = f"{1 if cancelled else 0}|{drained_count}"
            elif cmd == "/help":
                bridge_cmd = "help"
            elif cmd == "/compact":
                bridge_cmd = "compact"
            elif cmd == "/model":
                bridge_cmd = "model"
            elif cmd == "/agent":
                bridge_cmd = "agent"
            elif cmd == "/provider":
                bridge_cmd = "provider"
            elif cmd == "/status":
                bridge_cmd = "status"
            elif cmd == "/btw":
                bridge_cmd = "btw"
            elif cmd == "/update":
                bridge_cmd = "update"
            elif cmd == "/feishu-tasks":
                bridge_cmd = "feishu-tasks"
            elif cmd == "/feishu-doc":
                bridge_cmd = "feishu-doc"
            elif cmd == "/feishu-sheet":
                bridge_cmd = "feishu-sheet"
            elif cmd == "/feishu-bitable":
                bridge_cmd = "feishu-bitable"

            if bridge_cmd:
                bc_item = {
                    "_bridge_command": bridge_cmd,
                    "_cmd_arg": cmd_arg,
                    "bot_id": self.bot_id,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "message_id": message_id,
                    "sender_id": sender_id,
                    "_queued_reaction_id": None,
                }
                # Heavy commands (/compact, /new, /reset, /clear, /status)
                # serialize with normal messages via ChatTaskQueue.
                # Light commands (/help, /stop, /cancel, /model, /agent, /provider, /feishu-*)
                # go directly to work queue.
                _heavy_cmds = {"new", "compact", "status"}
                if bridge_cmd in _heavy_cmds:
                    bc_key = SessionMap._key_str(
                        (self.bot_id, chat_id, thread_id))
                    bc_item["_queue_key"] = bc_key
                    try:
                        status = self._chat_queue.enqueue(bc_key, bc_item)
                    except SessionQueueFull:
                        self._io_executor.submit(
                            self.command_handler.reply_queue_full,
                            chat_id, thread_id, message_id)
                        return
                    if status == 'queued':
                        self._io_executor.submit(
                            self.command_handler.add_queued_reaction_to_item,
                            bc_item, message_id)
                else:
                    try:
                        self._work_queue.put_nowait(bc_item)
                    except queue.Full:
                        log.warning("Queue full, dropping bridge cmd: %s",
                                    bridge_cmd)
                return

            # Enqueue via ChatTaskQueue (zero I/O, non-blocking)
            item = {
                "bot_id": self.bot_id,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "parent_id": parent_id,
                "message_id": message_id,
                "sender_id": sender_id,
                "text": text,
                "image_key": image_key,
                "file_key": file_key,
                "file_name": file_name,
                "_queued_reaction_id": None,
                "_todo_task_id": _todo_task_id,
                "_card_message_id": _card_message_id,
                "_merge_forward_message_id": _merge_forward_message_id,
                "_feishu_urls": _feishu_urls,
                "_cost_store": self._session_cost,
                "_quota_poller": getattr(self, "_quota_poller", None),
            }
            msg_key = SessionMap._key_str(
                (self.bot_id, chat_id, thread_id))
            item["_queue_key"] = msg_key

            try:
                status = self._chat_queue.enqueue(msg_key, item)
            except SessionQueueFull:
                self._io_executor.submit(
                    self.command_handler.reply_queue_full,
                    chat_id, thread_id, message_id)
                return

            if status == 'queued':
                # Add ⏳ reaction in background (zero I/O on event thread)
                self._io_executor.submit(
                    self.command_handler.add_queued_reaction_to_item,
                    item, message_id)

        except Exception:
            log.exception("on_message error")

    def _on_card_action(self, data):
        """Handle interactive card button clicks — must return within 3 seconds."""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse, CallBackToast, CallBackCard,
        )

        def _toast(type_: str, content: str, card_json: dict | None = None):
            resp = P2CardActionTriggerResponse()
            resp.toast = CallBackToast()
            resp.toast.type = type_
            resp.toast.content = content
            if card_json:
                resp.card = CallBackCard()
                resp.card.type = "raw"
                resp.card.data = card_json
            return resp

        try:
            action = data.event.action
            value = action.value or {}
            label = str(value.get("label", ""))[:200]
            chat_id = value.get("chat_id")
            bot_id = value.get("bot_id")

            # Extract operator identity for auth + sender_id
            operator = getattr(data.event, "operator", None)
            sender_id = getattr(operator, "open_id", None) if operator else None

            if not chat_id or not bot_id:
                log.warning("Card action missing chat_id/bot_id: %s", value)
                return _toast("warning", "按钮已过期")

            # Authorization: enforce same allowed_users / allowed_chats gates
            if sender_id:
                if ("*" not in self.allowed_users
                        and sender_id not in self.allowed_users):
                    log.info("Card action rejected: sender %s not allowed", sender_id)
                    return _toast("warning", "无权操作")
            if ("*" not in self.allowed_chats
                    and chat_id not in self.allowed_chats):
                log.info("Card action rejected: chat %s not allowed", chat_id)
                return _toast("warning", "无权操作")

            log.info("Card action: bot=%s chat=%s sender=%s label=%s",
                     bot_id, chat_id, sender_id, label)

            # Enqueue as a new user message through the normal pipeline
            msg_key = SessionMap._key_str((bot_id, chat_id, None))
            item = {
                "bot_id": bot_id,
                "chat_id": chat_id,
                "thread_id": None,
                "parent_id": None,
                "message_id": None,
                "sender_id": sender_id,
                "text": label,
                "image_key": None,
                "_queued_reaction_id": None,
                "_todo_task_id": None,
                "_card_message_id": None,
                "_merge_forward_message_id": None,
                "_feishu_urls": [],
                "_cost_store": self._session_cost,
                "_quota_poller": getattr(self, "_quota_poller", None),
                "_queue_key": msg_key,
            }
            try:
                self._chat_queue.enqueue(msg_key, item)
            except SessionQueueFull:
                return _toast("warning", "消息过多，请稍后再试")

            # Rebuild card: preserve original content, highlight selection
            card_ref = value.get("card_ref")
            rebuilt = (rebuild_card_with_selection(card_ref, label)
                       if card_ref else None)
            if not rebuilt:
                # Cache miss (process restarted / expired) — minimal fallback
                rebuilt = {
                    "schema": "2.0",
                    "config": {"update_multi": True},
                    "body": {"elements": [{
                        "tag": "markdown",
                        "content": f"已选择: **{label}**",
                    }]},
                }
            return _toast("info", f"已选择: {label}", card_json=rebuilt)

        except Exception:
            log.exception("on_card_action error")
            return _toast("error", "处理失败，请重试")

    def _handle_bridge_command(self, item: dict):
        self._command_handler().handle_bridge_command(item)

    def _command_handler(self) -> BridgeCommandHandler:
        handler = getattr(self, "command_handler", None)
        if handler is None:
            handler = BridgeCommandHandler(self)
            self.command_handler = handler
        return handler

    def _dispatch_task_command(self, arg: str, chat_id: str, sender_id: str) -> str:
        return self._command_handler().dispatch_task_command(arg, chat_id, sender_id)

    def _task_list(self, rest: str, chat_id: str, sender_id: str) -> str:
        return self._command_handler()._task_list(rest, chat_id, sender_id)

    def _task_get(self, rest: str, chat_id: str, sender_id: str) -> str:
        return self._command_handler()._task_get(rest, chat_id, sender_id)

    def _task_subtasks(self, rest: str, chat_id: str, sender_id: str) -> str:
        return self._command_handler()._task_subtasks(rest, chat_id, sender_id)

    def _task_add_subtask(self, rest: str, chat_id: str, sender_id: str) -> str:
        return self._command_handler()._task_add_subtask(rest, chat_id, sender_id)

    def _task_complete(self, rest: str, chat_id: str, sender_id: str) -> str:
        return self._command_handler()._task_complete(rest, chat_id, sender_id)

    def _task_help(self) -> str:
        return self._command_handler()._task_help()

    def _handle_feishu_service(self, item: dict, handle, service: str):
        return self._command_handler()._handle_feishu_service(item, handle, service)

    def _dispatch_feishu_service(self, service: str, arg: str,
                                 chat_id: str, sender_id: str) -> str:
        return self._command_handler().dispatch_feishu_service(
            service, arg, chat_id, sender_id)

    @staticmethod
    def _feishu_service_help(service: str) -> str:
        return BridgeCommandHandler.feishu_service_help(service)

    def _add_queued_reaction_to_item(self, item: dict, message_id: str):
        self._command_handler().add_queued_reaction_to_item(item, message_id)

    def _reply_queue_full(self, chat_id: str, thread_id, message_id: str):
        self._command_handler().reply_queue_full(chat_id, thread_id, message_id)

    def _worker_loop(self):
        """Consumer loop — pulls from work queue and processes.

        ChatTaskQueue guarantees at most one item per session in flight.
        on_complete() in finally block submits next pending or marks idle.
        """
        while True:
            handle = None
            key = None
            try:
                item = self._work_queue.get()
                key = item.get("_queue_key")

                # Remove ⏳ reaction in background (non-blocking)
                queued_reaction = item.pop("_queued_reaction_id", None)
                if queued_reaction and item.get("message_id"):
                    self._io_executor.submit(
                        remove_queued_reaction,
                        self.lark_client, item["message_id"], queued_reaction)

                if item.get("_bridge_command"):
                    self._handle_bridge_command(item)
                else:
                    handle = process_message(
                        item, self.bot_config, self.lark_client,
                        self.session_map, self.runner,
                        feishu_tasks=getattr(self, 'feishu_tasks', None),
                        feishu_docs=getattr(self, 'feishu_docs', None),
                        feishu_sheets=getattr(self, 'feishu_sheets', None),
                    )
            except Exception:
                log.exception("Worker loop error")
            finally:
                # Cancel fallback timer (no-op if already fired or cancelled)
                if handle and getattr(handle, '_card_fallback_timer', None):
                    handle._card_fallback_timer.cancel()
                    handle._card_fallback_timer = None
                # Ensure typing indicator is always removed
                if handle and getattr(handle, '_typing_reaction_id', None) \
                        and handle.source_message_id:
                    remove_typing_indicator(
                        handle.client, handle.source_message_id,
                        handle._typing_reaction_id)
                    handle._typing_reaction_id = None
                # Submit next pending item or mark session idle
                if key:
                    self._chat_queue.on_complete(key)
                self._work_queue.task_done()


# ============================================================
# Startup Validation
# ============================================================

def validate_feishu_token(client) -> bool:
    """Validate Feishu app credentials with a lightweight API call."""
    try:
        from lark_oapi.api.im.v1 import ListChatRequest
        req = ListChatRequest.builder().page_size(1).build()
        resp = client.im.v1.chat.list(req)
        if resp.success():
            log.info("Feishu credentials validated")
            return True
        log.error("Feishu validation failed: code=%s msg=%s", resp.code, resp.msg)
        if "permission" in (resp.msg or "").lower():
            log.error(
                "Check required scopes: im:message, im:message:send_as_bot, "
                "im:message:patch, im:resource"
            )
        return False
    except Exception:
        log.exception("Feishu validation error")
        return False




def fetch_bot_info(client, fallback_name: str = "Claude Code") -> tuple[str, str | None]:
    """Fetch bot display name and open_id from Feishu API.

    Returns (app_name, open_id). Falls back to (fallback_name, None) on failure.
    """
    try:
        req = lark.BaseRequest()
        req.http_method = lark.HttpMethod.GET
        req.uri = '/open-apis/bot/v3/info/'
        req.token_types = {lark.AccessTokenType.TENANT}
        resp = client.request(req)
        if resp.code == 0:
            bot_info = json.loads(resp.raw.content).get('bot', {})
            name = bot_info.get('app_name', '').strip() or fallback_name
            open_id = bot_info.get('open_id', '').strip() or None
            log.info('Bot info from API: name=%s open_id=%s', name, open_id)
            return name, open_id
        log.warning('fetch_bot_info: code=%s, using fallback', resp.code)
    except Exception:
        log.warning('Failed to fetch bot info, using fallback', exc_info=True)
    return fallback_name, None


def _notify_restart_complete(bot):
    """Patch the pre-restart card to show completion, if one exists."""
    state_dir = Path(bot.workspace) / "state" / "feishu-bridge"
    restart_file = state_dir / f"restart-{bot.bot_id}.json"
    if not restart_file.exists():
        return
    try:
        data = json.loads(restart_file.read_text())
        mid = data.get("message_id")
        if mid:
            from feishu_bridge.ui import build_restart_complete_card
            from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
            card = build_restart_complete_card()
            card_json = json.dumps(card, ensure_ascii=False)
            body = PatchMessageRequestBody.builder() \
                .content(card_json).build()
            req = PatchMessageRequest.builder() \
                .message_id(mid).request_body(body).build()
            resp = bot.lark_client.im.v1.message.patch(req)
            if resp.success():
                log.info("Restart-complete card patched: %s", mid)
            else:
                log.warning("Restart-complete patch failed: code=%s msg=%s",
                            resp.code, resp.msg)
    except Exception:
        log.warning("Failed to notify restart completion", exc_info=True)
    finally:
        try:
            restart_file.unlink(missing_ok=True)
        except Exception:
            pass


# ============================================================
# Main
# ============================================================

def _start_ollama_proxies(config: dict) -> None:
    """Start ollama think-proxy for any provider profiles that use it.

    A provider profile opts in by default when ANTHROPIC_BASE_URL points to a
    local Ollama instance (localhost / 127.0.0.1 / ::1).  Opt out by setting
    think_proxy: false.  Customize with think_proxy_port (default 11435) and
    ollama_url (default http://127.0.0.1:11434).
    """
    from urllib.parse import urlparse
    from feishu_bridge.ollama_proxy import start_proxy

    _LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
    agent_cfg = config.get("agent", {})
    # Use raw provider profiles so proxy config keys (think_proxy, think_proxy_port,
    # ollama_url) are not stripped by _normalize_provider_profiles.
    raw_profiles: dict = agent_cfg.get("providers") or {}

    started: dict[int, str] = {}  # port → ollama_url, for collision detection

    for profile_name, profile in raw_profiles.items():
        if not isinstance(profile, dict):
            continue

        env_claude = (profile.get("env_by_type") or {}).get("claude") or {}
        base_url = env_claude.get("ANTHROPIC_BASE_URL", "")
        if not base_url:
            continue

        # Only proxy profiles that point Claude CLI at a local Ollama instance
        try:
            parsed = urlparse(base_url)
            if parsed.hostname not in _LOCAL_HOSTS:
                continue
        except Exception:
            continue

        # Allow opt-out via think_proxy: false
        if profile.get("think_proxy") is False:
            continue

        proxy_port = int(profile.get("think_proxy_port") or 11435)
        ollama_url = profile.get("ollama_url") or "http://127.0.0.1:11434"

        # Reuse existing proxy if same port+upstream; error on port conflict
        if proxy_port in started:
            if started[proxy_port] == ollama_url:
                log.debug("ollama think-proxy port %d already started, reusing for '%s'",
                          proxy_port, profile_name)
            else:
                log.error(
                    "ollama think-proxy port %d already claimed by ollama_url=%s; "
                    "provider '%s' wants %s — set think_proxy_port to a different value",
                    proxy_port, started[proxy_port], profile_name, ollama_url,
                )
            continue

        try:
            start_proxy(port=proxy_port, ollama_url=ollama_url)
            started[proxy_port] = ollama_url
            log.info("ollama think-proxy started for provider '%s' on port %d → %s",
                     profile_name, proxy_port, ollama_url)
        except OSError as e:
            log.warning("Could not start ollama think-proxy for '%s': %s "
                        "(port %d may already be in use)", profile_name, e, proxy_port)


def main():
    parser = argparse.ArgumentParser(
        description="Feishu <-> Claude Code CLI bridge"
    )
    parser.add_argument("--bot", required=True, help="Bot name from config")
    parser.add_argument(
        "--config",
        default=None,
        help="Config file path (default: $FEISHU_BRIDGE_CONFIG or ~/.config/feishu-bridge/config.json)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    # Logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Materialize packaged data files (bridge-settings.json, cli_prompt.md)
    import atexit
    materialize_data_files()
    atexit.register(_resource_stack.close)

    # Load config
    from feishu_bridge.config import resolve_config_path
    config_path = resolve_config_path(args.config, bot_name=args.bot)
    config = load_config(config_path, args.bot)

    # Create bot and run startup tasks in parallel
    bot = FeishuBot(config)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from feishu_bridge.ui import set_bot_display_name

    def _startup_validate():
        if not validate_feishu_token(bot.lark_client):
            log.warning("Feishu token validation failed (may need im:chat:readonly scope). "
                        "Continuing anyway — message delivery will reveal actual issues.")

    def _startup_restart_card():
        _notify_restart_complete(bot)

    # Fetch bot info (name + open_id) before parallel startup tasks
    try:
        name, open_id = fetch_bot_info(
            bot.lark_client,
            fallback_name=bot.runner.get_display_name(),
        )
        set_bot_display_name(name)
        bot.bot_open_id = open_id
        if not open_id:
            log.warning("Bot open_id not available — mention detection will "
                        "use per-mode degradation (see group_policy design)")
    except Exception as e:
        log.warning("Failed to fetch bot info: %s", e)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="startup") as pool:
        futures = [pool.submit(fn) for fn in (
            _startup_validate, _startup_restart_card)]
        for f in as_completed(futures):
            exc = f.exception()
            if exc:
                log.warning("Startup task failed: %s", exc)

    # Start background update checker
    from feishu_bridge.commands import _get_install_info
    from feishu_bridge.updater import init_updater
    _mode, _plat, _src_path = _get_install_info()
    init_updater(_mode, _src_path)

    # Start ollama think-proxy if any provider profile needs it
    _start_ollama_proxies(config)

    # Start (blocking)
    from feishu_bridge import __version__
    log.info("=== Feishu Bridge v%s starting (bot=%s) ===", __version__, args.bot)
    try:
        bot.start()
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    main()
