---
branch: master
start-sha: 0a3f3f0383f54f9743a477b50b15ddd8aee23f5f
status: abandoned
---

# Proposal: omp-rpc-runner

## WHY

PiRunner 用 `--mode json` 每次消息 spawn 新进程，不支持 compact、mid-run steering、host tools。omp 的 RPC 模式（持久进程 + JSON-RPC 2.0 over stdin/stdout）提供这些能力，且进程复用消除 2-5s 冷启动延迟（Bun runtime 加载 + MCP server 初始化 + session file 解析），对多轮对话体验有直接影响。

## WHAT

- 新建 `OmpRpcRunner` 类（`runtime_omp.py`），继承 BaseRunner，override `run()` 管理持久 RPC 进程池
- 注册为 `"omp"` agent type（`_RUNNER_CLASSES`），与 `"pi"` 并存
- 持久进程池：按 session_id 维护 omp RPC 子进程，进程在多次 `run()` 调用间保持存活
- 支持 compact：拦截 `/compact` prompt 前缀，转为 RPC `{ type: "compact" }` command
- 支持 cancel：override `cancel()` 发 `{ type: "abort" }` 而非 SIGTERM
- 支持 context 跟踪：通过 `get_state` RPC command 获取 contextUsage
- 进程健康管理：超时回收、crash 自动清理、bridge 重启后通过 session file 恢复

## NOT

- 不改动 PiRunner（保持 `--mode json` fallback）
- 不实现 host tools、host URI schemes、extension UI（Phase 2 增量）
- 不实现 steer / follow_up（需要 worker.py 架构变更，Phase 2）
- 不改动 BaseRunner 基类
- 不改动 worker.py 的调用模式（保持 runner.run() 接口兼容）

## Acceptance Criteria

- [ ] 飞书发消息，omp agent type 能正常回复，流式输出可见
- [ ] 多轮对话保持上下文（session 持久化在 omp 进程内）
- [ ] `/compact` 命令正常工作
- [ ] bridge 重启后会话可恢复（omp 进程重启，通过 session file resume）
- [ ] 现有 claude/codex/pi agent 类型不受影响

## Approaches Considered

### Approach A: Persistent Process Pool (Selected)
每个 session 维护一个持久 omp RPC 进程。Effort: M, Risk: Med.
Pros: 进程复用减少启动延迟，支持所有 RPC 能力。
Cons: 需要管理进程池（健康检查、超时回收）。

### Approach B: Spawn-per-turn RPC
每次消息 spawn omp --mode rpc，发一次 prompt 后退出。Effort: S, Risk: Low.
Pros: 最小改动，进程生命周期和 PiRunner 一致。
Cons: 每次 spawn 有启动延迟，不能做 mid-run steer，和 --mode json 几乎无差别。

**Selected: A** — RPC 模式的核心价值在于进程持久化。bridge 已有 per-session 串行队列，进程池管理的并发复杂度有限。

## RISKS

1. **进程泄漏**：持久进程 crash/hang 后未清理 → 缓解：idle 超时回收（30min）+ poll() 健康检查 + bridge 退出时 shutdown
2. **compact 路径适配**：commands.py 用 `runner.run("/compact", ...)` 发文本 → 缓解：run() 内拦截 `/compact` 前缀转 RPC command
3. **启动竞态**：首次 prompt 需等 `{ type: "ready" }` → 缓解：带超时的 ready 等待（30s），失败返回错误
4. **env_extra 不可变**：持久进程的 env 在 spawn 时设定，后续 turn 的 env_extra 变更不可见 → 缓解：当前 env_extra 内容是 session-static 的（FEISHU_AUTH_FILE 等），不受影响；未来若需 turn-level env 变更，需扩展 RPC 协议

## Decision Log

| Date | Decision | Reason |
|------|----------|--------|
| 2026-05-23 | 新建 OmpRpcRunner 而非升级 PiRunner | 进程模型根本不同（persistent vs spawn-per-turn），保留回退路径 |
| 2026-05-23 | 选择 Approach A（Persistent Process Pool） | RPC 核心价值在于持久化，spawn-per-turn 和 --mode json 无实质差别 |
| 2026-05-23 | has_session() 保持默认 True | session file 持久化在磁盘，进程死亡不影响 session 可恢复性 |
| 2026-05-23 | env_extra spawn-time only | 当前 env_extra 是 session-static，持久进程不需要 turn-level env 变更 |
| 2026-05-23 | context 跟踪用 turn_end usage | 避免 get_state 额外 round-trip，compact 后 context 在下一 turn_end 更新 |
