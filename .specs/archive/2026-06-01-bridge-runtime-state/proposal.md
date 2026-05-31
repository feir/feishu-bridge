---
branch: master
start-sha: a7d6fa88ff582607d7790da7d867827010f6b71f
status: active
---

# Proposal: bridge-runtime-state

## WHY

**目标用户**：Bridge 运维者（Captain），通过飞书使用 bridge 的用户

**核心痛点**：
- `/agent alma` 后重启 bridge，agent 回到 config.json 默认值 `claude`，用户需反复切换
- `/model claude-opus-4-6` 在任何触发 `create_runner()` 的操作后丢失
- `switch_agent()` 和 `switch_provider()` 有 ~80% 代码重复，新增切换维度需复制粘贴

**价值**：Agent/provider/model 切换一次即持久生效，bridge 重启后自动恢复上次配置；代码结构更易维护。

## WHAT

- 新增 runtime-state 持久化文件（`{workspace}/state/feishu-bridge/runtime-state-{bot_id}.json`），存储 `{agent_type, provider, model_override}`
- 启动时 merge runtime-state 到 config.json 加载的默认配置（三字段独立 fail-open）
- 合并 `switch_agent()` / `switch_provider()` 的公共逻辑为 `_apply_config_change(next_cfg)`
- `create_runner()` 新增 `model_override` 参数，优先于 provider 默认 model
- `/model` handler 改为调用 `bot.set_model()`，写入 runtime-state
- 新增 `/model default` 清除 model override，回到 provider 默认

## NOT

- ❌ 不修改 config.json（保持为 factory defaults，含 `${VAR}` 占位符）
- ❌ 不引入跨进程文件锁（接受单 bot_id 单进程的 single-writer 语义）
- ❌ 不修改 SessionMap、ledger、bg_supervisor 等无关持久化组件
- ❌ 不改变 `/new` 行为（runtime-state 不受 /new 影响）
- ❌ 不在 bridge 层校验 model override 的 provider 兼容性（保持 passthrough 语义）

## Acceptance Criteria

- [ ] `/agent alma` → 重启 bridge → agent 仍为 alma
- [ ] `/provider omlx` → 重启 bridge → provider 仍为 omlx
- [ ] `/model X` → 重启 bridge → runner.model 仍为 X
- [ ] `/model X` → `/provider Y` → 切回 → model override 保留
- [ ] `/model default` 清除 override，runner.model 回到 effective default model（provider profile → bot-level fallback）
- [ ] runtime-state 文件损坏时 bridge 正常启动，fallback 到 config 默认值 + log warning
- [ ] runtime-state 包含已移除 agent type 时 bridge 正常启动，忽略该字段 + log warning
- [ ] runtime-state 包含已删除 provider 时 bridge 正常启动，fallback 到 config-loaded provider
- [ ] runtime-state 指向有效 type+provider 但组合后 build 失败时 bridge 正常启动，discard overrides + log warning
- [ ] 现有单元测试全部通过，/new 行为不变

## Approaches Considered

### Approach A: 三字段 runtime-state（推荐）
**Summary**: 独立 JSON 文件存 `{agent_type, provider, model_override}`，启动时三字段独立 fallback
**Effort**: M  **Risk**: Low
**Pros**: 字段语义清晰，fail-open 粒度好，与 SessionMap 模式一致，易扩展
**Cons**: 三字段需分别校验，create_runner 签名增加参数

### Approach B: 完整 agent_config 快照
**Summary**: 序列化整个 agent_config dict 到 runtime-state
**Effort**: S  **Risk**: High
**Pros**: 启动逻辑简单
**Cons**: config.json 修改不会被感知，字段污染严重，升级路径脆弱

### Approach C: 写回 config.json
**Summary**: 直接更新 config.json 中的 agent.type / agent.provider 字段
**Effort**: M  **Risk**: Med
**Pros**: 单一 state 源
**Cons**: 破坏 ${VAR} env substitution，部署配置与运行期 state 混淆

**Selected: A** — 分离 deploy-time config 与 runtime overrides，fail-open 友好，扩展性好。Codex 评审明确建议不动 config.json。

## RISKS

| 风险 | 影响 | 概率 | 缓解方案 |
|------|------|------|----------|
| 状态文件 JSON 损坏 | bridge 启动异常 | Low | fail-open：corrupt → fallback config 默认 + log warning |
| Agent type 被代码升级移除 | 状态指向不存在的 type | Low | 启动校验 `type in _RUNNER_CLASSES`，不在则忽略 |
| Provider 在 config 中被删除 | 状态指向不存在的 provider | Low | validate() 忽略无效 override，保持 config-loaded provider |
| Model override 在新 provider 下无效 | CLI 拒绝该 model | Low | 保持 passthrough（CLI 侧报错），不在 bridge 层 hard-fail |
| Save 失败导致磁盘 stale | 重启后回到旧状态 | Low | 降级成功（in-memory 生效 + log warning）；operator 回滚：删除 runtime-state 文件重启即可 |
| 合并 switch 逻辑引入回归 | agent/provider 切换失败 | Med | 现有 794 个测试 + 新增 runtime-state 测试 |

## Decision Log

| Date | Decision | Reason |
|------|----------|--------|
| 2026-05-18 | 选择独立 runtime-state 文件（方案 A） | Codex 评审建议不动 config.json；三字段 fail-open 粒度好 |
| 2026-05-18 | model_override 跨 agent/provider 切换保留 | Captain 确认 premise #2；用户清除需显式 `/model default` |
| 2026-05-18 | 接受 single-writer 语义 | 当前部署是单 bot_id 单进程；多实例场景留待未来补锁 |
