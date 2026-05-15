---
branch: master
status: abandoned
scope: SINGLE
supersedes: tool-approval-hook
---

# Proposal: permission-defer-v2

## WHY

v1 的 tool-approval hook 基于错误假设——hook 返回 `permissionDecision: "allow"` 能覆盖 permission rules。官方文档明确：**"Hook decisions do not bypass permission rules. Deny and ask rules are evaluated regardless of what a PreToolUse hook returns."** 在 `--dangerously-skip-permissions` 模式下，hooks 甚至不执行。整套审批系统（~300 LOC hook + ~80 LOC bridge）在 bridge 环境下是死代码。

## WHAT

用 Claude Code v2.1.89+ 的 `defer` 机制重建审批流程：

1. **去掉 `--dangerously-skip-permissions`**，让 permission 系统正常运作
2. **`Bash(*)` 加入 allow 列表**：headless 模式下 "ask" zone 工具对模型不可见（等同禁用），必须 allow 才能让模型尝试调用 + hooks 触发
3. **PreToolUse hook 精简为 ~30 行**：匹配内部 allow → passthrough；匹配 deny → deny；未匹配 → 返回 `{"permissionDecision": "defer"}`
4. **Bridge 检测 defer 状态**：`runner.run()` 返回后识别 deferred session
5. **Bridge 发送审批卡片**：复用已有 `_handle_tool_approval()` 卡片回调基础设施
6. **审批通过后 `--resume`**：bridge 用 `runner.run(resume=True)` 恢复 session

> **关键发现（Phase 0 R1）**：Permission allow 列表控制模型是否能"看到"工具。Headless 下不在 allow 中的 Bash = 不存在。安全门控从 permission 系统转移到 hook 层：allow 让工具可用，hook 的 defer 做实际审批。

## NOT

- 不修改 Claude Code 源码或 permission 系统
- 不做多步批量审批（每个 deferred tool 独立审批）
- 不覆盖 Read/Write/Edit/Grep/Glob（仅 Bash 工具需审批）
- 不保留 v1 的 hook 内轮询机制（polling 移到 bridge 层）

## 核心风控：Phase 0 Spike

v1 的教训是在未验证假设上建了整套系统。本方案的关键假设**必须**在 Phase 0 验证：

| # | 假设 | PASS 标准 | FAIL 标准 | 失败时 fallback |
|---|------|----------|----------|----------------|
| H1 | `Bash(*)` 在 allow 列表 + 无 bypass 时，headless 模式下 PreToolUse hooks 执行且 `defer` 返回值被尊重 | hook 日志包含 `HOOK_EXECUTED`；stream-json 或进程行为体现 defer 生效（命令未实际执行） | 日志为空（hook 未触发）或命令直接执行（defer 被忽略） | 方案不可行，退回 allow 白名单 |
| H2 | hook 返回 `{"permissionDecision": "defer"}` 后，stream-json 输出包含可识别的 defer 事件 | stdout 包含 JSON 行，`jq` 可解析出含 `defer` 的字段（记录具体 field path） | stdout 无 defer 相关 JSON；或 Claude 报错退出 | 改用 exit code / stderr 检测 |
| H3 | deferred session 用 `--resume` 恢复后，Claude 从 deferred tool call 继续执行 | resume 后 stdout 包含原命令的执行结果（如 `echo hello` 输出 `hello`） | resume 报错、或 Claude 跳过 deferred tool 开始新对话 | 方案不可行 |
| H4 | resume 时 PreToolUse hook 对**同一个 deferred tool call** 再次触发，此时返回 `allow` 可放行 | hook 日志显示第二次调用，且 tool_input 与首次 defer 时相同；tool 成功执行 | hook 未触发（Claude 绕过 hook 直接执行）、或 tool_input 不同 | 改用 `--dangerously-skip-permissions` 做 resume |
| H5 | hook 返回 defer 后，`claude -p` 子进程正常退出（返回控制权给调用方） | 进程在 10s 内退出；记录 exit code 和最后一条 stream-json 事件 | 进程挂起不退出（需 SIGTERM 才能结束） | Phase 2 需改用后台线程 + 超时终止模式 |
| H6 | 多 hook 冲突解决：hook A 返回 `allow` + hook B 返回 `defer` 时，defer 优先 | 命令未执行，stream-json 包含 defer 事件 | 命令直接执行（allow 赢），defer 被忽略 | rtk-rewrite 的 allow 命令绕过审批（可接受，但须明确记录） |

**如果 H1 或 H3 验证失败，整个方案终止，退回方案 A（allow 白名单）。** H5 失败需重新设计 Phase 2 架构。H6 失败是行为澄清，不阻塞方案但影响安全边界。不在未验证的假设上写生产代码。

## Acceptance Criteria

- [ ] AC1: Phase 0 spike 完成，四个假设全部验证通过（或明确失败并触发 fallback）
- [ ] AC2: Claude 执行非白名单 Bash 命令时，bridge 自动发送飞书审批卡片（≤5s）
- [ ] AC3: 用户点击「允许」后，Claude session resume 并从 deferred tool call 继续执行
- [ ] AC4: 用户点击「拒绝」后，Claude session resume 并收到 deny 信息
- [ ] AC5: 240s 超时未响应 → fail-closed，不 resume
- [ ] AC6: allow 白名单内的命令 → 直接执行，无卡片，无延迟
- [ ] AC7: deny 黑名单内的命令 → 直接拒绝，无卡片
- [ ] AC8: 已有功能（消息处理、tool status 显示、card callback）不回归

## Architecture

```
用户发消息 → bridge worker → runner.run(claude -p)
                                ↓
                          Claude 尝试 Bash
                                ↓
                      PreToolUse hook 检查 allow/deny
                           ↙         ↘
                    匹配 allow      未匹配
                    passthrough    返回 defer
                    命令执行          ↓
                                Claude session 暂停
                                runner.run() 返回
                                    ↓
                           bridge 检测 deferred
                           发送飞书审批卡片
                                    ↓
                          用户点击 允许/拒绝
                                    ↓
                    bridge resume session（或放弃）
                                    ↓
                          Claude 继续/中止
```

## Approaches Considered

### Approach A: Allow 白名单（Fallback）
- `Bash(bash *)` + `Bash(python3 *)` 加 allow list
- Effort: 5 min
- 优点：立即可用，零风险
- 缺点：无灰色地带审批，高权限命令只能靠 deny list 兜底

### Approach B: defer + resume（Selected）
- hook 返回 defer → bridge 检测 → 发卡 → resume
- Effort: M（~4h spike + ~4h 实现）
- 优点：真正的人工审批回路；hook 极简（无 polling、无 file IPC）
- 缺点：依赖 defer 机制行为正确（Phase 0 验证）

### Approach C: SIGSTOP/SIGCONT（Rejected）
- bridge 层 SIGSTOP 暂停 Claude 进程
- 缺点：stream-json 中 tool_use 事件出现时 tool 可能已开始执行；timing 不可靠

## RISKS

| Risk | Impact | Mitigation |
|------|--------|------------|
| H1-H6 假设验证失败 | 方案不可行（H1/H3）或需架构调整（H5/H6） | Phase 0 spike 先行，每个假设有二元 PASS/FAIL |
| 去掉 bypass 后 protected path 写入被拦 | .claude/ 等路径 Edit/Write 失败 | user settings.json 已有 Edit(*) + Write(*)；Phase 0 Step 5 验证 settings 层叠 |
| defer 后 claude -p 子进程不退出 | bridge 事件循环阻塞 | H5 验证；失败则改用后台线程 + 超时终止 |
| defer 后 session state 损坏 | resume 后 Claude 行为异常 | spike 验证 + Phase 0 检查 session 文件存储位置 |
| 审批超时后 session 残留 | 资源泄漏 | bridge 实现 240s 审批计时器（独立于 hook timeout）；超时 → 不 resume + 清理 session 文件 + 更新卡片 |
| 单次响应多个 Bash 调用触发多次 defer | 审批队列堆积、resume 链复杂 | Phase 0 测试多命令场景；Phase 2 实现 defer 队列 |
| 多次 defer 导致 resume 链过长 | 用户疲劳 | 「允许（本会话同类命令）」减少后续审批 |
| bridge 重启丢失 deferred session 上下文 | 卡片回调无法匹配到 session | v1: 接受限制（重启后 pending 审批失效）；v2: 可选持久化到 ~/.feishu-bridge/deferred/ |
| Hook 加载/执行失败时 Bash(*) allow = fail-open | 所有 Bash 命令无审批执行 | hook 脚本保持极简（~30 LOC）降低故障率；bridge 启动时验证 hook 可执行；deny 列表作为兜底（高危命令始终拒绝） |

## Implementation Plan

### Phase 0: Spike（验证 defer 行为）~2h

手动测试，不写生产代码。所有结果记录到 `/tmp/defer-spike-results.md`。

**Step 0: 版本检查**
```bash
claude --version  # 必须 >= 2.1.89，否则 spike 终止
```

**Step 1: 创建最小 defer hook**
```bash
#!/usr/bin/env bash
# spike-defer-hook.sh — 放在 ~/.claude/hooks/ 临时注册
LOG=/tmp/defer-spike.log
INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty')
TOOL_INPUT=$(echo "$INPUT" | jq -c '.tool_input // {}')
echo "$(date +%H:%M:%S) HOOK_EXECUTED tool=$TOOL input=$TOOL_INPUT" >> "$LOG"
[ "$TOOL" != "Bash" ] && exit 0
echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"defer"}}'
```

**Step 2: 验证 H1 + H2 + H5**（单次 defer）
```bash
rm -f /tmp/defer-spike.log
claude -p "run: echo hello" --output-format stream-json 2>/tmp/defer-spike-stderr.log \
  | tee /tmp/defer-spike-stdout.json
echo "EXIT_CODE=$?" >> /tmp/defer-spike-results.md
```
- H1 PASS: `/tmp/defer-spike.log` 包含 `HOOK_EXECUTED`
- H2 PASS: `/tmp/defer-spike-stdout.json` 含 defer 相关 JSON（`grep -i defer`）
- H5 PASS: 进程在 10s 内自行退出（不需要 Ctrl+C）
- 记录 exit code、最后一条 stream-json 事件、stderr 内容

**Step 3: 验证 H3 + H4**（resume 后继续）
从 Step 2 的 stream-json 输出中提取 session_id。spike hook 改为：第二次调用时返回 allow（用 `/tmp/defer-spike-resumed` 标志文件区分）：
```bash
#!/usr/bin/env bash
LOG=/tmp/defer-spike.log
INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty')
TOOL_INPUT=$(echo "$INPUT" | jq -c '.tool_input // {}')
echo "$(date +%H:%M:%S) HOOK_EXECUTED tool=$TOOL input=$TOOL_INPUT" >> "$LOG"
[ "$TOOL" != "Bash" ] && exit 0
if [ -f /tmp/defer-spike-resumed ]; then
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
else
  touch /tmp/defer-spike-resumed
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"defer"}}'
fi
```
```bash
claude -p --resume <session_id> "continue" --output-format stream-json \
  | tee /tmp/defer-spike-resume-stdout.json
```
- H3 PASS: stdout 包含 `hello`（原命令执行结果）
- H4 PASS: `/tmp/defer-spike.log` 显示第二次 HOOK_EXECUTED，tool_input 与首次相同

**Step 4: 验证 H6**（多 hook 冲突）
保留 rtk-rewrite.sh + spike-defer-hook.sh 同时注册。测试一个 rtk 会 rewrite 的命令（如 `ls`）：
- H6 PASS: 命令未执行，输出含 defer 事件
- H6 FAIL: 命令直接执行（rtk 的 allow 覆盖了 defer）

**Step 5: 验证 settings 层叠**
```bash
claude -p "read file /tmp/test.txt" --output-format stream-json --settings bridge-settings.json
# 不加 --dangerously-skip-permissions
```
- PASS: Read 工具正常执行（user settings.json 的 allow 规则仍生效）
- FAIL: Read 被拒绝（bridge-settings 覆盖了 user settings）

记录每个假设的 PASS/FAIL，附原始输出截图或日志片段。

### Phase 1: Hook 精简 ~1h

**前置条件**: Phase 0 四个假设全部 PASS。

改写 `~/.claude/hooks/tool-approval.sh`：
- 去掉 feishu-cli 发卡逻辑（移到 bridge）
- 去掉 file IPC polling（defer 不需要）
- 保留 allow/deny list 匹配（快速 passthrough）
- 未匹配命令 → 返回 `{"permissionDecision": "defer"}`
- ~30 LOC

### Phase 2: Bridge defer 检测 + 审批卡片 ~3h

修改 `runtime.py`：
- `RunResult` 新增 `deferred: bool` + `deferred_tool: dict | None` + `session_id: str | None`
- `parse_streaming_line()` 识别 defer 事件（具体 field path 由 Phase 0 H2 确定）
- `_run_streaming()` 返回结果包含 defer 状态
- `build_args()` 去掉 `--dangerously-skip-permissions`；resume 调用时根据 H4 结果决定是否加回 bypass

修改 `worker.py`：
- `process_message()` 检测 `result.deferred`
- deferred → 发送审批卡片 → 启动 240s 审批计时器 → 等待决策
- 审批通过 → resume；拒绝 → resume with deny message；超时 → 不 resume + 更新卡片为超时状态 + 清理 session
- hook timeout（settings.json）从 250s 降至 5s（defer 响应是即时的）

审批超时归属：
- hook timeout（5s）：hook 脚本执行的硬限制，与审批无关
- 审批超时（240s）：bridge 层实现，从发卡到回调的计时器

### Phase 3: Resume + 回归测试 ~2h

- 审批通过 → `runner.run(resume=True, session_id=deferred_session)`
- 审批拒绝 → resume with "The user denied this command, continue without it"
- 超时 → 不 resume，清理 session 文件（Phase 0 确认存储位置）
- 多次 defer 处理（如 Phase 0 发现单次响应可触发多次 defer）：逐个发卡审批，或批量合并
- 回归：正常消息、tool status 显示、已有 card callback
- Session 文件清理：deferred session 超时后删除残留文件

## Decision Log

| Date | Decision | Reason |
|------|----------|--------|
| 2026-04-27 | 废弃 v1 hook（permissionDecision:allow 方案） | 官方文档确认 hook decisions 不覆盖 permission rules |
| 2026-04-27 | 选 defer + resume 而非 SIGSTOP | SIGSTOP timing 不可靠；defer 是官方支持的 headless 审批机制 |
| 2026-04-27 | Phase 0 spike 为硬性前置条件 | v1 教训：未验证假设上建系统 = 无效功 |
| 2026-04-27 | 退回方案 A 作为 fallback | 如果 defer 不可行，allow 白名单是已验证的兜底方案 |
| 2026-04-27 | Plan review R1: 补 H5/H6 + 二元标准 | Review 发现 Phase 0 遗漏进程生命周期和多 hook 冲突假设，且缺 pass/fail 标准 |
| 2026-04-27 | Phase 0 R1: H1 FAIL → 修正架构，Bash(*) 必须在 allow 列表 | Headless 模式下 ask zone 工具对模型不可见，hooks 无法触发。安全门控转移到 hook 层 |
