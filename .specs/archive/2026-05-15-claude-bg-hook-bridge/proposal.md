---
branch: master
start-sha: 4613b6fc59c45f0a2747f163948380b825f98173
status: active
---

# Proposal: claude-bg-hook-bridge

## WHY

Claude Code 的 `Bash(run_in_background=true)` 通知机制是进程内事件（stdout `queue-operation`）。Bridge 的 per-turn 模型在收到 result 事件后杀进程，5 分钟级后台任务（如 Codex plan-review / code-review）的通知必然丢失——进程死后无人生成事件。

Bridge 已有完整的 bg_supervisor + bg_tasks_db 投递系统（`2026-04-20-feishu-bridge-bg-tasks`），但只追踪 bridge 主动创建的后台任务（CLI `bg enqueue`），与 Claude Code 内部后台任务完全隔离。

实测：3 轮 Codex plan-review 中 2 轮通知丢失（Round 1、Round 2），仅 Round 3 因 Monitor 工具调用使进程偶然存活而成功。

## WHAT

- 新增 PreToolUse hook `bg-task-redirect.sh`：拦截 `Bash(run_in_background=true)` 调用，将命令改写为 `bridge-cli bg enqueue -- <原命令>`，run_in_background 置 false
- hook 从 `~/.feishu-bridge/session.env` 读取 `FEISHU_CHAT_ID` + `FEISHU_THREAD_ID`，从 stdin JSON 读取 `session_id` 和 `cwd`，组装 enqueue 参数（含 `--cwd`、`--thread-id`、`--cmd-json`）
- 注册到 `~/.claude/settings.json` 的 PreToolUse Bash hook 列表（位于所有其他 Bash hooks 之后）
- hook 不输出 `permissionDecision`——权限由 tool-approval-v2 基于原始命令决定（Claude Code hooks 为 fan-out 模型，各 hook 看原始 input）
- 非 bridge 环境降级检测：`wake.sock` connect probe 失败（daemon 未运行或 crash 残留）/ `session.env` 不存在 / `bridge-cli` 不在 PATH → 静默 pass-through
- worker.py 修改：`_write_session_env` 增加写入 `FEISHU_THREAD_ID`（现有字段增量，minimal change）

## NOT

- 不修改 bg_tasks_db schema（复用现有 kind='adhoc'）
- 不改变 `Agent(run_in_background=true)` 的行为（Agent 已有通知机制）
- 不拦截 CLI 交互模式（仅 bridge 环境生效）
- 不修改 Claude Code 本体
- 不解决 session.env 多 worker 竞争写入问题（单用户场景概率极低，留后续 per-session env 改造）

## Acceptance Criteria

- [ ] Bridge 环境下已通过 approval 的 `Bash(run_in_background=true)` 被自动改写为 `bridge-cli bg enqueue`，任务完成后 bg_tasks.db 显示 delivery_state='sent'
- [ ] deny-list 命令（如 `kubectl`、`kill -9`）即使触发 run_in_background 仍被正确拒绝
- [ ] CLI 交互模式（wake.sock 不存在）下 hook 静默 pass-through
- [ ] rtk-rewrite 与 bg-task-redirect 同时匹配时行为确定（bg-task-redirect 包装原始命令，RTK 改写不影响 enqueue 内容）
- [ ] 现有 PreToolUse hooks（rtk-rewrite、pre-commit-verify、tool-approval-v2）行为不受影响
- [ ] 自动化测试覆盖 hook 改写逻辑 + 引号安全 + 环境降级 + RTK 交叉场景

## Approaches Considered

### Approach A: PreToolUse hook 劫持（Selected）
- Effort: S (~1-2 days)    Risk: Low
- Pros: 最小 bridge 代码改动（仅 session.env 增量字段）；完全复用 bg_supervisor 成熟投递链路；非 bridge 环境自动降级
- Cons: hook 执行顺序依赖 settings.json 数组位置；Claude AI 在同一 turn 看不到任务输出（需等下一 turn）；session.env 多 worker 竞争是已知限制

### Approach B: runtime.py 解析 + watcher 进程
- Effort: L (~1-2 weeks)    Risk: High
- Pros: 在 bridge 内部完整解决
- Cons: 依赖 Claude Code 内部实现细节；需 schema migrate；新增 watcher 进程运维负担

### Approach C: Skill/CLAUDE.md 规则引导
- Effort: XS (~2 hours)    Risk: Low
- Pros: 零代码改动
- Cons: 依赖 LLM 指令遵从，偶有失效

**Selected: A** — 最低风险，复用现有基础设施，hook 模式已有 rtk-rewrite 验证。

## RISKS

| Risk | Impact | Mitigation |
|------|--------|------------|
| session.env 多 worker 竞争写入 | 通知发到错误 chat | 单用户场景概率极低；后续 per-session env 改造可彻底解决 |
| session.env 残留（bridge crash 后文件未清理） | CLI 模式误触发 redirect | wake.sock connect probe（crash 后 socket 文件可能残留，connect 验证 daemon 真正存活）+ feishu-cli 可用性检查 |
| 引号/转义错误导致 enqueue 命令损坏 | 后台任务执行失败 | 使用 `--cmd-json` 结构化传参，不做 shell 字符串拼接 |
| RTK 与 bg-task-redirect updatedInput 竞争 | fan-out 中多 hook 改写同一 tool | bg-task-redirect 位于 RTK 之后，updatedInput 取最后一个；enqueue 包装原始命令，RTK 改写语义在 task-runner 上下文不适用 |
| hook 产出 allow 绕过 deny-list | 安全风险 | hook 不输出 permissionDecision，权限由 tool-approval-v2 独立决定 |

## Decision Log

| Date | Decision | Reason |
|------|----------|--------|
| 2026-04-30 | Approach A: PreToolUse hook | 最小改动，复用 bg_supervisor |
| 2026-04-30 | 复用 kind='adhoc' | 语义兼容 + 不需要 schema migrate |
| 2026-04-30 | 非 bridge 环境静默 pass-through | 不破坏 CLI 交互模式 |
| 2026-04-30 | CLI 名修正 feishu-cli（非 feishu-bridge-cli） | pyproject.toml console_scripts 定义为 feishu-cli（Codex R1） |
| 2026-04-30 | hook 不输出 permissionDecision | 防止 deny-list 安全绕过（Codex R1） |
| 2026-04-30 | 使用 --cmd-json 传参 | 避免 shell 引号拼接地狱（Codex R1） |
| 2026-04-30 | 传递 --cwd 和 --thread-id | 保留工作目录和 thread 路由上下文（Codex R1） |
| 2026-04-30 | wake.sock 存在性检查替代 60s file-age | 消除活跃 turn >60s 的假阴性（Codex R2） |
| 2026-04-30 | 定义 RTK 改写组合契约 | fan-out 多 updatedInput 竞争规则明确化（Codex R2） |
| 2026-04-30 | wake.sock connect probe 替代存在性检查 | crash 后 socket 残留导致 false-positive（Codex R3） |
| 2026-04-30 | bg 子命令拆分到 bridge-cli（非 feishu-cli） | feishu-cli 职责是 Feishu API 操作，bg 是 bridge 基础设施 |
