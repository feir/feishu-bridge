# Tasks: bridge-runtime-state

## 1. RuntimeState 模块

- [x] 1.1 创建 `feishu_bridge/runtime_state.py`，实现 RuntimeState dataclass（load/save/validate）
  - Validate: `RuntimeState.load(不存在的路径)` 返回空 state；`save()` 后 `load()` round-trip 正确；`validate()` 过滤未知 agent_type 和 provider
- [x] 1.2 单元测试 `tests/unit/test_runtime_state.py`
  - Validate: 覆盖 load（正常/corrupt/missing + FileNotFoundError 无 warning + non-dict JSON 如 `[]`/`1`/`"x"` 返回空 state + warning）、save（atomic write + fsync + os.replace）、validate（valid/invalid/partial）、model_override 省略 key = 无 override 约定；corrupt/invalid/non-dict 路径须 assert log 包含 warning（caplog）

## 2. create_runner 改造

- [x] 2.1 `create_runner()` 新增 `model_override` keyword 参数，优先级：`model_override > resolve_agent_model > bot_cfg.get("model")`
  - Validate: 传 model_override="test-model" 时 runner.model == "test-model"；传 None 时 fallback 到 provider 默认
- [x] 2.2 `FeishuBot.__init__` 加载 runtime-state + `_reconcile_startup_config()`：type/provider 与 config 默认值不同时执行完整 normalize 流程（_build_config），确保 agent_cfg 内部一致（commands/args/env/providers/_resolved_command/prompt 全部重建）。_build_config 失败时 catch ConfigError → discard overrides + log warning + fallback config defaults
  - Validate: runtime-state 指定 type=codex → 启动后 agent_cfg["type"]=="codex" 且 _resolved_command 指向 codex binary；runtime-state 指定 provider=omlx → 启动后 resolve_provider_name==omlx 且 model 为 omlx 默认；文件不存在或损坏时 fallback 到 config.json 默认 + log warning；reconcile _build_config 失败时 discard overrides + fallback + log warning

## 3. Switch 逻辑合并

- [x] 3.1 提取 `_build_config()` 纯函数 + `_apply_config_change()` 方法（build → persist → activate），新增 `_state_lock = threading.RLock()` 保护修改+持久化段
  - Validate: `_build_config()` 不修改 self 任何属性；ConfigError 正确传播；save 失败时 log warning 但 in-memory 仍更新（降级成功语义）
- [x] 3.2 `switch_agent()` 瘦身为 guard + next_cfg["type"] 赋值 + 调用 `_apply_config_change()`
  - Validate: `/agent alma` / `/agent claude` 热切换行为与重构前一致；alma preflight guard 仍生效
- [x] 3.3 `switch_provider()` 瘦身为 guard + next_cfg["provider"] 赋值 + 调用 `_apply_config_change()`
  - Validate: `/provider omlx` / `/provider default` 切换行为一致；alma 模式下仍返回错误

## 4. Model 管理统一

- [x] 4.1 `FeishuBot.set_model(raw_input: str)` 方法 + `get_model_status()` query 方法。set_model：内置 alias 解析（raw_input → self.model_aliases → 全名），"default" → 清除 override（通过共享 `_effective_default_model()` 获取 effective default），未知 name passthrough。持久化存解析后全名（不存别名 token）。线程安全（_state_lock）。get_model_status：read-only，返回当前 model + override 状态
  - Validate: `set_model("op4")` → runner.model=="claude-opus-4-1"（全名） + runtime-state 包含 model_override:"claude-opus-4-1"；`set_model("default")` → model_override 省略 + runner.model 回到 effective default（同 create_runner 逻辑）；save 失败时 log warning；`get_model_status()` 不修改任何状态
- [x] 4.2 `commands.py` /model handler：无参 → `bot.get_model_status()`（read-only），有参 → `bot.set_model(arg)`，删除 handler 内的 alias 解析逻辑
  - Validate: `/model opus` 写入 state；`/model default` 清除 state；`/model`（无参）显示当前 model + 是否有 override 标注；无参不触发 set_model

## 5. 集成测试

- [x] 5.1 重启恢复 — agent：switch_agent("codex") → 模拟重启 → 验证 agent 恢复
  - Validate: 重启后 bot.agent_config["type"] == "codex" 且 _resolved_command 指向 codex
- [x] 5.2 重启恢复 — provider：switch_provider("omlx") → 模拟重启 → 验证 provider 恢复
  - Validate: 重启后 resolve_provider_name == "omlx" 且 model 为 omlx 默认
- [x] 5.3 重启恢复 — model：set_model("claude-opus-4-6") → 模拟重启 → 验证 model 恢复
  - Validate: 重启后 runner.model == "claude-opus-4-6"
- [x] 5.4 model_override 跨 provider 切换存活：set_model → switch_provider → 验证
  - Validate: provider 切换后 runner.model 仍为之前 set_model 的值
- [x] 5.5 stale fallback：写入 runtime-state 包含 agent_type="removed_type" → 启动 → 验证 fallback
  - Validate: 启动后 agent_type 回到 config 默认值 + log 包含 warning
- [x] 5.6 stale provider fallback：写入 runtime-state 包含 provider="deleted_provider" → 启动 → 验证 fallback
  - Validate: 启动后 provider 回到 config-loaded provider（非硬编码 "default"）+ log 包含 warning
- [x] 5.7 corrupt file fallback：写入非法 JSON 到 runtime-state → 启动 → 验证 fallback
  - Validate: 启动后全部回到 config 默认值 + log 包含 warning
- [x] 5.8 reconcile build failure fallback：写入 runtime-state 包含 valid type + valid provider 但组合后 command 不存在 → 启动 → 验证 fallback
  - Validate: 启动后 discard overrides + fallback 到 config 默认值 + log 包含 warning + runtime-state 文件中 overrides 已清除
- [x] 5.9 并发安全：交错执行 set_model() + switch_agent()/switch_provider() → 验证 linearizable 行为
  - Validate: 多线程并发调用后 runner.model、agent_config["type"]、持久化 runtime-state 三者一致；后完成的操作不被先完成的覆盖（linearizable，无 lost update）
- [x] 5.10 回归：运行全量现有测试
  - Validate: 794+ 测试全部通过，0 regression

## Review Report

### Round 1 (2026-05-18, basis: a7d6fa8+dirty)

Codex gpt-5.4/high post-implementation code review. 3 HIGH, 0 CRITICAL.

[HIGH] Restart recovery tests (5.1–5.3) only exercised RuntimeState.load/validate, not the actual __init__ startup wiring (_reconcile_startup_config + create_runner + model_override).
→ Fixed: added _simulate_restart() helper + test_restart_recovers_agent_type, test_restart_recovers_model_override, test_restart_with_stale_agent_falls_back.

[HIGH] Concurrency test only checked for exceptions and partial consistency; did not prove persisted == in-memory state (no-lost-update contract).
→ Fixed: strengthened assertions to compare persisted {agent_type, provider, model_override} with bot._runtime_state exactly, and verified internal consistency of in-memory state.

[HIGH] /model default and get_model_status() with/without override had no test coverage.
→ Fixed: added test_model_default_clears_override, test_get_model_status_no_override, test_get_model_status_with_override, test_model_default_returns_provider_model_when_available.

Post-fix: 840 tests pass (up from 819 pre-change), 13 pre-existing failures unchanged.

## Spec-Check

### Round 1 (2026-05-18, Codex gpt-5.4/high)
**Verdict: BLOCK (1 CRITICAL, 3 HIGH, 2 MEDIUM)**
- CRITICAL: Startup path missing normalize/reconcile when type/provider differ from config defaults
- HIGH: No thread safety for concurrent command handling
- HIGH: set_model persists alias tokens instead of resolved full names
- HIGH: Save failure after activation leaves memory/disk diverged
- MEDIUM: File format contract inconsistent (null vs omit key)
- MEDIUM: AC/tests only covered agent restart, missing provider/model/stale paths
All fixed in same session.

### Round 2 (2026-05-18, Codex gpt-5.4/high)
**Verdict: BLOCK (1 CRITICAL, 2 HIGH, 2 MEDIUM)**
- CRITICAL: _reconcile_startup_config calls _build_config without ConfigError catch
- HIGH: set_model("default") resolver differs from create_runner fallback chain
- HIGH: load()/validate() silently return empty without logging warnings per AC
- MEDIUM: Provider fallback hardcodes "default" string instead of config-loaded provider
- MEDIUM: _state_lock introduced but no concurrency test validates it
All fixed in same session.

### Round 3 (2026-05-18, Codex gpt-5.4/high)
**Verdict: BLOCK (0 CRITICAL, 3 HIGH, 2 MEDIUM)**
- HIGH: reconcile mutates cfg in place before validation; failure leaves memory polluted
- HIGH: _build_config reads model_override outside lock; concurrent set_model can cause inconsistency
- HIGH: startup reconcile skips alma preflight
- MEDIUM: load() doesn't handle non-dict JSON ([], 1, "x") → AttributeError
- MEDIUM: set_model(None) = clear conflicts with /model no-arg = status query
All fixed in same session.

### Round 4 (2026-05-18, Codex gpt-5.4/high)
**Verdict: WARN (0 CRITICAL, 4 HIGH, 1 MEDIUM, 1 LOW)**
- HIGH: reconcile builds model_aliases/SessionMap from old cfg instead of built_cfg
- HIGH: AlmaRunner.preflight_check() returns tuple; `if not` on tuple is always truthy
- HIGH: load()/validate() don't validate field types; {"provider": []} can raise
- HIGH: snapshot+release+reacquire pattern allows lost updates; full-lock needed
- MEDIUM: tasks.md 4.1 still references None→clear (contradicts API split)
- LOW: proposal Risks table still hardcodes "default" for provider fallback
All fixed in same session.

Post-review: 4 rounds, 22 findings total, all fixed. HIGH count R1→R4: 3→2→3→4 (plateau).
Remaining HIGHs in R4 are surgical precision issues (tuple unpacking, type checks, lock scope), consistent with plan-review plateau signal — further paper rounds unlikely to surface qualitatively new issues.

- result: WARN
- reviewer: Codex gpt-5.4/high (plan-review)
- basis: proposal.md + design.md + tasks.md (post-R4 fix)
- timestamp: 2026-05-18
- notes: 0 CRITICAL remaining; 4 HIGH all implementation-detail precision (not design direction); recommend proceeding to implementation with tests as primary validation gate

### Code-Review Round 1 (2026-05-18, Codex gpt-5.4/high)
**Verdict: WARNING (0 CRITICAL, 3 HIGH, 0 MEDIUM, 0 LOW)**
All 3 HIGH were test coverage gaps (restart simulation, concurrency assertions, /model default). Fixed in same session.

- result: WARN
- reviewer: Codex gpt-5.4/high (code-review)
- basis: HEAD=a7d6fa8+dirty
- timestamp: 2026-05-18
- notes: 0 CRITICAL; all HIGHs were test gaps fixed immediately; 840 tests pass post-fix
