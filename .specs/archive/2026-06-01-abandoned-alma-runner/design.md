# Design: alma-runner

## 架构

```
┌──────────────────────────────────────────────────────┐
│                    feishu-bridge                      │
│                                                      │
│  ┌────────────┐   ┌─────────────┐   ┌────────────┐  │
│  │ ClaudeRunner│   │ AlmaRunner  │   │ CodexRunner │  │
│  │ (claude -p) │   │  (WS API)   │   │ (codex)    │  │
│  └──────┬─────┘   └──────┬──────┘   └────────────┘  │
│         │                │                            │
│         │        ┌───────┴────────┐                   │
│         │        │  WS Connection │                   │
│         │        │  Manager       │                   │
│         │        │  - connect()   │                   │
│         │        │  - reconnect() │                   │
│         │        │  - send()      │                   │
│         │        └───────┬────────┘                   │
│         │                │                            │
│  ┌──────┴────────────────┴──────────────────────┐    │
│  │           worker.py  process_message()        │    │
│  │  runner.run(prompt, session_id, on_output...) │    │
│  └──────────────────────┬───────────────────────┘    │
│                         │                             │
│              ┌──────────┴──────────┐                  │
│              │  Feishu Card Engine  │                  │
│              │  (streaming updates) │                  │
│              └──────────┬──────────┘                  │
└─────────────────────────┼────────────────────────────┘
                          │ Lark API
                          ▼
                    ┌───────────┐
                    │  Feishu   │
                    └───────────┘

                ┌──────────────────┐
                │       Alma       │
                │  localhost:23001  │
                │                  │
                │  OAuth + API     │──────► Anthropic API
                │  Tool Execution  │        (subscription)
                │  Memory          │
                │  Compaction      │
                │  Thread Store    │
                └──────────────────┘
```

## 数据流：一个用户 turn

```
1. Feishu msg arrives
   │
2. worker.py process_message()
   │
3. AlmaRunner.run(prompt, session_id, on_output, on_tool_status)
   │  session_id = bridge 的 session key（worker 从 bot_id/chat_id/thread_id 生成）
   │
   ├─ 3a. AlmaThreadMap.get(session_id) → alma_thread_id
   │      miss → POST /api/threads → store mapping(session_id → new_thread_id)
   │
   ├─ 3b. Connect WS to ws://localhost:23001/ws/threads (if not connected)
   │
   ├─ 3c. Send generate_response:
   │      {
   │        type: "generate_response",
   │        data: {
   │          threadId: alma_thread_id,
   │          model: "claude-subscription:claude-opus-4-7",
   │          userMessage: { role: "user", parts: [{ type: "text", text: prompt }] },
   │          ephemeralContext: "<bridge safety rules + feishu-cli prompt>",
   │          source: "feishu"
   │        }
   │      }
   │
   ├─ 3d. Receive streaming events:
   │      message_delta (text_append) → on_output(accumulated_text)
   │      message_delta (part_add tool_use) → on_tool_status([{name, hint_data}])
   │      message_delta (tool_output_set) → on_tool_status update
   │      context_usage_update → log context usage (v1 仅 log)
   │      generation_completed → break loop
   │      generation_error → set is_error, break loop
   │
   └─ 3e. Return result dict:
          { result, session_id, is_error, usage, ... }
          session_id = 传入的 bridge session key（原样返回）
   │
4. Feishu card delivered with final text + tool status + model info
```

## 关键决策

### 1. WS 连接策略：长连接 + 事件过滤

AlmaRunner 维护一个到 `localhost:23001` 的持久 WS 连接，多个 session 共享。每条 WS 消息按 `threadId` 过滤分发到对应的 pending run。

**连接管理状态机**：
- `DISCONNECTED` → connect() → `CONNECTED`
- `CONNECTED` → WS close/error → `RECONNECTING` → reconnect() → `CONNECTED`
- `RECONNECTING` 失败 → `DISCONNECTED`

**断连时 in-flight run 处理**：WS connection manager 维护 `{threadId: asyncio.Future}` 的 pending run registry。断连时 fan-out：对所有 pending Future 设置 `ConnectionError`，调用方 `run()` 捕获后返回 `is_error=True`。reconnect 成功后 pending registry 为空（已失败的 run 不自动重试，由用户下一条消息触发新 run）。

理由：Alma 的 WS 设计是单连接多 thread（所有 bridge 都这样用）。短连接（per-run connect/disconnect）会错过 thread 事件且增加延迟。

### 2. Session 映射：独立文件，与 SessionMap 并列

创建 `AlmaThreadMap`，存储 `{session_id → alma_thread_id}` 到 `state/alma-threads-<bot_id>.json`。其中 `session_id` 是 bridge worker 从 `(bot_id, chat_id, thread_id)` 生成的字符串 key，与 `runner.run()` 接收的 `session_id` 参数一致。与现有 `sessions-<bot_id>.json`（存 Claude session_id）并列。

**生命周期**：`alma-threads-*.json` 在 `/agent` 切换时保留（round-trip 可用），仅 `/new` 清理对应 chat 的映射条目。禁用 AlmaRunner 后 stale mapping 无副作用（文件不被读取）。

理由：AlmaRunner 的 "session" 概念是 Alma thread（HTTP 资源），不是 Claude Code session（文件系统目录）。两者生命周期不同，不宜混用同一存储。

### 3. System prompt 注入：ephemeralContext

通过 `generate_response.data.ephemeralContext` 注入 bridge 安全规则。内容通过现有 prompt 管线获取（`get_system_prompts()` / `_GLOBAL_PROMPT_DEFAULTS` / `extra_system_prompts`），序列化为单字符串后传给 Alma。此字段在 Alma 内被 prepend 到 user message context，不修改 Alma 的 SOUL.md 或 persistent settings。

注入内容（来源于 prompt 管线，与其他 runner 保持同步）：
```
CRITICAL: You are running as a subprocess of feishu-bridge. NEVER execute systemctl restart/stop/reload on feishu-bridge.
Do not output 'Status:' lines at the end of responses.
<feishu-cli prompt>
<cron-mgr prompt>
```

理由：不侵入 Alma 配置，bridge 安全规则独立维护。ephemeralContext 是 Alma WS API 的 documented 字段，所有 bridge 都使用。复用现有 prompt 管线确保安全规则与其他 runner 不漂移。

### 4. Tool status 映射

Alma 的 `part_add` delta 格式：
```json
{ "type": "part_add", "part": { "type": "tool-invocation", "toolName": "bash", "toolCallId": "...", "args": {...} } }
```

映射到 bridge 的 `pending_tool_status`：
```python
{ "name": part["toolName"],  # "bash" → "Bash" 需 title-case
  "hint_data": extract_hint(part["toolName"], part["args"]) }
```

**toolCallId → index 映射**：同一 turn 可能多次调用同一 tool（如多次 Bash），`part_add` 中的 `toolCallId` 是唯一标识。AlmaRunner 维护 per-run 的 `{toolCallId: int}` 映射，`part_add` 时分配递增 index，`tool_output_set` 通过 `toolCallId` 关联到正确的 tool 实例更新状态。

Alma 的 tool naming 可能与 Claude Code 不同（小写 vs PascalCase），需要映射表。

### 5. Usage / Cost 获取

Alma 的 `generation_completed` 事件不直接包含 token usage。通过两个途径获取：
- `context_usage_update` 事件（generation 过程中推送，含 context window 使用率）
- GET `/api/threads/:id/context-usage`（generation 结束后查询）

Cost 不可直接获取（Alma 用订阅额度，无 per-turn dollar cost）。`total_cost_usd` 在 AlmaRunner 中返回 None。

### 6. Commandless runner

AlmaRunner 是首个不依赖 CLI binary 的 runner（通过 WS 通信）。需修改三处代码路径：
- `load_config()`：`type=alma` 时 `_resolved_command = None`（跳过 shutil.which）
- `create_runner()`：`type=alma` 时跳过 command resolve 和 validation；通过 `agent_cfg` 透传 `bot_id` 给 AlmaRunner 构造函数
- `switch_agent()`：`type=alma` 时跳过 command resolution，直接从 `_RUNNER_CLASSES` 实例化；透传 `bot_id`
- `switch_provider()`：当前 runner 为 AlmaRunner 时 early return 错误（在 command resolution 之前拦截）

**BaseRunner.command 类型**：放宽为 `Optional[str]`（默认 `None`），AlmaRunner 传 `command=None`。

**BaseRunner ABC 兼容性**：AlmaRunner 对 `build_args` / `parse_streaming_line` / `parse_blocking_output` 三个抽象方法实现为 `raise NotImplementedError("AlmaRunner uses WS, not subprocess")`，完整 override `run()` 绕过 BaseRunner 的 subprocess 执行路径。AlmaRunner.run() 通过调用共享 prompt builder helper（从 `get_system_prompts()` 提取）获取 safety prompt 和 truncation 逻辑，确保与 BaseRunner.run() 中的 prompt 处理一致。

**`run()` 参数兼容性**：AlmaRunner.run() 接受 BaseRunner.run() 的完整签名，各参数处理方式：

| 参数 | 处理 |
|------|------|
| `prompt` | 传入 generate_response |
| `session_id` | AlmaThreadMap lookup key |
| `on_output` | 调用，传累积文本 |
| `on_tool_status` | 调用，传工具状态列表 |
| `fork_session` | `True` → 返回错误 |
| `tag` | ignore（Alma 不支持） |
| `on_todo_update` | ignore |
| `on_agent_update` | ignore |
| `env_extra` | ignore（Alma 管理自己的环境） |

理由：`switch_provider()` 只切换 provider profile（同 runner class 内换 API key / model），无法跨 runner class。`switch_agent()` + `_RUNNER_CLASSES` 是 bridge 已有的 runner class 切换机制（codex / pi 都走此路径）。

## 影响范围

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `feishu_bridge/runtime_alma.py` | NEW | AlmaRunner + AlmaThreadMap + WS manager |
| `feishu_bridge/main.py` | MOD | `_RUNNER_CLASSES` 注册 alma→AlmaRunner；`create_runner()` 对 type=alma 跳过 command validation |
| `feishu_bridge/worker.py` | MOD | AlmaRunner 兼容性检查（compact 路由、/btw 拦截） |
| `feishu_bridge/commands.py` | MOD | `/agent alma` 命令支持 |
| `tests/test_alma_runner.py` | NEW | 单元测试 |
