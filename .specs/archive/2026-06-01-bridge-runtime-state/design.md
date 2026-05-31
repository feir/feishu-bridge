# Design: bridge-runtime-state

## 技术方案

### 数据流

```
启动:
  config.json ──load_config()──→ agent_cfg (factory defaults)
                                    │
  runtime-state.json ──load()──→ overrides {agent_type?, provider?, model_override?}
                                    │
                              ┌─────▼──────┐
                              │  merge_cfg  │  三字段独立 fail-open validate
                              └─────┬──────┘
                                    │
                           type 或 provider 与 config 默认值不同？
                           ├─ YES → _reconcile_config(agent_cfg)
                           │        （normalize commands/args/env/providers
                           │         + resolve_command + rebuild prompt）
                           └─ NO  → 直接继续
                                    │
                              create_runner(agent_cfg, model_override=overrides.model)
                                    │
                              FeishuBot ready

运行时切换:
  /agent alma ──→ switch_agent()
                    ├─ alma preflight guard
                    ├─ next_cfg["type"] = "alma"
                    └─ _apply_config_change(next_cfg)
                         ├─ _build_config(next_cfg) → (cfg, runner) [纯函数，无副作用]
                         ├─ persist runtime-state
                         └─ _activate(cfg, runner) [替换 self.* 属性]

  /provider omlx ──→ switch_provider()
                       ├─ alma block guard
                       ├─ next_cfg["provider"] = "omlx"
                       └─ _apply_config_change(next_cfg)  [同上流程]

  /model opus ──→ set_model("opus")
                    ├─ resolve alias: "opus" → "claude-opus-4-6"
                    ├─ persist runtime-state {model_override: "claude-opus-4-6"}
                    └─ runner.model = "claude-opus-4-6"

  /model default ──→ set_model("default")
                       ├─ 识别 "default" → clear override
                       ├─ persist runtime-state（省略 model_override key）
                       └─ runner.model = resolve_agent_model(cfg, type)
```

### RuntimeState 类

```python
# 新文件：feishu_bridge/runtime_state.py（约 60 行）

@dataclass
class RuntimeState:
    agent_type: str | None = None
    provider: str | None = None
    model_override: str | None = None

    @classmethod
    def load(cls, path: Path) -> "RuntimeState":
        """Fail-open: corrupt/missing/malformed → empty state + log warning."""
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                log.warning("runtime-state at %s is not a JSON object; using defaults", path)
                return cls()
            def _str_or_none(key):
                v = data.get(key)
                if v is None:
                    return None
                if not isinstance(v, str) or not v:
                    log.warning("runtime-state: field %r has invalid value %r; ignoring", key, v)
                    return None
                return v
            return cls(
                agent_type=_str_or_none("agent_type"),
                provider=_str_or_none("provider"),
                model_override=_str_or_none("model_override"),
            )
        except FileNotFoundError:
            return cls()  # first run, no warning needed
        except (OSError, json.JSONDecodeError, TypeError):
            log.warning("runtime-state corrupt or unreadable at %s; using defaults", path, exc_info=True)
            return cls()

    def save(self, path: Path) -> None:
        """Atomic write: fsync + os.replace (aligned with SessionMap pattern).

        File format contract:
        - Key present with string value = active override
        - Key absent = no override (fallback to config default)
        - Never write null/None values; omit the key instead
        """
        data = {}
        if self.agent_type is not None:
            data["agent_type"] = self.agent_type
        if self.provider is not None:
            data["provider"] = self.provider
        if self.model_override is not None:
            data["model_override"] = self.model_override
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))

    def validate(self, runner_classes: dict, provider_profiles: dict) -> "RuntimeState":
        """Validate each field independently; invalid → None + log warning."""
        validated = RuntimeState()
        if self.agent_type:
            if self.agent_type in runner_classes:
                validated.agent_type = self.agent_type
            else:
                log.warning("runtime-state: agent_type=%r not in runner_classes; ignoring", self.agent_type)
        if self.provider:
            if self.provider in provider_profiles:
                validated.provider = self.provider
            else:
                log.warning("runtime-state: provider=%r not in config; ignoring", self.provider)
        # model_override always passthrough (CLI validates)
        validated.model_override = self.model_override
        return validated
```

### _apply_config_change() 提取

拆分为三步：`_build_config`（纯函数）→ persist → `_activate`（副作用）。

`FeishuBot` 新增 `_state_lock = threading.RLock()`，覆盖 `_apply_config_change()` 和 `set_model()` 的修改+持久化段，对齐 SessionMap 的线程安全标准。

```python
# main.py 内

def _build_config(self, next_cfg: dict, *, model_override: str | None = None) -> tuple[dict, list[str], BaseRunner, str | None, str | None]:
    """Pure function: normalize cfg + build runner. No side effects on self.

    Args:
        model_override: Caller-provided snapshot; avoids reading self._runtime_state
                        (which may change concurrently).

    Returns (next_cfg, next_prompts, next_runner, resolved_cmd, configured_cmd).
    Raises ConfigError on normalization/resolution failure.
    """
    target_type = next_cfg["type"]

    # 1. Normalize
    next_cfg["commands"] = _normalize_agent_commands(next_cfg)
    next_cfg["args_by_type"] = _normalize_agent_args(next_cfg)
    next_cfg["env_by_type"] = _normalize_agent_env(next_cfg)
    next_cfg["providers"] = _normalize_provider_profiles(next_cfg)

    # 2. Resolve command (alma has no command)
    resolved_cmd = configured_cmd = None
    if target_type == "alma":
        next_cfg["command"] = None
        next_cfg["_resolved_command"] = None
        resolved_cmd = "alma (WS)"
    else:
        resolved_cmd, configured_cmd = resolve_effective_agent_command(next_cfg, target_type)
        if not resolved_cmd:
            raise ConfigError(f"Agent 命令 `{configured_cmd}` 未在 PATH 中找到")
        next_cfg["command"] = configured_cmd
        next_cfg["_resolved_command"] = resolved_cmd

    # 3. Re-normalize prompt from raw intent
    raw_prompt = next_cfg.get("_prompt_raw") or {}
    next_cfg["prompt"] = _normalize_prompt_config(
        raw_prompt, fill_defaults=True, agent_type=target_type,
    )

    # 4. Build runner with model_override
    next_bot_cfg = dict(self.bot_config)
    next_prompts = build_extra_prompts(next_cfg)
    next_runner = create_runner(
        next_cfg, next_bot_cfg, next_prompts,
        model_override=model_override,
    )

    return next_cfg, next_prompts, next_runner, resolved_cmd, configured_cmd


def _apply_config_change(self, next_cfg: dict) -> tuple[str | None, str | None]:
    """Build → persist → activate. Thread-safe via _state_lock.

    Entire build→persist→activate sequence held under _state_lock to prevent
    lost updates (e.g. concurrent set_model being silently reverted by a
    slower switch_provider). Aligned with SessionMap's full-lock pattern.

    Returns (resolved_cmd, configured_cmd). Caller handles guards.
    Raises ConfigError on build failure.
    Save failure → log warning + return degraded success (memory updated, disk stale).
    """
    with self._state_lock:
        # 1. Build under lock (includes model_override from current state)
        next_cfg, next_prompts, next_runner, resolved_cmd, configured_cmd = \
            self._build_config(next_cfg, model_override=self._runtime_state.model_override)

        # 2. Persist first (before activation)
        next_state = RuntimeState(
            agent_type=next_cfg["type"],
            provider=resolve_provider_name(next_cfg),
            model_override=self._runtime_state.model_override,
        )
        try:
            next_state.save(self._runtime_state_path)
        except OSError:
            log.warning("runtime-state save failed; in-memory state will diverge from disk",
                        exc_info=True)

        # 3. Activate (replace self.* attributes)
        self._runtime_state = next_state
        self.agent_config = next_cfg
        self._extra_prompts = next_prompts
        self.runner = next_runner
        self.model_aliases = resolve_model_aliases(next_cfg)
        self.session_map = SessionMap(
            self._session_map_path,
            agent_type=session_identity(next_cfg),
        )
        self._session_cost.clear()

    return resolved_cmd, configured_cmd
```

### 启动时 reconcile 路径

```python
# FeishuBot.__init__ 中，load_config 之后、create_runner 之前

def _reconcile_startup_config(self, state: RuntimeState) -> None:
    """If runtime-state overrides type or provider, re-normalize agent_cfg.

    Uses the same _build_config() path as hot-switch, ensuring
    agent_cfg internal consistency (commands, args, env, prompt, _resolved_command).
    """
    cfg = self.agent_config
    next_cfg = dict(cfg)  # work on a copy; original stays clean on failure
    changed = False

    if state.agent_type and state.agent_type != cfg.get("type"):
        next_cfg["type"] = state.agent_type
        changed = True
    if state.provider and state.provider != resolve_provider_name(cfg):
        next_cfg["provider"] = state.provider
        changed = True

    if state.agent_type == "alma":
        ok, msg = AlmaRunner.preflight_check()
        if not ok:
            log.warning("runtime-state: alma preflight failed (%s); discarding agent_type override", msg)
            next_cfg["type"] = cfg["type"]
            state.agent_type = None
            changed = next_cfg.get("provider") != resolve_provider_name(cfg)

    if changed:
        log.info("Reconciling startup config from runtime-state: type=%s, provider=%s",
                 next_cfg["type"], resolve_provider_name(next_cfg))
        try:
            built_cfg, prompts, runner, _, _ = self._build_config(dict(next_cfg))
            self.agent_config = built_cfg
            self._extra_prompts = prompts
            self.runner = runner
            self.model_aliases = resolve_model_aliases(built_cfg)
            self.session_map = SessionMap(
                self._session_map_path,
                agent_type=session_identity(built_cfg),
            )
        except ConfigError:
            log.warning(
                "runtime-state reconcile failed (type=%s, provider=%s); "
                "discarding overrides, booting on config defaults",
                state.agent_type, state.provider, exc_info=True,
            )
            # Discard the stale overrides from runtime-state
            state.agent_type = None
            state.provider = None
            try:
                state.save(self._runtime_state_path)
            except OSError:
                pass
    # model_override handled separately via create_runner(model_override=...)
```

### _effective_default_model() 共享 resolver

```python
# main.py — 模块级函数，create_runner 和 set_model 共用

def _effective_default_model(agent_cfg, bot_cfg):
    """Resolve the effective model when no override is active.

    Fallback chain: provider profile model → bot-level model → None (CLI default).
    """
    return resolve_agent_model(agent_cfg, agent_cfg["type"]) or bot_cfg.get("model")
```

### create_runner 改造

```python
# main.py:581 — 新增 model_override 参数

def create_runner(agent_cfg, bot_cfg, extra_prompts, *, model_override=None):
    agent_type = agent_cfg["type"]
    runner_cls = _RUNNER_CLASSES[agent_type]
    model = model_override or _effective_default_model(agent_cfg, bot_cfg)
    # ... 其余不变
```

### set_model() 方法

接收 raw user input，负责 alias 解析 + `"default"` 特殊处理 + 持久化全名。
`commands.py` 的 `/model` handler：无参 → `bot.get_model_status()`（status query）；有参 → `bot.set_model(arg)`。

```python
# main.py FeishuBot 上新增

def get_model_status(self) -> tuple[str, bool]:
    """Return current effective model and whether an override is active."""
    override = self._runtime_state.model_override
    if override:
        return override, True
    effective = _effective_default_model(self.agent_config, self.bot_config)
    return effective or "(CLI 默认)", False

def set_model(self, raw_input: str) -> tuple[str, bool]:
    """Set or clear model override. Thread-safe via _state_lock.

    Args:
        raw_input: alias/full name/"default" → clear override.
                   None not accepted (use get_model_status for query).

    Returns:
        (effective_model_display, is_cleared)
    """
    # 1. Resolve alias → full name; "default" → clear
    if raw_input.strip().lower() == "default":
        resolved = None
    elif raw_input in self.model_aliases:
        resolved = self.model_aliases[raw_input]
    elif raw_input in self.model_aliases.values():
        resolved = raw_input
    else:
        resolved = raw_input  # passthrough unknown name

    with self._state_lock:
        if resolved is None:
            # Clear override → effective default (same resolver as create_runner)
            effective = _effective_default_model(self.agent_config, self.bot_config)
            self.runner.model = effective
            self._runtime_state.model_override = None
        else:
            self.runner.model = resolved
            self._runtime_state.model_override = resolved
            effective = resolved

        try:
            self._runtime_state.save(self._runtime_state_path)
        except OSError:
            log.warning("runtime-state save failed after model change", exc_info=True)

    return effective or "(CLI 默认)", resolved is None
```

## 关键决策

### 1. Fail-open 启动策略
**决策**: 三字段独立校验，任一字段无效 → 该字段 fallback 到 config.json 默认
**理由**: 避免状态文件损坏阻塞 bridge 启动；config.json 始终是 safe fallback
**实现**: `RuntimeState.validate()` 按字段过滤，不是全有全无

### 2. model_override 跨切换保留
**决策**: agent/provider 切换时保留 model_override（除非用户 `/model default`）
**理由**: Captain 确认 — 用户切了 model 就是想用那个 model，切 agent/provider 是换后端配置，model 偏好应该独立
**实现**: `_apply_config_change()` 从 `self._runtime_state.model_override` 传入 create_runner

### 3. Guard 逻辑不进 helper
**决策**: Alma preflight、local 移除提示、alma 禁止切 provider 的 guard 留在各自的 switch 方法
**理由**: Codex 评审要求 — helper 只做 normalize→build→activate→persist，不隐藏 validation policy
**实现**: switch_agent / switch_provider 先执行 guard，再调 _apply_config_change

### 4. 线程安全：_state_lock 全程持锁
**决策**: `FeishuBot` 新增 `_state_lock = threading.RLock()`，`_apply_config_change()` 和 `set_model()` 的 build→persist→activate 全程在锁内完成
**理由**: 现有 `SessionMap` 已使用 RLock（`runtime.py:180`）全程持锁。snapshot+release+reacquire 模式无法防止 lost update（并发 set_model 被 stale switch 覆盖）
**实现**: `_apply_config_change()` 整个 build+persist+activate 在 `with self._state_lock:` 内；`_build_config()` 虽然是纯函数，但在锁内调用以保证线性化。build 耗时可接受（本地 normalize + resolve，无 I/O）

### 5. Save 失败策略：log + 降级成功
**决策**: save 失败时 catch OSError → log warning，内存状态仍更新（当前 session 生效），磁盘状态 stale（下次重启回到旧值）
**理由**: 另一个选择是"先 save 再 activate"（staged commit），但 save 失败的实际概率极低（本地磁盘写小 JSON），且 save 失败后"不让切换"的体验更差。降级成功 + 明确 log 是最务实的选择
**回滚路径**: 删除 `runtime-state-{bot_id}.json` 后重启，即可恢复到 config.json 默认值

### 6. Reconcile build failure = discard overrides
**决策**: `_reconcile_startup_config()` 中 `_build_config()` 抛 ConfigError 时，discard type/provider overrides + log warning + 继续用 config defaults 启动
**理由**: 单字段 valid 但组合后 invalid 的场景（如 runtime-state 指向已存在但 command 被卸载的 agent type），应与 validate() 的 fail-open 策略一致——启动优先级高于状态恢复
**实现**: try/except ConfigError 包裹 _build_config()；catch 后 state.agent_type = state.provider = None + save 清理磁盘状态

### 7. Effective default model 共享 resolver
**决策**: 抽 `_effective_default_model(agent_cfg, bot_cfg)` 模块级函数，`create_runner()` 和 `set_model("default")` 共用
**理由**: Codex Round 2 发现 set_model 清除 override 时只用 `resolve_agent_model()`，而 create_runner 的 fallback 链多一步 `bot_cfg.get("model")`，两处不一致会导致 `/model default` 和重启后行为不同
**实现**: 单一函数封装完整 fallback 链：provider profile model → bot-level model → None (CLI default)

### 8. /model 无参 = read-only query，有参 = mutation
**决策**: 拆为 `get_model_status()`（read-only）和 `set_model(raw_input: str)`（mutation）；commands.py 保留无参分支调 get_model_status
**理由**: Codex Round 3 指出 set_model(None) = clear override 与 /model 无参 = status query 语义冲突
**实现**: 只有显式 `"default"` 清除 override；None 不作为 set_model 输入

## 影响范围

### 新增文件
```
feishu_bridge/runtime_state.py          # RuntimeState dataclass（~60 行）
tests/unit/test_runtime_state.py        # RuntimeState 单元测试（~80 行）
```

### 修改文件
```
feishu_bridge/main.py                   # _apply_config_change 提取 + create_runner 签名 + set_model + __init__ 加载
feishu_bridge/commands.py               # /model handler 改用 bot.set_model()
```

### 不修改
```
feishu_bridge/runtime.py                # SessionMap 不变
feishu_bridge/runtime_alma.py           # AlmaRunner 不变
config.json                             # 不动
```
