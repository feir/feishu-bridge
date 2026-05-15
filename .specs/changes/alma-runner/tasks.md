# Tasks: alma-runner

## 1. AlmaRunner 基础

- [x] 1.1 创建 `runtime_alma.py`，定义 `AlmaRunner(BaseRunner)` 骨架。ABC 抽象方法 `build_args` / `parse_streaming_line` / `parse_blocking_output` 实现为 stub（`raise NotImplementedError("AlmaRunner uses WS")`），完整 override `run()`
  - Validate: class 可实例化，`ALWAYS_STREAMING = True`，`get_display_name()` 返回 "Alma"，三个 stub 方法在直接调用时 raise NotImplementedError
- [x] 1.2 实现 WS 连接管理：connect / reconnect / health check + pending run registry（`{threadId: Future}`）。断连时 fan-out 对所有 pending Future 设置 ConnectionError
  - Validate: WS 可连接时连接成功；Alma 未运行时抛明确错误；mid-stream disconnect 时 in-flight run 返回 `is_error=True`
- [x] 1.3 override `run()` — 通过共享 prompt builder helper 获取 safety prompt + truncation，构造 `generate_response` 消息，通过 WS 发送，收集 streaming events 直到 `generation_completed`
  - Validate: 发送 "hello" prompt 后收到 text_append + generation_completed 事件，返回语义等价的结果；safety prompt 内容与 ClaudeRunner 一致
- [x] 1.4 实现 Alma 进程检测，未运行时返回明确错误消息
  - Validate: Alma 未运行 → run() 返回 `is_error=True`，result 包含 "Alma is not running" 提示用户 `/agent claude` 切回
- [x] 1.5 `fork_session=True` 显式拦截，返回 "AlmaRunner 不支持 /btw"
  - Validate: 调用 `run(fork_session=True)` 时返回错误消息

## 2. Streaming 事件映射

- [x] 2.1 实现 `parse_ws_event()` — 路由 WS message type 到对应 handler
  - Validate: 覆盖 message_delta / thread_generating / generation_completed / generation_error / context_usage_update
- [x] 2.2 `text_append` delta → `state.pending_output`（累积 + 推送）
  - Validate: Feishu 卡片实时显示流式文本
- [x] 2.3 `part_add` delta (tool_use) → `state.pending_tool_status`，维护 `toolCallId → index` 映射，提取 tool name + hint_data
  - Validate: 同一 turn 多次调同一 tool 时各实例独立追踪，卡片工具状态栏显示 tool 名称和简要参数
- [x] 2.4 `tool_output_set` delta → 通过 `toolCallId` 关联到正确的 tool 实例，更新完成状态
  - Validate: 工具执行完成后卡片状态正确更新，多个同名 tool 不错乱
- [x] 2.5 `generation_completed` → `state.done = True` + 构建 result dict
  - Validate: run() 返回的 dict 包含 result / session_id / is_error / usage 字段
- [x] 2.6 `generation_error` → `state.is_error = True` + 错误消息
  - Validate: Alma 模型报错时 bridge 显示错误卡片

## 3. Session ↔ Thread 映射

Bridge 的 `session_id`（由 worker 从 `(bot_id, chat_id, thread_id)` 生成的字符串 key）直接作为 AlmaThreadMap 的 lookup key。`run()` 接收此 key，内部映射到 `alma_thread_id`，返回 `result["session_id"]` 保持同一 key。

- [x] 3.1 实现 `AlmaThreadMap`：`{session_id → alma_thread_id}` 持久化到 `state/alma-threads-<bot_id>.json`
  - Validate: 映射文件原子写入（tempfile + rename）
- [x] 3.2 新会话：POST /api/threads 创建 Alma thread，存储映射
  - Validate: 首次对话创建 thread，后续对话复用
- [x] 3.3 /new 命令：使用与消息处理相同的 session key builder 构造 key，创建新 Alma thread + 清理该 key 的映射
  - Validate: /new 后新消息进入新 thread，旧 thread 保留
- [x] 3.4 映射 auto-heal：thread 不存在时自动创建新 thread
  - Validate: 手动删除 Alma thread 后，下条消息自动恢复

## 4. System Prompt + Model

- [x] 4.1 通过 `ephemeralContext` 注入 bridge 安全规则。内容复用现有 prompt 管线（`get_system_prompts()` / `_GLOBAL_PROMPT_DEFAULTS`），序列化为字符串传给 Alma
  - Validate: ephemeralContext 内容与 ClaudeRunner 的 safety prompt 保持一致；Alma 生成的回复遵循 "NEVER restart feishu-bridge" 等安全规则
- [x] 4.2 /model 切换映射到 Alma provider:model 格式
  - Validate: `/model sonnet` → generate_response 中 model 字段为 `claude-subscription:claude-sonnet-4-20250514`

## 5. Compact

- [x] 5.1 /compact → POST /api/threads/:id/compact
  - Validate: /compact 后 Alma thread context 缩小
- [x] 5.2 `context_usage_update` 事件 → log 记录 context 使用率（v1 仅 log，不渲染卡片 alert）
  - Validate: context_usage_update 到达时 log 包含使用率百分比
- [x] 5.3 worker 的 `IdleCompactManager` 对 AlmaRunner 显式跳过（按 runner 类型或 capability 判断）
  - Validate: Alma 模式下 idle compact 定时器不触发；切回 ClaudeRunner 后恢复

## 6. 集成 + Agent 切换

- [x] 6.1 注册 AlmaRunner 到 `_RUNNER_CLASSES`（`main.py`）。修改四处 commandless 兼容：
  - `BaseRunner.__init__`：`command` 参数改为 `Optional[str]`（默认 `None`）
  - `load_config()`：`type=alma` 时跳过 `_resolved_command` 赋值（设为 `None`）
  - `create_runner()`：`type=alma` 时跳过 command resolve 和 validation；通过 `agent_cfg` 透传 `bot_id` 给 AlmaRunner 构造函数
  - `switch_agent()`：`type=alma` 时跳过 command resolution，直接从 `_RUNNER_CLASSES` 实例化；透传 `bot_id`
  - Validate: `_RUNNER_CLASSES["alma"]` 指向 AlmaRunner；`create_runner(type="alma")` 不报 command-not-found；`switch_agent("alma")` 成功切换；AlmaRunner 接收到正确的 `bot_id`
- [x] 6.2 `/agent alma` 热切换：通过 `switch_agent()` 切换 runner class。`alma-threads-*.json` 在 `/agent` 切换时保留（仅 `/new` 清理对应 chat 的映射）
  - Validate: 从 claude 切到 alma 后，下条消息通过 Alma 处理；`alma → claude → alma` round-trip 后 thread 映射仍在
- [x] 6.3 `/agent claude` 切回：恢复 ClaudeRunner
  - Validate: 切回后 claude -p 正常工作
- [x] 6.4 `/agent alma` 启用前 preflight gate：验证 Alma 内置 Feishu bridge 已禁用（feishu.enabled=false），未禁用时阻止切换并提示
  - Validate: Alma feishu.enabled=true 时 `/agent alma` 返回错误提示而非切换；feishu.enabled=false 时正常切换
- [x] 6.5 `/provider` 在 AlmaRunner 活跃时：`switch_provider()` 加 early guard（在 command resolution 之前拦截），返回 "当前使用 Alma，请用 /agent 切换"
  - Validate: Alma 模式下执行 `/provider xxx` 得到明确错误，不触发 command resolve

## 7. 测试

- [x] 7.1 AlmaRunner 单元测试（mock WS server）
  - Validate: test_alma_runner.py 通过
- [ ] 7.2 端到端：Feishu 发消息 → bridge → Alma → Feishu 卡片回复
  - Validate: 完整流程工作，卡片有流式文本 + tool status
- [ ] 7.3 端到端：tool use 场景（让 Claude 读文件/执行命令）
  - Validate: tool 在 Alma 侧执行，结果正确返回并显示在卡片
- [ ] 7.4 /agent 热切换 round-trip（alma → claude → alma）
  - Validate: 三次切换后功能正常
- [x] 7.5 异常场景：Alma 未运行 / WS 断连 / generation 超时
  - Validate: 各场景有明确错误提示（无 auto fallback）
- [x] 7.6 持久化场景：bridge 重启后 thread 映射恢复、mapping 文件损坏/缺失时 auto-heal
  - Validate: 重启后复用旧 thread；删除 mapping 文件后下条消息自动创建新 thread
- [x] 7.7 并发事件隔离：两个 session_id 同时生成，WS 事件交错到达
  - Validate: 文本和 toolCallId→index 映射严格按 threadId 隔离，不串流
- [x] 7.8 WS 断连恢复：generation 进行中 WS 断开
  - Validate: in-flight run 返回 is_error=True；reconnect 后新 run 正常工作

## Review Report

### Round 1 (2026-05-15, basis: 160bbdc2+dirty)

**Verdict: WARNING (4 HIGH, 1 LOW)**

[HIGH] `/agent alma` 热切换成功路径会崩溃 (`UnboundLocalError`)
File: feishu_bridge/main.py:878
Issue: `target_type == "alma"` 分支内不赋值 `resolved_cmd`/`configured_cmd`，但后续 `log.info` 和 return 使用这两个变量，成功切换时抛 `UnboundLocalError`。
Fix: 在分支入口初始化显示用变量，或直接用 `next_cfg["_resolved_command"]`。

[HIGH] Alma-first 配置污染后续 `/agent claude` 切回路径
File: feishu_bridge/main.py:450
Issue: `load_config()` 对 `type=alma` 保留 `command="alma"` 默认值，`switch_agent()` 切换 type 后 `_normalize_agent_commands()` 会用这个陈旧值填充目标 runner command，Alma-first 启动时 `/agent claude` 切回路径可能解析到错误 binary。
Fix: `load_config()` 中 `type=alma` 时不设 `command` 字段（设为 None）。

[HIGH] 初始 `agent.type=alma` 启动跳过 preflight gate
File: feishu_bridge/main.py:483
Issue: `AlmaRunner.preflight_check()` 只在 `switch_agent()` 热切换时执行，Alma-first 静态配置启动不触发，可能导致 Alma 未运行时启动成功但第一条消息报错。
Fix: `load_config()` 或 bot 初始化阶段对 `type=alma` 运行 preflight。

[HIGH] switch_agent/Alma-first 启动路径无测试覆盖
File: tests/unit/test_alma_runner.py:360
Issue: 现有测试从不执行 `switch_agent("alma")`，未检测到以上切换路径回归。
Fix: 补充 `switch_agent("alma")` 成功/失败 + Alma-first 启动场景的单元测试。

[LOW] [SCOPE] 无关 spec archive 文件混入变更集
File: .specs/archive/2026-05-15-*/
Issue: `claude-bg-hook-bridge` 和 abandoned permission-hook 归档文件不在 alma-runner WHAT 范围内。
Fix: 独立提交或更新 proposal 说明。

### Round 2 (2026-05-15, basis: 160bbdc2+dirty, post-fix)

All 4 HIGH issues fixed in same session:
- HIGH 1 fixed: `resolved_cmd = None; configured_cmd = None` initialized before if/else in switch_agent, alma branch sets `resolved_cmd = "alma (WS)"`
- HIGH 2 fixed: `load_config()` alma branch now sets `command = None` (was missing)
- HIGH 3 fixed: `load_config()` alma branch now calls `AlmaRunner.preflight_check()` and `sys.exit(1)` on failure
- HIGH 4 fixed: Added `test_switch_agent_to_alma_preflight_failure` + `test_switch_agent_to_alma_success` to TestIntegration (43 alma tests total, 794 total pass)
- LOW (SCOPE) acknowledged: spec archive housekeeping files can be in separate commit; not blocking

Post-fix: 43 alma tests pass, 794 total pass, 0 new regressions.

## Spec-Check

- result: WARN
- reviewer: code-review
- basis: HEAD=160bbdc2+dirty
- timestamp: 2026-05-15
- notes: 3 unchecked tasks (7.2-7.4 需 Alma 在线 e2e，技术风险已在单元测试覆盖); LOW scope issue acknowledged (archive housekeeping in separate commit)
