# Design: claude-bg-hook-bridge

## 技术方案

```
Claude Code turn 内:

  Claude AI → Bash(run_in_background=true, command="acpx exec ...")
                │
     ┌──────────┼──────────────── fan-out: 各 hook 看原始 input ──┐
     ▼          ▼                                                  ▼
  rtk-rewrite  bg-task-redirect                          tool-approval-v2
  (改写 cmd)   (检测 run_in_background                    (deny-list 检查
               → 包装为 feishu-cli                         基于原始 command)
                 bg enqueue)
     │          │                                                  │
     └──────────┼──────────────── 合并 ────────────────────────────┘
                ▼
  Claude Code 执行改写后的命令（同步，秒级）
  → 返回 {"task_id": "abc123", "enqueue_latency_ms": 12}
  → Claude AI 看到 "任务已入队"，结束 turn

后台（bridge 进程存活期间）:

  bg_supervisor 1s poller ──┐
  UDS wake (CLI nudge) ─────┤
                            ▼
  ┌───────────────────────────────────────┐
  │ task-runner wrapper                   │
  │ spawn 原命令 (--cwd 保留工作目录)       │
  │ wait() → exit code → manifest.json   │
  │ UPDATE bg_runs.delivery_state=pending │
  │ UDS wake (0x03) → bg_supervisor      │
  └───────────────────────────────────────┘
                            │
                            ▼
  ┌───────────────────────────────────────┐
  │ bg_supervisor._scan_delivery_outbox() │
  │ resolve session → build synthetic     │
  │ turn → enqueue_fn(thread_id) → worker │
  │ → 飞书用户在正确 thread 收到通知        │
  └───────────────────────────────────────┘
```

### Hook 检测逻辑

```bash
#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

if ! command -v jq &>/dev/null; then exit 0; fi

INPUT=$(cat)

# 仅拦截 Bash + run_in_background=true
RUN_BG=$(echo "$INPUT" | jq -r '.tool_input.run_in_background // false')
[ "$RUN_BG" != "true" ] && exit 0

# 非 bridge 环境 → pass-through（三重守卫）
# connect probe 而非 -S 存在性检查：crash 后 socket 文件可能残留
WAKE_SOCK=~/.feishu-bridge/wake.sock
python3 -c "
import socket,sys,os
p=os.path.expanduser('~/.feishu-bridge/wake.sock')
s=socket.socket(socket.AF_UNIX)
s.settimeout(0.5)
try: s.connect(p); s.close()
except: sys.exit(1)
" 2>/dev/null || exit 0                               # bridge daemon 未运行
ENV_FILE=~/.feishu-bridge/session.env
[ ! -f "$ENV_FILE" ] && exit 0                       # session context 不存在
command -v bridge-cli &>/dev/null || exit 0           # CLI 不可用
```

### 命令改写（结构化传参，避免 shell 引号问题）

```bash
source "$ENV_FILE"   # → FEISHU_CHAT_ID, FEISHU_THREAD_ID
SID=$(echo "$INPUT" | jq -r '.session_id // empty')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

# on_done_prompt: 保留原命令前 200 字符供 Claude 恢复上下文
ON_DONE=$(printf 'Background task completed. Original command: %.200s' "$CMD")

# 构造 --cmd-json（结构化 argv，不做 shell 引号拼接）
CMD_JSON=$(jq -n --arg c "$CMD" '["bash", "-c", $c]')

# 构造改写后的命令
REWRITTEN="bridge-cli bg enqueue"
REWRITTEN="$REWRITTEN --chat-id $(printf '%q' "$FEISHU_CHAT_ID")"
REWRITTEN="$REWRITTEN --session-id $(printf '%q' "$SID")"
REWRITTEN="$REWRITTEN --on-done-prompt $(printf '%q' "$ON_DONE")"
[ -n "${FEISHU_THREAD_ID:-}" ] && REWRITTEN="$REWRITTEN --thread-id $(printf '%q' "$FEISHU_THREAD_ID")"
[ -n "$CWD" ] && REWRITTEN="$REWRITTEN --cwd $(printf '%q' "$CWD")"
REWRITTEN="$REWRITTEN --cmd-json $(printf '%q' "$CMD_JSON")"

# 构造 updatedInput（不含 permissionDecision — 权限由 tool-approval-v2 决定）
ORIGINAL_INPUT=$(echo "$INPUT" | jq -c '.tool_input')
UPDATED_INPUT=$(echo "$ORIGINAL_INPUT" | jq \
  --arg cmd "$REWRITTEN" \
  '.command = $cmd | .run_in_background = false')

jq -n \
  --argjson updated "$UPDATED_INPUT" \
  '{
    "hookSpecificOutput": {
      "hookEventName": "PreToolUse",
      "updatedInput": $updated
    }
  }'
```

### 安全模型

Claude Code hooks 为 **fan-out** 模型（lessons: "fan-out 非 pipeline，各 hook 看原始 input"）：

- tool-approval-v2 检查**原始命令**（如 `kubectl delete pods`），deny-list 命中时整个 tool 调用被阻止
- bg-task-redirect 的 updatedInput 只在 tool 未被 deny 时生效
- hook 不输出 `permissionDecision`，不会覆盖 tool-approval-v2 的 deny 决定

防御层次：
1. tool-approval-v2 deny-list 基于原始命令（fan-out 模型保证）
2. hook 不输出 allow（不会误授权）
3. 自动化测试验证 deny-list 命令在 run_in_background=true 时仍被拒绝


### RTK 改写组合契约

Claude Code hooks 为 fan-out 模型，`rtk-rewrite` 和 `bg-task-redirect` 同时收到原始 `tool_input`：

| 场景 | rtk-rewrite | bg-task-redirect | 最终行为 |
|------|------------|------------------|----------|
| `run_in_background=false` + RTK 命令 | 改写 command | exit 0（不匹配） | RTK 改写生效 |
| `run_in_background=true` + RTK 命令 | 改写 command | 包装原始 command 为 enqueue | 两个 updatedInput 竞争 |
| `run_in_background=true` + 非 RTK 命令 | exit 0 | 包装为 enqueue | bg-task-redirect 生效 |

**竞争解决**：场景 2 中，两个 hook 各自产出 `updatedInput`。Claude Code 对 fan-out 中的多个 `updatedInput` 取**最后一个**（数组顺序）。bg-task-redirect 位于 rtk-rewrite 之后，其 updatedInput 优先。

bg-task-redirect 的 `--cmd-json` 包装的是**原始命令**（从 stdin `.tool_input.command` 读取），RTK 改写对 enqueue 内容无影响。后台任务执行时 `task-runner` 调用原始命令，RTK 改写语义在该上下文中不适用（RTK 是 Claude Code 沙箱的 PATH 补丁，task-runner 有独立 PATH 配置）。

### session.env 扩展

worker.py `_write_session_env` 增加 `FEISHU_THREAD_ID` 字段：

```python
env_extra = {
    "FEISHU_CHAT_ID": chat_id,
    "FEISHU_BOT_ID": bot_id,
    "FEISHU_THREAD_ID": thread_id or "",  # 新增
}
```

**已知限制**：session.env 是全局文件，多 worker 并发写入存在竞争。单用户场景下概率极低（并发 turn 来自不同 chat 且时间窗口为毫秒级）。彻底解决需改为 per-session env 文件，留后续 change。

### Hook 注册顺序

settings.json PreToolUse hooks 按数组顺序：

```
[0] rtk-rewrite.sh      — matcher: Bash — 改写 command
[1] pre-commit-verify.sh — matcher: Bash — 仅匹配 git commit
[2] bg-task-redirect.sh  — matcher: Bash — 检测 run_in_background，改写 tool_input（新增）
[3] tool-approval-v2.sh  — matcher: * — 权限检查
```

bg-task-redirect 放在 tool-approval-v2 **之前**。虽然 fan-out 模型下顺序对安全无影响（各 hook 看原始 input），但放在 approval 之前可确保 updatedInput 在 approval 决定之前已生成。

### 环境降级

| 环境 | wake.sock | session.env | bridge-cli | Hook 行为 |
|------|-----------|------------|------------|-----------|
| Bridge 活跃 | 存在 | 存在 | 可用 | 改写 + enqueue |
| Bridge 停止 | 残留（connect 失败） | 可能残留 | 可用 | exit 0（pass-through）|
| CLI 交互 | 不存在 | 不存在 | 可能可用 | exit 0（pass-through）|
| CI/CD | 不存在 | 不存在 | 不可用 | exit 0（pass-through）|

### 回滚步骤

1. 从 `~/.claude/settings.json` 的 PreToolUse 数组移除 bg-task-redirect 条目
2. 可选：删除 `~/.claude/hooks/bg-task-redirect.sh`
3. 验证：执行一轮对话含 `Bash(run_in_background=true)`，确认走原生后台执行路径

## 关键决策

| Decision | Choice | Rationale |
|----------|--------|-----------|
| CLI 二进制 | `bridge-cli`（非 feishu-cli） | pyproject.toml console_scripts 定义 |
| 传参方式 | `--cmd-json` | 避免 shell 引号拼接；结构化 argv 安全性高 |
| 权限模型 | hook 不输出 permissionDecision | fan-out 模型下由 tool-approval-v2 独立决定 |
| --cwd 来源 | PreToolUse stdin `.cwd` | Claude Code hook stdin 包含 cwd 字段 |
| --thread-id 来源 | session.env `FEISHU_THREAD_ID` | worker.py 增量字段，thread 路由必需 |
| Bridge 存活检测 | wake.sock connect probe | 存在性检查对 crash 残留 false-positive；connect probe 验证 daemon 真正存活 |

## 影响范围

| File | Change |
|------|--------|
| `~/.claude/hooks/bg-task-redirect.sh` | NEW |
| `~/.claude/settings.json` | MOD: PreToolUse 数组插入新 hook |
| `feishu_bridge/worker.py` | MOD: `_write_session_env` 增加 FEISHU_THREAD_ID |
