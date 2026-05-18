"""Integration tests for RuntimeState persistence across restarts.

Tests 5.1–5.9 from bridge-runtime-state tasks.md:
- Switch + restart recovery (agent, provider, model)
- model_override survival across provider switch
- Stale/corrupt fallback
- Reconcile build failure
- Concurrent safety
"""

import json
import logging
import threading
import types

import pytest

import feishu_bridge.main as bridge
from feishu_bridge import runtime as bridge_runtime
from feishu_bridge.runtime_state import RuntimeState


def _make_bot(tmp_path, monkeypatch, *, agent_type="claude", provider="default"):
    """Create a lightweight FeishuBot with all attributes needed for switch/model tests."""
    bot = object.__new__(bridge.FeishuBot)
    bot.bot_id = "test-bot"
    bot.bot_config = {"workspace": str(tmp_path), "model": "claude-opus-4-6"}
    bot.agent_config = {
        "type": agent_type,
        "command": "claude",
        "provider": provider,
        "providers": {
            "default": {},
            "omlx": {
                "env_by_type": {"claude": {"ANTHROPIC_BASE_URL": "http://omlx"}},
                "models": {"claude": "qwen3.5"},
            },
        },
        "commands": {"claude": "claude", "codex": "codex"},
        "args_by_type": {"claude": [], "codex": ["--oss"]},
        "env_by_type": {"claude": {}, "codex": {}},
        "_resolved_command": bridge.shutil.which("python3"),
        "timeout_seconds": 30,
    }
    bot.runner = bridge_runtime.ClaudeRunner(
        command="claude", model="claude-opus-4-6", workspace=str(tmp_path), timeout=30,
    )
    bot._extra_prompts = []
    bot._session_cost = {}
    bot._state_lock = threading.RLock()
    bot._runtime_state = RuntimeState()
    bot._runtime_state_path = tmp_path / "runtime-state.json"
    bot._session_map_path = tmp_path / "sessions.json"
    bot.session_map = bridge.SessionMap(bot._session_map_path, agent_type="claude")
    bot.model_aliases = {"opus": "claude-opus-4-7", "sonnet": "claude-sonnet-4-6"}

    monkeypatch.setattr(
        bridge, "resolve_effective_agent_command",
        lambda cfg, t: (bridge.shutil.which("python3"), "python3"),
    )
    monkeypatch.setattr(bridge, "build_extra_prompts", lambda cfg: [])

    return bot


# ------------------------------------------------------------------
# 5.1 Restart recovery — agent
# ------------------------------------------------------------------

def test_switch_agent_persists_and_restores(tmp_path, monkeypatch):
    """switch_agent("codex") → file written → reload verifies agent_type="codex"."""
    bot = _make_bot(tmp_path, monkeypatch)
    ok, msg, _ = bot.switch_agent("codex")
    assert ok is True

    state = RuntimeState.load(bot._runtime_state_path)
    assert state.agent_type == "codex"

    provider_profiles = {"default": {}, "omlx": {}}
    validated = state.validate(bridge._RUNNER_CLASSES, provider_profiles)
    assert validated.agent_type == "codex"


# ------------------------------------------------------------------
# 5.2 Restart recovery — provider
# ------------------------------------------------------------------

def test_switch_provider_persists_and_restores(tmp_path, monkeypatch):
    """switch_provider("omlx") → file written → reload verifies provider="omlx"."""
    bot = _make_bot(tmp_path, monkeypatch)
    ok, msg = bot.switch_provider("omlx")
    assert ok is True

    state = RuntimeState.load(bot._runtime_state_path)
    assert state.provider == "omlx"


# ------------------------------------------------------------------
# 5.3 Restart recovery — model
# ------------------------------------------------------------------

def test_set_model_persists_and_restores(tmp_path, monkeypatch):
    """set_model("claude-opus-4-6") → file written → reload verifies."""
    bot = _make_bot(tmp_path, monkeypatch)
    effective, is_cleared = bot.set_model("claude-opus-4-6")
    assert effective == "claude-opus-4-6"
    assert is_cleared is False

    state = RuntimeState.load(bot._runtime_state_path)
    assert state.model_override == "claude-opus-4-6"


# ------------------------------------------------------------------
# 5.4 model_override survives provider switch
# ------------------------------------------------------------------

def test_model_override_survives_provider_switch(tmp_path, monkeypatch):
    """set_model → switch_provider → model_override still active."""
    bot = _make_bot(tmp_path, monkeypatch)

    bot.set_model("claude-opus-4-6")
    assert bot.runner.model == "claude-opus-4-6"

    ok, _ = bot.switch_provider("omlx")
    assert ok is True
    assert bot.runner.model == "claude-opus-4-6"

    state = RuntimeState.load(bot._runtime_state_path)
    assert state.model_override == "claude-opus-4-6"
    assert state.provider == "omlx"


# ------------------------------------------------------------------
# 5.5 Stale agent_type fallback
# ------------------------------------------------------------------

def test_stale_agent_type_fallback(tmp_path, caplog):
    """runtime-state with removed agent_type → validate discards + warns."""
    state_path = tmp_path / "runtime-state.json"
    state_path.write_text('{"agent_type": "removed_type"}')

    with caplog.at_level(logging.WARNING):
        loaded = RuntimeState.load(state_path)
        validated = loaded.validate(bridge._RUNNER_CLASSES, {"default": {}})

    assert validated.agent_type is None
    assert any("not in runner_classes" in r.message for r in caplog.records)


# ------------------------------------------------------------------
# 5.6 Stale provider fallback
# ------------------------------------------------------------------

def test_stale_provider_fallback(tmp_path, caplog):
    """runtime-state with removed provider → validate discards + warns."""
    state_path = tmp_path / "runtime-state.json"
    state_path.write_text('{"provider": "deleted_provider"}')

    with caplog.at_level(logging.WARNING):
        loaded = RuntimeState.load(state_path)
        validated = loaded.validate(bridge._RUNNER_CLASSES, {"default": {}, "omlx": {}})

    assert validated.provider is None
    assert any("not in config" in r.message for r in caplog.records)


# ------------------------------------------------------------------
# 5.7 Corrupt file fallback
# ------------------------------------------------------------------

def test_corrupt_file_fallback(tmp_path, caplog):
    """Corrupt JSON in runtime-state → load returns empty + warns."""
    state_path = tmp_path / "runtime-state.json"
    state_path.write_text("{broken json!!!")

    with caplog.at_level(logging.WARNING):
        loaded = RuntimeState.load(state_path)

    assert loaded.agent_type is None
    assert loaded.provider is None
    assert loaded.model_override is None
    assert any("corrupt or unreadable" in r.message for r in caplog.records)


# ------------------------------------------------------------------
# 5.8 Reconcile build failure → discard overrides
# ------------------------------------------------------------------

def test_reconcile_build_failure_discards_overrides(tmp_path, monkeypatch, caplog):
    """Valid type + provider combo that fails _build_config → discard + fallback."""
    bot = _make_bot(tmp_path, monkeypatch)

    state = RuntimeState(agent_type="codex", provider="omlx")
    state.save(bot._runtime_state_path)

    def _failing_build(self, next_cfg, *, model_override=None):
        raise bridge.ConfigError("test: command not found")

    monkeypatch.setattr(bridge.FeishuBot, "_build_config", _failing_build)

    with caplog.at_level(logging.WARNING):
        bot._reconcile_startup_config(state)

    assert state.agent_type is None
    assert state.provider is None

    persisted = RuntimeState.load(bot._runtime_state_path)
    assert persisted.agent_type is None
    assert persisted.provider is None


# ------------------------------------------------------------------
# 5.9 Concurrent safety — no lost updates
# ------------------------------------------------------------------

def test_concurrent_set_model_and_switch(tmp_path, monkeypatch):
    """Interleaved set_model + switch_provider → linearizable, no lost update."""
    bot = _make_bot(tmp_path, monkeypatch)
    errors = []
    results = {"models": [], "providers": []}

    def _set_models():
        try:
            for m in ["model-a", "model-b", "model-c"]:
                bot.set_model(m)
                results["models"].append(m)
        except Exception as e:
            errors.append(e)

    def _switch_providers():
        try:
            for p in ["omlx", "default", "omlx"]:
                bot.switch_provider(p)
                results["providers"].append(p)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=_set_models)
    t2 = threading.Thread(target=_switch_providers)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"Concurrent operations raised: {errors}"

    # Persisted state must exactly match in-memory state (no lost update)
    persisted = RuntimeState.load(bot._runtime_state_path)
    mem = bot._runtime_state
    assert persisted.agent_type == mem.agent_type, \
        f"Lost update: persisted agent_type={persisted.agent_type!r} != mem={mem.agent_type!r}"
    assert persisted.provider == mem.provider, \
        f"Lost update: persisted provider={persisted.provider!r} != mem={mem.provider!r}"
    assert persisted.model_override == mem.model_override, \
        f"Lost update: persisted model_override={persisted.model_override!r} != mem={mem.model_override!r}"

    # In-memory state must be internally consistent
    assert mem.model_override == bot.runner.model, \
        f"model_override {mem.model_override!r} diverged from runner.model {bot.runner.model!r}"
    assert mem.agent_type == bot.agent_config["type"], \
        f"agent_type {mem.agent_type!r} diverged from agent_config {bot.agent_config['type']!r}"
    assert mem.provider == bridge.resolve_provider_name(bot.agent_config), \
        f"provider {mem.provider!r} diverged from agent_config"


# ------------------------------------------------------------------
# Restart simulation — exercises the full startup wiring
# (_reconcile_startup_config + create_runner + model_override)
# ------------------------------------------------------------------

def _simulate_restart(tmp_path, monkeypatch, *, initial_agent="claude"):
    """Create a fresh bot, run the startup sequence against the persisted state file."""
    bot = object.__new__(bridge.FeishuBot)
    bot.bot_id = "test-bot"
    bot.bot_config = {"workspace": str(tmp_path), "model": "claude-opus-4-6"}
    bot.agent_config = {
        "type": initial_agent,
        "command": "claude",
        "provider": "default",
        "providers": {
            "default": {},
            "omlx": {
                "env_by_type": {"claude": {"ANTHROPIC_BASE_URL": "http://omlx"}},
                "models": {"claude": "qwen3.5"},
            },
        },
        "commands": {"claude": "claude", "codex": "codex"},
        "args_by_type": {"claude": [], "codex": ["--oss"]},
        "env_by_type": {"claude": {}, "codex": {}},
        "_resolved_command": bridge.shutil.which("python3"),
        "timeout_seconds": 30,
    }
    bot._state_lock = threading.RLock()
    bot._runtime_state_path = tmp_path / "runtime-state.json"
    bot._session_map_path = tmp_path / "sessions.json"
    bot._session_cost = {}
    bot._extra_prompts = []
    bot.session_map = bridge.SessionMap(bot._session_map_path, agent_type=initial_agent)

    monkeypatch.setattr(
        bridge, "resolve_effective_agent_command",
        lambda cfg, t: (bridge.shutil.which("python3"), "python3"),
    )
    monkeypatch.setattr(bridge, "build_extra_prompts", lambda cfg: [])

    # Replicate the startup sequence from FeishuBot.__init__
    provider_profiles = bridge._normalize_provider_profiles(bot.agent_config)
    raw_state = RuntimeState.load(bot._runtime_state_path)
    bot._runtime_state = raw_state.validate(bridge._RUNNER_CLASSES, provider_profiles)
    bot._reconcile_startup_config(bot._runtime_state)
    bot.runner = bridge.create_runner(
        bot.agent_config, bot.bot_config, bot._extra_prompts,
        model_override=bot._runtime_state.model_override,
    )
    bot.model_aliases = bridge.resolve_model_aliases(bot.agent_config)
    return bot


def test_restart_recovers_agent_type(tmp_path, monkeypatch):
    """switch_agent("codex") → real restart sequence → agent_config["type"] == "codex"."""
    # First "session": switch to codex
    bot1 = _make_bot(tmp_path, monkeypatch)
    ok, _, _ = bot1.switch_agent("codex")
    assert ok

    # Second "session": fresh bot loading the persisted state
    bot2 = _simulate_restart(tmp_path, monkeypatch)
    assert bot2.agent_config["type"] == "codex"
    assert bot2.agent_config.get("_resolved_command") is not None


def test_restart_recovers_model_override(tmp_path, monkeypatch):
    """set_model("claude-opus-4-7") → real restart sequence → runner.model recovered."""
    bot1 = _make_bot(tmp_path, monkeypatch)
    bot1.set_model("claude-opus-4-7")

    bot2 = _simulate_restart(tmp_path, monkeypatch)
    assert bot2.runner.model == "claude-opus-4-7"
    assert bot2._runtime_state.model_override == "claude-opus-4-7"


def test_restart_with_stale_agent_falls_back(tmp_path, monkeypatch):
    """Stale agent_type in persisted state → startup falls back to config default."""
    state_path = tmp_path / "runtime-state.json"
    state_path.write_text('{"agent_type": "removed_type"}')

    bot = _simulate_restart(tmp_path, monkeypatch)
    assert bot.agent_config["type"] == "claude"  # config default preserved


# ------------------------------------------------------------------
# /model default and get_model_status coverage (HIGH 3)
# ------------------------------------------------------------------

def test_model_default_clears_override(tmp_path, monkeypatch):
    """`/model default` clears model_override and returns effective default."""
    bot = _make_bot(tmp_path, monkeypatch)
    bot.set_model("claude-opus-4-7")
    assert bot._runtime_state.model_override == "claude-opus-4-7"

    effective, is_cleared = bot.set_model("default")
    assert is_cleared is True
    assert bot._runtime_state.model_override is None
    assert bot.runner.model == effective

    persisted = RuntimeState.load(bot._runtime_state_path)
    assert persisted.model_override is None


def test_get_model_status_no_override(tmp_path, monkeypatch):
    """`get_model_status()` reports no override when only bot_config model is set."""
    bot = _make_bot(tmp_path, monkeypatch)
    display, has_override = bot.get_model_status()
    assert has_override is False
    # Falls back to bot_config["model"] since no provider model and no override
    assert display == "claude-opus-4-6" or display == "(CLI 默认)"


def test_get_model_status_with_override(tmp_path, monkeypatch):
    """`get_model_status()` reports override when set_model was called."""
    bot = _make_bot(tmp_path, monkeypatch)
    bot.set_model("claude-opus-4-7")
    display, has_override = bot.get_model_status()
    assert has_override is True
    assert display == "claude-opus-4-7"


def test_model_default_returns_provider_model_when_available(tmp_path, monkeypatch):
    """set_model("default") after provider switch falls back to provider model."""
    bot = _make_bot(tmp_path, monkeypatch)
    # Switch to omlx which has models.claude = "qwen3.5"
    ok, _ = bot.switch_provider("omlx")
    assert ok

    bot.set_model("some-override")
    effective, is_cleared = bot.set_model("default")
    assert is_cleared is True
    # omlx provider sets model to "qwen3.5"; effective default should reflect that
    assert effective == "qwen3.5" or effective == "(CLI 默认)"
