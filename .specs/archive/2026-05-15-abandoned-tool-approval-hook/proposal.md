---
branch: master
start-sha: ce2b3728db29a7688fb31a949dd34f6e8e7aebfe
status: abandoned
scope: SINGLE
---

# Proposal: tool-approval-hook

## WHY

Claude Code 在 bridge 的 `--dangerously-skip-permissions` 模式下，遇到不在 `settings.json` allow 白名单中的命令时会静默拒绝（已知 bug：bypass 不覆盖 ask 规则，且非交互模式无法弹出审批弹窗）。用户在飞书会话中完全看不到任何权限请求信息，Claude 表现为"卡住"或悄悄跳过该操作。

Hermes、OpenClaw 等工具在聊天界面展示所有权限请求，用户可实时审批。bridge 缺少这个能力导致 Claude 能力被人为削弱——大量合理命令因不在白名单而被拦截。

本变更通过 PreToolUse hook 拦截被拒命令，发送飞书交互卡片供用户实时审批，审批通过后返回 `permissionDecision: "allow"` 覆盖 sandbox 拒绝。

## WHAT

- 新增 PreToolUse hook 脚本 `~/.claude/hooks/tool-approval.sh`：
  - 接收工具调用信息（tool_name, tool_input）
  - 对已知安全命令（settings.json allow 白名单匹配）直接放行
  - 对需审批命令：通过 `feishu-cli send-message` 发送交互卡片到当前会话
  - 通过文件轮询等待用户决策（`~/.feishu-bridge/approvals/{uuid}.decision`）
  - 返回 `permissionDecision: "allow"` 或 `"deny"` 给 Claude Code
- 扩展 bridge `_on_card_action()` 处理 `action == "tool_approval"` 的按钮回调：
  - 验证操作者身份（复用现有 allowed_users 门控）
  - 写入决策文件（atomic rename）
  - 更新卡片状态显示审批结果
- 注入 `FEISHU_CHAT_ID` 到 worker.py env_extra，供 hook 发送卡片

## NOT

- 跨会话持久化策略文件 —— session 级记忆随会话结束自动清理，不引入永久规则存储
- 替换 settings.json 白名单机制 —— hook 是补充层，不修改现有权限框架
- 非 Bash 工具审批（Read/Write/Edit/Grep/Glob）—— v1 仅拦截 Bash 工具
- 多步操作批量审批 —— 每个 Bash 调用独立审批
- Web UI / 独立审批面板

## Acceptance Criteria

- [ ] AC1: Claude 尝试执行不在 allow 白名单中的 Bash 命令时，飞书会话内 ≤3s 出现交互卡片，显示工具名、命令内容
- [ ] AC2: 卡片提供三个按钮：「允许（仅本次）」「允许（本会话）」「拒绝」。允许 → 命令执行；拒绝 → Claude 收到 deny 并换用其他方式
- [ ] AC2a: 「允许（本会话）」→ 同一会话中后续相同命令前缀（二进制名）自动放行，不再弹卡片
- [ ] AC3: 用户 240s 内未点击 → hook 超时返回 deny（fail-closed），Claude 继续工作
- [ ] AC4: 已在 settings.json allow 白名单中的命令 → hook 不发卡片，直接放行（极低延迟，仅白名单匹配耗时）
- [ ] AC5: bridge 未运行时（hook 通过 feishu-cli 发送失败），hook 默认 deny 并退出，不阻塞 Claude
- [ ] AC6: 多个审批并发到达时不串扰 —— 每个审批用独立 UUID，决策文件不碰撞
- [ ] AC7: settings.json deny 列表中的命令 → hook 不发卡片，直接 deny（尊重硬拒绝规则）
- [ ] AC8: 现有 rtk-rewrite.sh 和 pre-commit-verify.sh hook 行为不受影响（fan-out 语义，各 hook 看原始 input）
- [ ] AC9: 卡片按钮点击后卡片 UI 更新为已审批/已拒绝状态，不可重复操作

## Approaches Considered

### Approach A: PreToolUse Hook + File IPC + Feishu Card（Selected）

- hook 脚本在 PreToolUse 阶段拦截，通过 feishu-cli 发送交互卡片
- 用户通过飞书按钮决策，bridge _on_card_action 回调写入决策文件
- hook 轮询文件获取结果，返回 permissionDecision
- **Effort**: M（~300 LOC hook + ~80 LOC bridge 扩展 + ~150 LOC 测试）
- **Risk**: Low — 建立在已验证的机制上（rtk-rewrite.sh 的 permissionDecision、_on_card_action 的卡片回调、file-based IPC 的 atomic rename）
- **Pros**: 完全在现有框架内实现；hook 与 bridge 松耦合；hook 失败不影响 bridge 主流程
- **Cons**: 文件轮询有最多 500ms 延迟；审批窗口期 hook 进程阻塞

### Approach B: UDS Socket IPC

- hook 通过 UDS socket 与 bridge 直接通信（类似 bg-tasks 的 wake.sock）
- **Effort**: M-L
- **Risk**: Med — 需要新的 listener 线程、协议约定
- **Pros**: 延迟更低（~10ms vs ~500ms）
- **Cons**: 额外进程间通信复杂度；hook 失败模式更多；bridge 未运行时无法 fallback

### Approach C: Bridge 内部拦截（修改 runtime.py stream parsing）

- 修改 Claude CLI stream-json 解析层，检测被拒命令并注入审批流程
- **Effort**: L
- **Risk**: High — Claude CLI stream-json 协议不包含 permission 事件（已验证），需要 hack parsing 层
- **Pros**: 无外部 IPC
- **Cons**: 依赖未公开的 CLI 内部行为；stream-json 协议变更会破坏实现；不可行

**Selected: A** — 所有组件已独立验证可行（probe 测试确认 hook 执行顺序、rtk-rewrite 确认 permissionDecision 覆盖、feishu-cli 确认卡片发送）。文件 IPC 的 500ms 延迟对人类审批场景无实际影响。

## RISKS

| Risk | Impact | Mitigation |
|---|---|---|
| Hook 超时导致 Claude 长时间阻塞 | 用户等待体验差 | 240s 硬上限 + 卡片显示倒计时提示 |
| feishu-cli 发送失败 | 审批请求不可见 | fail-closed：发送失败立即 deny 并退出 |
| 决策文件竞态 | 审批结果错配 | UUID 唯一标识 + atomic rename |
| settings.json allow 匹配逻辑与 Claude Code 不一致 | 已允许命令误触审批卡片 | 匹配逻辑复用 Claude Code 的 glob 语义（prefix match） |
| hook 进程未清理 | 孤儿进程累积 | hook 在 timeout 后无条件退出；决策文件带 TTL 清理 |
| 卡片回调与正常消息并发 | _on_card_action 路由混乱 | tool_approval action 类型独立于现有 human 消息入队路径 |
| 多个 hook 并行触发 | 多张卡片同时出现 | 每张卡片独立，UUID 隔离，用户逐一操作 |
| FEISHU_CHAT_ID 未注入 | hook 不知道发往哪个会话 | hook 检查变量存在，不存在则 deny 退出 |
| Hook 默认 timeout 变更 | 未来版本可能调低 PreToolUse 默认 timeout | 注册时显式配置 timeout: 250000ms |
| exit 0 无输出在 bypass 下是 fail-open | 错误路径意外放行 | 所有错误路径统一输出 permissionDecision: deny |

## Decision Log

| Date | Decision | Reason |
|------|----------|--------|
| 2026-04-27 | 选定 Approach A（PreToolUse Hook + File IPC） | 所有组件已独立验证；低风险增量实现 |
| 2026-04-27 | v1 仅拦截 Bash 工具 | Bash 是唯一有 sandbox 拦截问题的工具类型；Read/Write/Edit 已在 allow 白名单 |
| 2026-04-27 | 默认 deny on timeout（fail-closed） | 安全优先；超时意味着用户可能不在或网络问题 |
| 2026-04-27 | session 级 "允许（本会话）" 记忆 | 基于命令二进制名（首词）做 session 级自动放行。存储在 `~/.feishu-bridge/approvals/session-{session_id}.allowed`，每行一个已批准的命令前缀。随会话结束或 TTL 清理，不引入永久规则存储 |
| 2026-04-27 | 文件 IPC 而非 UDS socket | 文件 IPC 已有成熟模式（atomic rename）；500ms 延迟对人类审批无感知 |
| 2026-04-27 | tool_approval 回调不走 enqueue_turn | 决策写入是即时的文件操作，不需要进 ChatTaskQueue；避免与正常消息排队 |
| 2026-04-27 | hook 匹配 allow 白名单使用 shell glob | 与 Claude Code 的 Bash 权限匹配语义一致（prefix match with `*` wildcard） |
| 2026-04-27 | rtk-rewrite × tool-approval fan-out 语义 | hook fan-out 非 pipeline，所有 hook 看原始 input。tool-approval 内部先调 `rtk rewrite "$CMD"` 获取改写后命令，卡片同时显示原始和改写后命令（如有差异）。hook 返回的 permissionDecision 仅在 rtk-rewrite 未返回 allow 时生效——如果 rtk 已 auto-allow，tool-approval 也 exit 0 不发卡片（避免已改写命令被二次审批）。多 hook 同时返回 permissionDecision 时 Claude Code 取最严结果（deny > allow），实施前需 probe 验证此行为 |
| 2026-04-27 | tool_approval 卡片不走 bridge card_cache | hook 通过 feishu-cli 直接发送卡片，不经过 bridge 发送路径，_card_cache 无此卡片。审批回调在 _on_card_action 中直接构造 post-click 卡片（审批结果 + 操作者 + 时间戳），不调用 rebuild_card_with_selection |
| 2026-04-27 | FEISHU_AUTH_FILE / FEISHU_CHAT_ID 必须无条件注入 | hook 调 feishu-cli 依赖 auth file。不能依赖 runner.wants_auth_file() 条件——即使 runner 自身不需要 feishu-cli，hook 也需要。将 chat_id/bot_id/auth_file 注入提到 wants_auth_file 条件之外 |
| 2026-04-27 | **Probe 结果**: deny 在 bypass 下仍 ENFORCED；unmatched 命令在 bypass 下 RUNS（fail-open） | deny 规则不被 --dangerously-skip-permissions 覆盖 → hook 对 deny 命令 exit 0。bridge 环境中 ENV_SCRUB=1 强制 default mode → unmatched 命令被额外保护。hook 错误路径统一输出 permissionDecision: deny 做 belt-and-suspenders |
| 2026-04-27 | 评估 `defer` 方案后仍选文件轮询 | v2.1.89 新增 PreToolUse `defer` 字段——hook 输出 defer 后 claude -p 暂停，外部通过 --resume 恢复。架构更优雅（零资源占用），但 bridge 的 runner.run() 是同步阻塞的，支持 deferred 状态检测 + 异步恢复需重构 worker 线程模型，改动远超 v1 scope。文件轮询把复杂度封装在 hook 内部，bridge 仅增加 card callback 写决策文件。v2 可考虑 defer 方案 |
| 2026-04-27 | PreToolUse hook 默认 timeout = 600s | 240s 轮询在默认窗口内。settings.json 注册时仍显式配置 `timeout: 250000` 以防默认值变更 |
| 2026-04-27 | 错误路径统一输出 permissionDecision: deny（fail-closed） | exit 0 无输出在 --dangerously-skip-permissions 下等同放行（fail-open）。所有错误路径（feishu-cli 失败、jq 不可用、目录创建失败）统一输出 permissionDecision: deny，确保 fail-closed 语义 |
| 2026-04-27 | ask 列表命令走审批流程 | settings.json 的 ask 规则在 bridge 非交互模式下同样被静默拒绝——这正是 hook 要解决的问题。ask 命令与"两者都不命中"走相同审批路径 |
| 2026-04-27 | 重复点击防护用 bridge 内存集合，不依赖决策文件 | hook 读取决策文件后立即删除，bridge 侧用 _handled_approvals: set 判断重复点击（bridge 重启后集合丢失可接受——重启后 hook 进程也已结束） |
