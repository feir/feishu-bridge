# Tasks: tool-approval-hook

## 0. 前置验证（Pre-implementation Probe）

- [x] 0.1 Probe: deny 规则在 `--dangerously-skip-permissions` 下的行为
    - 结果：**deny ENFORCED** — deny 规则不被 bypass 覆盖
    - → hook 对 deny 命令 exit 0 即可（Claude Code 自行处理拒绝）
    - bridge-settings.json deny 列表待后续验证（v1 先只检查 settings.json）

- [x] 0.2 Probe: exit 0 无输出在 `--dangerously-skip-permissions` 下的行为
    - 结果：**FAIL-OPEN** — unmatched 命令在纯 bypass 模式下直接执行
    - 但 bridge 环境中 `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` 强制 default mode → unmatched 命令被阻止
    - → hook 错误路径统一输出 `permissionDecision: deny`（belt-and-suspenders，不依赖 ENV_SCRUB）

## 1. Hook 脚本基础框架

- [ ] 1.1 新建 `~/.claude/hooks/tool-approval.sh` —— PreToolUse hook 入口
    - 读取 stdin JSON（tool_name, tool_input, session_id, cwd）
    - 仅匹配 `tool_name == "Bash"` 或 `tool_name == "computer"`，其他工具直接 exit 0
    - 检查 `FEISHU_CHAT_ID` 环境变量存在，不存在则 exit 0（非 bridge 环境直接放行）
    - Validate: 非 Bash 工具调用 → hook exit 0（无输出）；缺少 FEISHU_CHAT_ID → exit 0

- [ ] 1.2 实现 allow/deny 白名单匹配 + rtk-rewrite 交互
    - 先调用 `rtk rewrite "$CMD"` 获取改写后命令（如有）
    - 如果 rtk 返回 allow（exit 0 + permissionDecision: allow），说明 rtk 已自动处理 → hook 也 exit 0 不发卡片（避免已改写命令被二次审批）
    - 从 `~/.claude/settings.json` 和 bridge `bridge-settings.json` 读取 permissions.allow 和 permissions.deny
    - 提取 `Bash(...)` 模式列表
    - 对当前命令做 prefix glob 匹配（与 Claude Code 语义一致：`Bash(git push*)` 匹配 `git push origin main`）
    - 命中 deny → 根据 probe 0.1 结果决定：exit 0（deny 仍生效）或输出 permissionDecision: deny（deny 被绕过）
    - 命中 allow → 直接 exit 0（已授权，不发卡片）
    - **检查 session 级已批准列表**（`~/.feishu-bridge/approvals/session-{session_id}.allowed`）→ 命中则直接输出 permissionDecision: allow，不发卡片
    - 命中 ask → 进入审批流程（ask 在 bridge 非交互模式下同样被静默拒绝）
    - 两者都不命中 → 进入审批流程
    - PATH 处理：hook 开头 `export PATH="$HOME/.local/bin:$PATH"`（sandbox PATH 不含 ~/.local/bin）
    - rtk 不可用时 fallback：跳过 rtk 交互步骤，直接进入 allow/deny/审批逻辑
    - 审批卡片同时显示原始命令和改写后命令（如有差异）
    - Validate: `git push origin main` 命中 `Bash(git push*)` → exit 0 无输出；`systemctl restart feishu-bridge` 命中 deny → 按 probe 结果处理；`design-from-url analyze` 不命中任何规则 → 进入审批；rtk 已 auto-allow 的命令 → exit 0 不发卡片

- [ ] 1.3 实现审批请求发送
    - 生成 UUID 作为 approval_id
    - 通过 `feishu-cli send-message` 发送交互卡片到 `$FEISHU_CHAT_ID`
    - 卡片内容：工具名、命令全文、工作目录（cwd）、三个按钮
    - 按钮设计：
      - 「允许（仅本次）」— decision: "allow_once"
      - 「允许（本会话）」— decision: "allow_session"，附带命令前缀（二进制名）
      - 「拒绝」— decision: "deny"
    - 按钮 value 包含 `{action: "tool_approval", approval_id: "<uuid>", decision: "allow_once"/"allow_session"/"deny", cmd_prefix: "<binary>", chat_id, bot_id}`
    - feishu-cli 发送失败 → 输出 permissionDecision: deny 并 exit 0（fail-closed，不是静默放行）
    - Validate: feishu-cli 可用时卡片成功发送；feishu-cli 不可用时 hook 在 <2s 内退出

- [ ] 1.4 实现文件轮询等待决策
    - 轮询路径：`~/.feishu-bridge/approvals/{approval_id}.decision`
    - 轮询间隔 500ms，总超时 240s
    - 决策文件格式：`allow_once\n`、`allow_session\n<cmd_prefix>\n`、或 `deny\n`
    - 超时 → deny（fail-closed）
    - 读取到决策后删除决策文件（cleanup）
    - Validate: 决策文件写入后 ≤1s hook 读取到结果；240s 内未写入 → hook 返回 deny

- [ ] 1.5 实现 hook 输出（permissionDecision JSON）
    - allow_once / allow_session 决策 → 输出 `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "permissionDecisionReason": "Approved via Feishu card"}}`
    - allow_session 额外动作：将 cmd_prefix 追加到 session patterns 文件（`~/.feishu-bridge/approvals/session-{session_id}.allowed`）
    - deny 决策 → 输出 `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "Denied via Feishu card / timeout"}}`
    - session patterns 文件追加用 flock 保护（多 hook 实例并发写入安全）
    - Validate: allow 输出可被 Claude Code 解析并放行命令；allow_session 后同前缀命令自动放行

## 2. Bridge 侧回调扩展

- [ ] 2.1 扩展 `_on_card_action()` 识别 `tool_approval` action 类型
    - 在鉴权完成后（log.info "Card action" 之后）、`msg_key = SessionMap.format_key(...)` 之前插入路由分支
    - `if value.get("action") == "tool_approval": return self._handle_tool_approval(value, sender_id)`
    - `_handle_tool_approval()` 提取 `approval_id` 和 `decision`（allow/deny）
    - 验证 sender_id 属于 allowed_users（复用现有门控）
    - 与现有 human 消息入队路径完全分离 —— 不走 `enqueue_turn`
    - 维护 `_handled_approvals: set` 内存集合，用于重复点击检测（task 2.3）
    - Validate: tool_approval action 不触发 ChatTaskQueue enqueue；非 tool_approval action 行为不变

- [ ] 2.2 实现决策文件写入
    - 目标路径：`~/.feishu-bridge/approvals/{approval_id}.decision`
    - 确保 `~/.feishu-bridge/approvals/` 目录存在（`mkdir -p` 等效）
    - atomic write：先写 `.tmp` 后 `os.rename`
    - 写入内容：`allow_once\n`、`allow_session\n<cmd_prefix>\n`、或 `deny\n`
    - cmd_prefix 从按钮 value 中提取（bridge 侧不需要解析命令）
    - Validate: 文件内容正确；原子性（不出现半写状态）

- [ ] 2.3 卡片 UI 状态更新
    - hook 通过 feishu-cli 直接发送卡片，不经过 bridge 发送路径，_card_cache 无此卡片
    - **不使用 `rebuild_card_with_selection`**（该方法依赖 _card_cache，会 cache-miss）
    - 回调中直接构造 post-click 卡片 JSON：显示审批结果 + 操作者 + 时间戳
    - 按钮替换为纯文本状态标签（"✅ 已允许 by XXX" / "❌ 已拒绝 by XXX"）
    - 通过飞书 API 返回更新后的卡片（_on_card_action response body）
    - 重复点击防护：检查 `approval_id in _handled_approvals`，命中则返回 toast"已处理"（不依赖决策文件存在性——hook 读取后已删除）
    - Validate: 点击后卡片 UI 更新；重复点击返回 toast"已处理"

## 3. 环境变量注入

- [ ] 3.1 worker.py 环境变量注入
    - **无条件注入**（纯字符串，无副作用）：`FEISHU_CHAT_ID: chat_id`、`FEISHU_BOT_ID: bot_id`
    - **FEISHU_AUTH_FILE**：当前被 `feishu_docs and runner.wants_auth_file()` 门控（worker.py ~line 929）。hook 也需要 auth file，将 auth file 创建提到条件之外——但仍需 feishu_docs 中的 sender_id 来生成 auth content。方案：auth file 创建逻辑从 wants_auth_file 条件中独立出来，只要有 sender_id 就创建
    - hook 检测到 FEISHU_AUTH_FILE 不存在时走降级路径（deny）
    - Validate: 不带 feishu_docs 的场景下 Claude Code 子进程 env 仍包含 FEISHU_CHAT_ID 和 FEISHU_BOT_ID；有 sender_id 时 FEISHU_AUTH_FILE 也存在

- [ ] 3.2 Hook 注册到 settings.json
    - 在 `~/.claude/settings.json` hooks.PreToolUse 数组中追加 tool-approval.sh 条目
    - matcher: "Bash"
    - **显式配置 `"timeout": 250000`**（240s 轮询 + 10s margin；PreToolUse 默认 600s 但显式设置防未来变更）
    - 确保在 rtk-rewrite.sh 之后、pre-commit-verify.sh 之前（或之后，因 fan-out 语义无顺序依赖）
    - Validate: Claude Code 启动后 hook 被识别并在 Bash 工具调用时触发

## 4. 清理与健壮性

- [ ] 4.1 文件 TTL 清理
    - hook 启动时扫描 `~/.feishu-bridge/approvals/` 目录
    - 删除 mtime > 10 分钟的 `.decision` 文件（防累积）
    - 删除 mtime > 24 小时的 `session-*.allowed` 文件（session 级记忆清理）
    - Validate: 旧文件被清理；新文件和活跃 session 文件不受影响

- [ ] 4.2 并发安全
    - 多个 hook 实例并行运行时各自使用独立 UUID，文件路径不碰撞
    - 决策文件写入使用 atomic rename（bridge 侧）
    - hook 读取后立即删除（rm），不影响其他 hook 实例
    - Validate: 模拟 3 个并发审批请求，各自独立完成无串扰

- [ ] 4.3 错误处理兜底（fail-closed：所有错误路径输出 permissionDecision: deny）
    - jq 不可用 → 输出 permissionDecision: deny + exit 0（非 bridge 环境由 FEISHU_CHAT_ID 检查提前 exit，不影响）
    - feishu-cli 不可用 → 输出 permissionDecision: deny + exit 0
    - settings.json 解析失败 → 进入审批流程（不自动 allow，不自动 deny）
    - 决策文件目录创建失败 → 输出 permissionDecision: deny + exit 0
    - FEISHU_CHAT_ID 不存在 → 特殊情况：exit 0 无输出（非 bridge 环境，不需要 deny）
    - Validate: 各错误场景 hook 均在 <2s 内退出；bridge 环境下错误路径不放行命令

- [ ] 4.4 测试可配置性
    - 轮询超时从 `TOOL_APPROVAL_TIMEOUT` 环境变量读取，默认 240s
    - 轮询间隔从 `TOOL_APPROVAL_POLL_INTERVAL` 环境变量读取，默认 500ms
    - Validate: 集成测试可通过环境变量缩短超时

## 5. 测试

- [ ] 5.1 Hook 单元测试
    - allow 白名单匹配正确性（glob 语义）
    - deny 黑名单匹配正确性
    - 非 Bash 工具 → passthrough
    - 缺少 FEISHU_CHAT_ID → passthrough
    - 决策文件轮询超时 → deny 输出
    - Validate: 所有测试用 `echo '{"tool_input":{"command":"..."}, ...}' | bash tool-approval.sh` 模式

- [ ] 5.2 Bridge 回调测试
    - tool_approval action 写入正确的决策文件
    - 重复点击返回 toast 而非重复写入
    - 非 tool_approval action 行为不变（回归）
    - Validate: 现有 test_bridge.py 全部通过

- [ ] 5.3 集成测试
    - 完整链路：hook 发送卡片 → bridge 模拟按钮回调 → hook 读取决策 → 返回 permissionDecision
    - 超时链路：hook 发送卡片 → 无回调 → 240s 后 deny
    - Validate: 集成测试在 <10s 内完成（超时测试用缩短的 timeout 参数）

- [ ] 5.4 回归测试
    - 现有 hooks（rtk-rewrite, pre-commit-verify）行为不变
    - settings.json 结构未被修改（只新增 hook 条目）
    - _on_card_action 对非 tool_approval 消息行为不变
    - Validate: 完整 test suite 零新失败

## Review Report

### Round 1 (2026-04-27, basis: ce2b372+dirty)

**Scope**: tool-approval.sh (221 LOC), main.py (+74), worker.py (+7/-9), settings.json hook entry

**[CRITICAL][Claude] Path traversal in approval_id allows arbitrary file write**
File: feishu_bridge/main.py:1665-1677
Issue: `approval_id` is extracted from the Feishu card button `value` payload with no format validation, then used directly in `approvals_dir / f"{approval_id}.decision"`. Pathlib `/` operator with `../` or absolute paths escapes the approvals directory. Verified: `Path.home()/".feishu-bridge"/"approvals" / "../../tmp/pwn.decision"` resolves to `/Users/feir/tmp/pwn.decision`; absolute path `"/etc/passwd"` resolves to `/private/etc/passwd.decision`. Any authorized user (or compromised bot creds) can write arbitrary `.decision` files anywhere the process has write access.
Fix: Validate `approval_id` as UUID before any path use: `uuid.UUID(approval_id)` in try/except, or `re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', approval_id)`.

**[HIGH][Claude] No tests for tool_approval code path**
Files: tests/unit/, tests/integration/
Issue: Zero test coverage for `_handle_tool_approval`, decision file writing, duplicate-click protection, and the hook script. `grep tool_approval tests/` returns nothing. Tasks 5.1-5.4 in tasks.md are all unchecked with no evidence.
Fix: Add unit tests for (1) valid approval write, (2) duplicate-click returns toast, (3) invalid approval_id rejected, (4) path traversal blocked after fix. Add hook tests for allow/deny/timeout paths.

**[MEDIUM][Claude] _handled_approvals set grows unbounded**
File: feishu_bridge/main.py:682, 1688
Issue: `_handled_approvals: set[str]` accumulates every approval_id for the lifetime of the bridge process. In a long-running deployment with frequent approvals, this is a slow memory leak.
Fix: Use an LRU structure (e.g., `collections.OrderedDict` capped at 1000 entries) or periodically clear entries older than the TTL.

**[MEDIUM][Claude] PIPESTATUS capture after `|| true` is unreliable**
File: ~/.claude/hooks/tool-approval.sh:65-66
Issue: `RTK_OUT=$(rtk rewrite "$CMD" 2>/dev/null || true)` followed by `RTK_EXIT=${PIPESTATUS[0]:-1}`. The `|| true` makes the command substitution always exit 0, so `$?` is always 0. `PIPESTATUS` only tracks pipeline segments, and this is a simple `||` compound command — `PIPESTATUS[0]` reflects the last simple command's status, which is `true` (exit 0). The guard `RTK_EXIT -eq 0 && -n RTK_OUT` works in practice because it checks output content, but the exit code check is misleading.
Fix: Capture exit code before `|| true`: `RTK_OUT=$(rtk rewrite "$CMD" 2>/dev/null); RTK_EXIT=$?; [ $RTK_EXIT -ne 0 ] && RTK_OUT=""`.

**[LOW][Claude] tasks.md known-pitfalls.md diff is out of scope**
File: .claude/ctx/known-pitfalls.md
Issue: 9 lines of known-pitfalls accumulated from prior sessions appear in the working tree diff. These are unrelated to tool-approval-hook.
Fix: Commit or stash separately before this change's final commit.

Codex cross-review: skipped (Codex returned internal error).

| Severity | Count | Claude | Codex | Both |
|----------|-------|--------|-------|------|
| CRITICAL | 1     | 1      | 0     | 0    |
| HIGH     | 1     | 1      | 0     | 0    |
| MEDIUM   | 2     | 2      | 0     | 0    |
| LOW      | 1     | 1      | 0     | 0    |

Verdict: **BLOCK** -- 1 CRITICAL (path traversal) must be fixed before merge.

Re-review: required (CRITICAL security fix)

## Spec-Check

- result: BLOCK
- reviewer: code-reviewer
- basis: HEAD=ce2b372+dirty
- timestamp: 2026-04-27
- notes: Completeness BLOCK — all tasks 1.x-5.x unchecked, no evidence lines, zero test coverage. Correctness: implementation scope aligns with WHAT (hook + bridge callback + env injection); NOT section respected (no cross-session persistence, no non-Bash tools). CRITICAL path traversal finding in approval_id.
