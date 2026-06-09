---
branch: master
start-sha: b2afb9f1780b265379a7033048712c94a0b1240a
status: active
---

# Proposal: pi-tool-active-silent-budget

## WHY

pi 会话"经常卡住不响应"。诊断 + Codex 跨模型评审确认两个根因：

- **A（真假死，主因）**：pi 工具活跃期间不产生助手文本输出，bridge 的 silent 计时器（`SILENT_TIMEOUT=480s`，只在助手文本 / tool-status 落地时重置）误判为 hang 并 SIGKILL 正在干活的 pi 进程。worker 随后盲目重发"继续"再撞一次 480s → 16+ 分钟假死。日志证据：`Pi silent timeout ... no assistant text for 480s` 出现 147 次，且 pi 路径**完全拿不到** Claude 享有的 `bg_agent_running` 1 小时静默延长窗口（runtime.py:1159-1161 仅 Claude 设置该 latch）。
- **B（每长回合 +30s 尾延迟）**：回合 `turn_end` 后 `proc.wait(30)` 超时（`Pi process hung after result event` 184 次，elapsed p50=365s、全部 >30s），pi 进程被后台子进程拖住未及时退出。结果仍交付，属体验问题。Codex 评估主因是 `&` 后台子进程驻留进程组，stdin 继承为次要因素。

## WHAT

- **核心（B 方案 / dynamic tool-active silent budget）**：在流式 loop 中引入"工具活跃"动态静默预算——工具在飞期间用更长的 `TOOL_ACTIVE_SILENT_TIMEOUT`（默认 1800s），无活跃工具时回落到基础 `SILENT_TIMEOUT=480s`（仍可抓模型级 hang）。
- 将 PiRunner 中现为 no-op 的 `tool_execution_start` / `tool_execution_end`（runtime_pi.py:109-115）改为驱动"工具活跃计数 + 静默计时器心跳重置"，不复用 `pending_tool_status`（避免与权威源 `message_update.toolcall_*` 重复计 UI 卡片）。
- 修补 Codex 指出的遗漏：`pending_todo_update` / `pending_agent_launches` 落地时也重置 silent 计时器（runtime.py:1177, 1182 当前不重置）。
- `Popen` 增加 `stdin=subprocess.DEVNULL`（runtime.py:1024），缓解根因 B 的进程驻留。
- 在 silent-timeout 返回结果中**暴露**进度元数据（kill 时是否有工具活跃），供后续 auto-continue 判据 follow-up 使用（本 change 不改 worker 重试逻辑）。

## NOT

- 不改 Claude / OMP runner 的计时语义；只动 pi 路径 + 共享 loop 的最小通用扩展点（新增 state 字段默认值不影响旧 runner）。
- 不改 worker 的 silent-timeout auto-continue 重试逻辑本身（fix #4 拆为独立 follow-up，本 change 仅暴露其所需元数据）。
- 不深挖根因 B 的后台子进程 / 进程组回收机制（仅 stdin 加固）。
- 不强制 pi 把长任务路由到 `bridge-cli bg enqueue`（依赖模型听话，独立 follow-up）。
- 不改全局 `timeout_seconds=7200`（idle 预算）配置。

## Acceptance Criteria

- [ ] pi 执行单个长工具（> 480s，如下载 / 构建）期间不再被 silent 计时器误杀；在 `TOOL_ACTIVE_SILENT_TIMEOUT` 内持续被视为"活跃"。
- [ ] 工具结束后若 pi 模型层不再推进（无任何事件），仍在基础 `SILENT_TIMEOUT`（480s）内被判定 silent timeout（保留 hang 兜底能力）。
- [ ] 多工具连续序列（每个工具产生 lifecycle 事件）期间 silent 计时器被逐事件重置，不累计触发误杀。
- [ ] Claude / OMP runner 的现有计时行为与回归测试不受影响（新增 state 字段默认关闭）。
- [ ] `stdin=subprocess.DEVNULL` 已生效且不破坏既有 streaming 路径（机会性加固）；若同时采集到 `process hung after result event` 日志频次下降则记录为佐证，**但本 change 不以尾延迟下降为硬验收条件**——根因 B 的彻底解决（后台子进程 / 进程组回收）拆为 follow-up。

## Approaches Considered

### Approach A: 单向 latch（minimal viable）
工具首次活跃即把 `silent_limit` 一次性抬到固定值并整回合不降，复用现有 `bg_agent_running` loop 路径。Effort: S，Risk: Low。Pros: 改动最小、复用已验证路径、零 loop 重构。Cons: 粗粒度——任一工具就把整回合静默兜底放宽，真 hung 要等到固定大窗口才发现。

### Approach B: 双向动态预算 + 逐事件心跳（ideal）
新增"工具活跃"计数状态，活跃期用 `TOOL_ACTIVE_SILENT_TIMEOUT` 预算、空闲期回落 480s；每个 lifecycle 事件经独立信号重置计时器（不走 pending_tool_status，避免重复计数）。Effort: M，Risk: Med。Pros: 精确——多工具序列持续重置、单超长工具有专门预算、无活跃工具仍 480s 抓 hang（Codex 推荐方向）。Cons: 需重构 loop 的 silent_limit 升降逻辑（现仅支持只升），测试面更大。

**Selected: B** — Captain 指定。精确区分"工具在飞"与"模型 hang"，从根本上消除假死同时保留 hang 兜底；loop 改动虽大于 A，但通过 max(base, bg-latch, tool-active) 的预算合成可与既有 `bg_agent_running` 单向 latch 共存。

## RISKS

| 风险 | 缓解 |
|------|------|
| 延长 silent 窗口让真正 hung 的 pi 工具更晚被发现 | 用"工具活跃"门控延长；无活跃工具时保留 480s 兜底；预算合成取 max 不无限放大 |
| 改共享流式 loop 影响 Claude/OMP | 新增 state 字段默认 False/0，旧 runner 不设即等价旧行为；补 Claude/OMP 回归单测 |
| `tool_execution_start/end` 不带 id，计数可能不平衡（工具异常无 end） | 计数 clamp ≥0；`turn_end` 时强制归零；不平衡只会偏宽松（多给预算），不会误杀 |
| 预算切换需重新 arm 计时器，与 bg_agent_running 单向 latch 交互 | 统一通过 `_recompute_silent_budget()` 取 max 后 `_reset_silent_timer()`，单一改写点 |

## Rollback / Kill-switch

共享 `_run_streaming` 改动需可快速回退：

- **pi-only 隔离**：动态 tool-active 预算仅由 `state.tool_active_count`（只有 PiRunner 写入）驱动；Claude/OMP 永不增减该计数，`_recompute_silent_budget()` 对它们等价于旧的 `max(base, bg_latch)`。隔离边界即"谁写 tool_active_count"。
- **Feature flag**：新增 `PI_TOOL_ACTIVE_BUDGET_ENABLED`（env，默认 on）。关闭时 `_recompute_silent_budget()` 忽略 tool_active 项，退回纯 base+bg-latch 行为，无需改代码即可现场回滚。
- **最小 revert 清单**：(1) runtime_pi.py 的 tool_execution_start/end 改回 no-op；(2) loop 中移除 tool_active 分支与 pending_silent_reset drain；(3) 移除新增常量/字段。三处独立可分别回退。
- **运维回滚触发条件**：上线后监控 `Pi silent timeout` 与 `Pi process hung` 日志频次；若**任一 runner**（含 Claude/OMP）出现新的 silent-timeout 误杀（回归），立即置 flag=off 并按 revert 清单回退。

## Decision Log

| Date | Decision | Reason |
|------|----------|--------|
| 2026-06-09 | 采用 Approach B（双向动态预算）| Captain 指定；精确区分工具在飞 vs 模型 hang |
| 2026-06-09 | fix #4（auto-continue 进度判据）拆为 follow-up，本 change 仅暴露元数据 | 控制 Critical 变更范围，保持可评审 |
| 2026-06-09 | `TOOL_ACTIVE_SILENT_TIMEOUT` 默认 1800s | 在合法长工具与 hang 发现速度间取平衡，低于 bg-agent 的 3600s |
| 2026-06-09 | plan-review 后：stdin=DEVNULL 降级为机会性加固，删除尾延迟硬验收 | Codex #3：根因 B 主因是进程组驻留，stdin 是次要因素 |
| 2026-06-09 | plan-review 后：新增 Rollback/Kill-switch（feature flag + revert 清单）| Codex #5：共享 _run_streaming 改动需可快速回退 |
| 2026-06-09 | plan-review 后：tool_execution_start/end 定性为 best-effort 信号，补 error/cancel 清理 | Codex #2：事件不带 id，缺失 end 会污染计数 |
