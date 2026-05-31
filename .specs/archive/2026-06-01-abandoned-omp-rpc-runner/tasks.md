# Tasks: omp-rpc-runner

## 1. Core OmpRpcRunner

- [x] 1.1 创建 `feishu_bridge/runtime_omp.py`，实现 OmpRpcRunner 类
  - Validate: 类定义完整，继承 BaseRunner，实现所有 abstract 方法（build_args, parse_streaming_line, parse_blocking_output）
- [x] 1.2 实现 RPC 进程池管理（_processes dict + _RpcProcess dataclass）
  - Validate: spawn → ready 等待(30s timeout) → prompt 发送 → event 流读取 → 结果返回
- [x] 1.3 实现 `run()` override：持久进程复用 + 首次 spawn + tag 注册/cleanup
  - Validate: 第一次调用 spawn 进程，后续调用复用同一进程；tag lifecycle 与 BaseRunner 一致（_active 注册 → _cleanup_tag → cancelled result shape）
- [x] 1.4 实现 streaming event 解析（message_update text_delta/text_end, tool_execution_start/end, turn_end, agent_end）
  - Validate: 文本增量→on_output, 工具状态→on_tool_status, agent_end→break
- [x] 1.5 实现 `cancel()` override：发 `{ type: "abort" }` + 标记 _cancelled + 进程保持存活
  - Validate: cancel 后 streaming loop break → 返回 `{cancelled: True, is_error: False}`；进程仍可用于下次 prompt

## 2. Compact & Context Tracking

- [x] 2.1 实现 compact 支持：拦截 `/compact` prompt，转为 RPC `{ type: "compact" }` command
  - Validate: `/compact` 和 `/compact 自定义指示` 飞书命令正常压缩上下文，返回结果；idle-compact 静默完成
- [x] 2.2 实现 context 跟踪：从 turn_end event 的 message.usage 提取 tokens，计算 peak_context_tokens 和 compact_detected
  - Validate: usage 字段映射正确（input/output/cacheRead/cacheWrite）；compact_detected 在 >50K peak + >50% drop 时触发
- [x] 2.3 `supports_compact()` 返回 True，`supports_auto_compact()` 返回 True
  - Validate: IdleCompactManager 正常调度 idle compact

## 3. Process Lifecycle & Timeout

- [x] 3.1 实现超时状态机：idle timeout → send abort → wait 15s for agent_end → force-kill + evict
  - Validate: idle timeout 返回 `{is_error: True}`, silent timeout 返回 `{silent_timeout: True, is_error: False}`
- [x] 3.2 实现进程健康检查：poll() 检测 crash → 清理 _processes entry → 下次 run() 自动 respawn
  - Validate: 手动 kill omp 进程后，下次 run() 自动 spawn 新进程 + session file resume
- [x] 3.3 实现 idle 回收：后台 daemon 线程每 5min 扫描，>30min 无活动 → SIGTERM + evict
  - Validate: 长时间无消息后进程被清理，下次消息重新 spawn
- [x] 3.4 `has_session()` 保持默认 True（session file 持久化在磁盘）
  - Validate: bridge 重启后 worker.py 走 resume=True 路径，_get_or_spawn() 重新 spawn + session file 恢复
- [x] 3.5 env_extra 在 spawn 时注入，进程存活期间不可变
  - Validate: env_extra 中的 FEISHU_AUTH_FILE 在首次 spawn 时生效

## 4. Registration & Integration

- [x] 4.1 在 main.py `_RUNNER_CLASSES` 注册 `"omp": OmpRpcRunner` + import
  - Validate: `/agent omp` 切换成功
- [x] 4.2 适配 `create_runner()` factory（与其他 CLI runner 共用 kwargs 路径）
  - Validate: 从 config.json 正确创建 OmpRpcRunner 实例

## 5. 验证

- [ ] 5.1 Happy path E2E：飞书发消息 → omp 回复 → 多轮对话 → /compact → /new
  - Validate: 完整流程无报错，流式输出可见
- [ ] 5.2 Cancel/reuse：对话中 /stop → 确认进程存活 → 发新消息 → 正常回复
  - Validate: cancel 后进程不退出，后续消息可复用
- [ ] 5.3 Crash recovery：手动 kill omp 进程 → 发新消息 → 自动 respawn + session 恢复
  - Validate: 用户无感知，对话上下文保留
- [ ] 5.4 Bridge restart recovery：重启 bridge → 发消息到已有 session → 恢复上下文
  - Validate: has_session() 返回 True，resume=True，omp 从 session file 恢复
- [ ] 5.5 回归验证：切回 claude/codex/pi agent 类型 → 正常工作
  - Validate: 现有 runner 不受影响

## Review Report

### Round 1 (2026-05-23, basis: unstaged+untracked)

**Codex Verdict: WARNING** (2 HIGH, 1 MEDIUM)

1. [HIGH] Cancel 返回前未等待 abort drain — `cancel()` 发 abort 后立即返回，残留事件可能污染下一轮 prompt ack 同步
2. [HIGH] 新 runner 无测试覆盖 — OmpRpcRunner 核心路径（spawn/ready/streaming/compact/cancel/timeout/reap）无 unit test
3. [MEDIUM] `_terminate_rpc()` 仅 SIGTERM 不等退出 — hung 进程被从 `_processes` 移除后无法再清理，应复用 `_kill_proc_tree()` 模式

**Scope**: 变更文件与 design.md 影响范围一致（runtime_omp.py NEW + main.py MOD），无 NOT 项引入。

### Round 2 (2026-05-23, basis: unstaged+untracked)

**Codex Verdict: WARNING** (1 HIGH, 1 MEDIUM) — Round 1 修复验证通过

Round 1 fixes verified:
- ✓ cancel drain 正确（prompt 路径）
- ✓ `_terminate_rpc()` 正确委托给 `_kill_proc_tree()`

New findings:
1. [HIGH] `shutdown()` 未被 bridge 调用 — runner 热替换和 bridge 退出时持久进程泄漏 → **已修复**：热替换前调用 `old_runner.shutdown()`，bridge finally 块调用 `runner.shutdown()`
2. [MEDIUM] `/compact` 路径未处理 cancel — `_do_compact()` 不检查 cancelled 状态 → **已修复**：finally 后检查 `was_cancelled`，调用 `_abort_and_drain` 并返回 cancelled result

### Round 3 (2026-05-23, basis: unstaged+untracked)

**Codex Verdict: WARNING** (0 HIGH, 1 MEDIUM) — Round 2 修复验证

Round 2 fixes verified:
- ✓ `runner.shutdown()` 在热替换和 bridge 退出时正确调用
- ✗ `/compact` cancel：except 分支提前 `_cleanup_tag` 清除 cancelled 标记 → **已修复**：except 不再调用 cleanup，改用 `compact_error` 变量延迟错误处理，finally 独占 cleanup

No new CRITICAL/HIGH findings.

## Spec-Check

- result: WARN
- reviewer: code-review
- basis: HEAD=unstaged+untracked
- timestamp: 2026-05-23
- notes: 代码 HIGH findings 全部清零；Phase 5 验证任务（5.1-5.5）需实际飞书环境 E2E 测试
