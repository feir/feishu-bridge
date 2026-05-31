---
branch: master
start-sha: 160bbdc2cd659f3be590ac0e8d68f0977ba2b1a2
status: abandoned
---

# Proposal: alma-runner

## WHY

2026-06-15 起 `claude -p` 消耗独立 Agent SDK credit（Pro $20/月），bridge 重度使用可能超额。Alma 桌面 app 通过 OAuth PKCE + Anthropic Messages API 直接使用订阅额度，已验证可行。新增 AlmaRunner 通过 Alma 的本地 WebSocket API 调用 LLM，保留 bridge 的飞书卡片体验，同时使用订阅额度计费。

## WHAT

- 新增 `AlmaRunner` 类（继承 `BaseRunner`），通过 `ws://localhost:23001/ws/threads` 与 Alma 通信
- 映射 Alma 的 `message_delta` / `generation_completed` 事件到 bridge 的 `StreamState`
- Bridge session_id ↔ Alma thread_id 双向映射持久化
- 通过 `ephemeralContext` 注入 bridge 安全规则和工具提示
- 手动 `/compact` 调用 Alma 的 `/api/threads/:id/compact`
- `/agent alma` 热切换支持（走 `switch_agent()` + `_RUNNER_CLASSES` 路径）

## NOT

- 不修改 Alma 源码或 app.asar
- 不复刻 OAuth / API client / tool execution / memory / compaction 逻辑（Alma 已有）
- 不支持 Agent 子进程生成
- 不支持 Skills 原生执行
- 不实现 fork-session / /btw（`fork_session=True` 时返回错误）
- 不处理 Alma 不在运行时的自动启动（返回明确错误，用户手动切回 `/agent claude`）
- 不实现 idle compact 自动触发（v1 仅支持手动 `/compact`）
- 不实现 context health alert 卡片渲染（v1 仅 log 记录）
- `/provider` 在 AlmaRunner 活跃时返回错误提示（引导用户使用 `/agent`）

## Acceptance Criteria

- [ ] `/agent alma` 切换后，纯对话和 tool use 场景在 Feishu 正常工作，卡片实时流式更新
- [ ] Alma tool 调用（Bash/Read/Write/Edit 等）在 bridge 卡片中正确显示 tool status
- [ ] 多轮对话通过 Alma thread 维持上下文，/new 创建新 thread
- [ ] 手动 `/compact` 正常工作，不因 token 超限崩溃
- [ ] `/agent claude` 可随时切回 ClaudeRunner，功能不受影响
- [ ] Alma 未运行时返回明确错误消息，不静默失败

## Approaches Considered

### Approach A: Full SubscriptionRunner — 从零直调 API
完全绕开 Claude Code 和 Alma，自己实现 OAuth + Messages API + Tool loop + History + Compaction。
Effort: L (~30 天) / Risk: High
Pros: 无外部依赖，完全可控
Cons: 重复 Alma 已解决的所有问题，工期长，sandbox 从零缺乏 battle-test

### Approach B: Patch Alma Feishu 输出
修改 Alma app.asar 的 FeishuBridge，将纯文本发送改为交互式卡片。
Effort: M (~10-15 天) / Risk: Medium
Pros: 单系统架构
Cons: 在 minified 代码中做 feature dev，每次 Alma 更新需 re-patch + codesign

### Approach D: AlmaRunner — Alma WS API 做 LLM 后端
Bridge 保留飞书卡片，通过 Alma 本地 WS API 调用 LLM pipeline。
Effort: S (~5-7 天) / Risk: Low
Pros: 最小工作量，两端独立演进，不修改 Alma
Cons: 依赖 Alma 桌面 app 运行

**Selected: D** — 工作量是 A 的 1/5，不修改 Alma 源码（vs B），两个成熟系统通过稳定 WS API 集成。

## RISKS

| 风险 | 严重度 | 缓解 |
|------|--------|------|
| Anthropic 调整 OAuth API 计费分类 | HIGH | ClaudeRunner 作为 fallback，`/agent claude` 可热切换 |
| Alma WS API 在版本更新中变更 | MED | 单点依赖（唯一执行路径）；启动时 WS 兼容性探测 + 记录已测试版本；版本更新后跑集成测试 |
| Alma 进程未运行时 bridge 无法使用 | MED | 返回明确错误，用户通过 `/agent claude` 手动切回 |
| Alma thread 与 bridge session 映射不一致 | LOW | 映射文件 atomic write；不一致时创建新 thread（auto-heal） |

## Rollback Plan

1. `/agent claude` 切回 ClaudeRunner（即时生效）
2. `sessions-*.json` 保留不动（ClaudeRunner 使用的映射）
3. `alma-threads-*.json` 保留即可（不被读取，无副作用）；如需清理可直接删除
4. 代码回退：从 `_RUNNER_CLASSES` 移除 `alma` 条目，`runtime_alma.py` 可保留或删除
5. `BaseRunner.command` 改为 `Optional[str]` 是向后兼容变更，无需回退

## Decision Log

| Date | Decision | Reason |
|------|----------|--------|
| 2026-05-15 | 选择 Approach D (AlmaRunner) over A (SubscriptionRunner) | Alma 已实现 OAuth+API+Tools+History+Compaction 全栈，bridge 只需 WS 适配层 |
| 2026-05-15 | 使用 ephemeralContext 注入 system prompt，不修改 Alma SOUL | 不侵入 Alma 配置，bridge 安全规则独立维护 |
| 2026-05-15 | 必须禁用 Alma 内置 Feishu bridge | 避免两个 bot 同时响应同一消息 |
| 2026-05-15 | `/agent alma` 走 switch_agent() 路径，放弃 `/provider` 跨 runner-class 切换 | switch_provider() 只切 profile 不切 runner class（R1 #1 CRITICAL） |
| 2026-05-15 | v1 去掉 auto fallback / idle compact / context alert | 降低 scope 到 80% path，5-7 天可达（R1 #2 #4 #7） |
| 2026-05-15 | fork_session=True 显式拦截 | AlmaRunner 不支持 /btw，需 guard 而非静默忽略（R1 #8） |
