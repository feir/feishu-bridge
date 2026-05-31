# Design: omp-rpc-runner

## 技术方案

### 架构概览

```
worker.py                          runtime_omp.py                     omp process
─────────                          ───────────────                     ───────────
                                   ┌─────────────────────┐
runner.run(prompt, sid, resume)──▶ │  OmpRpcRunner.run() │
                                   │                     │
                                   │  _processes[sid]    │
                                   │   exists & alive?   │
                                   │     │         │     │
                                   │    YES        NO    │
                                   │     │         │     │
                                   │     │    spawn omp  │──▶ omp --mode rpc
                                   │     │    --mode rpc │       --session-dir ...
                                   │     │         │     │       --model ...
                                   │     │    wait for   │
                                   │     │   { ready }   │◀── { type: "ready" }
                                   │     │         │     │
                                   │     ▼         ▼     │
                                   │  stdin.write(       │──▶ { type: "prompt",
                                   │   { prompt cmd })   │      message: "..." }
                                   │                     │
                                   │  for line in stdout │◀── { type: "event",
                                   │    parse events     │      event: AgentEvent }
                                   │    drain callbacks  │      ...
                                   │    until agent_end  │◀── agent_end
                                   │                     │
                                   │  return result dict │
  ◀────────────────────────────────│                     │
                                   └─────────────────────┘
                                   (process stays alive)
```

### run() 方法流程

```
run(prompt, session_id, resume, tag, on_output, ...)
  │
  ├── prompt starts with "/compact"?
  │     YES → _do_compact(session_id) → return result
  │
  ├── tag registration: _active[tag] = rpc_proc (for cancel)
  │
  ├── _get_or_spawn(session_id, resume)
  │     ├── _processes[sid] alive? → return existing proc
  │     ├── resume=True, proc dead → spawn new with --session-dir (omp 从 session file 恢复)
  │     └── resume=False → send { type: "new_session" } if proc alive, else spawn new
  │           └── wait for { type: "ready" } (timeout 30s)
  │
  ├── _send_command({ type: "prompt", message: prompt })
  │
  ├── _stream_events(proc, tag, on_output, on_tool_status, ...)
  │     ├── for line in proc.stdout:
  │     │     parse JSON → route by type:
  │     │       "response" (command: "prompt") → ack, continue
  │     │       "event" → dispatch by event.type:
  │     │         agent_start/turn_start → status
  │     │         message_update → text_delta → on_output
  │     │         tool_execution_start/end → on_tool_status
  │     │         turn_end → extract usage, update peak_context_tokens
  │     │         agent_end → done, break
  │     ├── idle timeout watchdog (resets on any stdout line)
  │     ├── silent timeout watchdog (resets on text output)
  │     └── cancelled check (tag in _cancelled → break)
  │
  ├── _cleanup_tag(tag) → was_cancelled?
  │     YES → return { result: "任务已取消。", cancelled: True, is_error: False }
  │
  └── return result dict
```

### has_session() 语义

```
has_session(session_id) → bool:
  return True  (继承 BaseRunner 默认值)
```

**设计决策**：`has_session()` 始终返回 True，和 BaseRunner 默认行为一致。理由：
- omp session 状态持久化到 `--session-dir` 下的 session file
- 即使进程不存在，session file 仍在磁盘上，omp 重新 spawn 后可恢复
- worker.py 的 resume gate (`if runner.has_session(sid)`) 只需知道"session 是否可恢复"，答案永远是 yes（session file 不会被 bridge 删除）
- 进程存活检查由 `_get_or_spawn()` 内部处理：进程死了就重新 spawn + session file resume

### cancel 路径

```
run() 入口:
  _active[tag] = rpc_proc          ← tag 注册

cancel(tag) override:
  with _lock:
    proc = _active.get(tag)
    if proc:
      _send_command({ type: "abort" }, proc)
      _cancelled.add(tag)          ← 标记已取消
      return True
    return False
  (进程保持存活 — 不发 SIGTERM)

_stream_events() 循环中:
  每次迭代检查 tag in _cancelled → break

run() 结尾:
  was_cancelled = _cleanup_tag(tag)  ← 复用 BaseRunner._cleanup_tag()
  if was_cancelled:
    return {
      "result": "任务已取消。",
      "session_id": session_id,
      "is_error": False,
      "cancelled": True,
    }
```

与 BaseRunner.cancel() 的差异：BaseRunner 发 SIGTERM 杀进程；OmpRpcRunner 发 RPC abort 保持进程存活。tag/cancelled/cleanup/result 契约完全一致。

### 超时状态机

```
                    ┌─────────────────┐
                    │  _stream_events │
                    │  (reading stdout)│
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
         idle timeout   silent timeout   normal completion
         (no stdout     (no text output  (agent_end event)
          for N secs)    for 480s)            │
              │              │              return result
              │              │
              ▼              ▼
        _send_command    _send_command
        { type: abort }  { type: abort }
              │              │
              ▼              ▼
        wait for agent_end (bounded 15s)
              │              │
         ┌────┴────┐    ┌───┴────┐
         │ received │    │timeout │
         │         │    │        │
         ▼         ▼    ▼        │
     return      SIGTERM proc    │
     timeout     remove from     │
     result      _processes      │
                 return timeout  │
                 result          │
                                 │
                                 ▼
                           same as idle
```

**返回值格式**（与 BaseRunner 一致）：
- idle timeout: `{ result: "OMP 空闲超时...", is_error: True }`
- silent timeout: `{ result: text + warning, is_error: False, silent_timeout: True }`
- 超时后进程被 kill → 从 `_processes` 移除，下次 run() 重新 spawn

### env_extra 处理

**设计决策**：env_extra 仅在 spawn 时注入，进程存活期间不可变。

理由：
- env_extra 在 feishu-bridge 中主要用于注入 `FEISHU_AUTH_FILE` 路径和 `SESSION_ENV_FILE`
- 这些值是 per-bot-scope static 的（bot 启动时确定），不会在 turn 间变化
- RPC 协议没有运行时 env 注入能力
- worker.py 的 `_write_session_env(env_extra)` 写文件，agent 读文件——这个机制在持久进程下仍然有效（文件每次 turn 更新，agent 读取时拿到最新值）

**限制声明**：如果未来有 turn-level env_extra 变更需求（如每条消息不同的 auth context），需要扩展 RPC 协议或改用 spawn-per-turn 模式。

### compact 路径

```
commands.py: runner.run("/compact", sid, resume=True)
                │
                ▼
OmpRpcRunner.run():
  prompt.strip().startswith("/compact") → _do_compact(sid, prompt)
    │
    ├── _get_or_spawn(sid)  (reuse existing process)
    ├── extract custom instructions: prompt[len("/compact"):].strip()
    ├── _send_command({ type: "compact", customInstructions?: ... })
    ├── wait for { type: "response", command: "compact", data: CompactionResult }
    └── return {
          result: "上下文已压缩。",
          session_id: sid,
          is_error: False,
          compact_detected: True,
        }

idle-compact:
  commands.py: runner.run("/compact", sid, resume=True)
  → 走同一路径，无 on_output callback → 静默完成
```

### context 跟踪

**主数据源**：`turn_end` event 中的 `message.usage` 字段

```python
# turn_end event 解析
usage = event.get("message", {}).get("usage", {})
input_tokens  = usage.get("input", 0)
output_tokens = usage.get("output", 0)
cache_read    = usage.get("cacheRead", 0)
cache_write   = usage.get("cacheWrite", 0)

# peak_context_tokens 计算（与 PiRunner 一致）
ctx_tokens = input_tokens + cache_read + cache_write
if ctx_tokens > state.peak_context_tokens:
    state.peak_context_tokens = ctx_tokens

# compact_detected 检测（与 ClaudeRunner 一致）
if (not state.compact_detected
        and state.peak_context_tokens >= 50_000
        and ctx_tokens < state.peak_context_tokens * 0.5):
    state.compact_detected = True
```

**Fallback**：compact response 的 `CompactionResult` 不含 usage 细节，仅用于确认操作成功。compact 后的 context 大小在下一次 turn_end 时更新。

**不使用 get_state**：get_state RPC command 需要额外 round-trip，且 contextUsage 在 compact 后为 null。turn_end event 已包含每轮 usage，足够跟踪。

### 进程池管理

```python
_processes: dict[str, _RpcProcess]

@dataclass
class _RpcProcess:
    proc: subprocess.Popen
    stdin_lock: threading.Lock    # serialize writes to stdin
    last_activity: float          # monotonic timestamp
    session_id: str
```

- **spawn**: `omp --mode rpc --session-dir <workspace>/state/feishu-bridge/omp-sessions/ --model <model>`
- **ready 等待**: 读 stdout 直到 `{ type: "ready" }`，超时 30s
- **stdin 写入**: JSON line + newline，加锁防并发写入（虽然 ChatTaskQueue 已串行）
- **health check**: `proc.poll() is not None` → 进程已退出，清理 _processes entry
- **idle 回收**: 每次 run() 后更新 last_activity；后台 daemon 线程每 5min 扫描，>30min 无活动的发 SIGTERM + evict
- **shutdown**: bridge 退出时遍历 _processes，逐一 SIGTERM

## 关键决策

1. **进程粒度**：一个 session_id 对应一个 omp 进程。omp 内部的 session 管理（new_session/switch_session）不使用，bridge 侧仍按 `(bot_id, chat_id, thread_id)` 管理 session_id 映射。

2. **stdout 消费模式**：RPC 模式的 stdout 是持久流——进程不退出，stdout 不会 EOF。run() 中按 agent_end 事件判断本轮结束，而非 stdout EOF。这是与 BaseRunner._run_streaming() 的核心差异。

3. **resume 语义映射**：
   - `resume=True` + 进程存活 → 直接发 prompt（进程已有上下文）
   - `resume=True` + 进程不存在 → spawn 新进程 + `--session-dir`（omp 从 session file 恢复上下文）
   - `resume=False` + 进程存活 → 发 `{ type: "new_session" }`
   - `resume=False` + 进程不存在 → spawn 新进程（无 session 恢复）

4. **has_session() 永远返回 True**：session file 持久化在磁盘，即使进程死亡也可恢复。进程存活检查是 `_get_or_spawn()` 的内部关注点。

5. **env_extra spawn-time only**：持久进程的 env 在 spawn 时设定，后续不可变。当前 env_extra 内容是 session-static 的，此限制不影响功能。

6. **error 恢复**：omp 进程 crash 后，_processes entry 被清理，下次 run() 自动 spawn 新进程 + session file 恢复。

## 影响范围

| 文件 | 变更 | 类型 |
|------|------|------|
| `feishu_bridge/runtime_omp.py` | 新建 OmpRpcRunner | NEW |
| `feishu_bridge/main.py` | _RUNNER_CLASSES 加 "omp" + import | MOD (2 行) |
