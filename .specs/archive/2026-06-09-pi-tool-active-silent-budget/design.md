# Design: pi-tool-active-silent-budget

## 技术方案

引入"工具活跃"动态静默预算。核心是把单一固定的 `SILENT_TIMEOUT` 升级为按状态合成的动态预算，并让 pi 的工具生命周期事件成为静默计时器的心跳源。

### 静默预算状态机（每回合）

```
                    tool_execution_start          tool_execution_end (count→0)
                  ┌───────────────────────┐      ┌──────────────────────────┐
                  ▼                       │      ▼                          │
   ┌─────────────────────┐   start   ┌─────────────────────┐   end&count>0  │
   │  IDLE               │──────────▶│  TOOL-ACTIVE        │────────────────┘
   │  budget = 480s      │           │  budget = 1800s     │
   │  (base SILENT)      │◀──────────│  (TOOL_ACTIVE)      │
   └─────────────────────┘  end&count=0└─────────────────────┘
            │                                   │
            │ 任一状态下，bg_agent_running=True 时 budget = max(当前, 3600s)（单向只升）
            ▼
   每次相关事件 → _recompute_silent_budget() → 若变化则 _reset_silent_timer()
```

### 预算合成（单一改写点）

```
def _recompute_silent_budget(state, base_silent):
    return max(
        base_silent,                                       # 480  基础兜底（抓模型 hang）
        BG_AGENT_SILENT_TIMEOUT if state.bg_agent_running else 0,   # 3600 Claude 后台 agent（既有）
        TOOL_ACTIVE_SILENT_TIMEOUT if state.tool_active_count > 0 else 0,  # 1800 pi 工具在飞（新增）
    )
```

- IDLE（无工具、无 bg agent）→ 480s：工具结束后若模型不再推进，480s 内仍被 silent timeout 杀掉，**保留 hang 兜底**。
- TOOL-ACTIVE → 1800s：单个长工具有专门预算，覆盖日志中绝大多数假死场景。
- bg_agent_running 仍以 max 形式参与，**既有 Claude 行为不变**（3600s 单向 latch 被 max 自然包含）。

### 单一有序重置路径（Codex #4）

所有事件分支统一走 `_apply_silent_budget()`，顺序固定：

```
parse event → recompute = _recompute_silent_budget(...)
            → 仅当 recompute != 当前 silent_limit 才更新 nonlocal
            → 用新 limit 恰好 _reset_silent_timer() 一次
```

顺序必须 recompute-before-reset：否则 `tool_execution_end` 时计时器仍以旧的 1800 re-arm，预算不会降回 480，破坏"工具结束后 480s 抓 hang"的核心兜底。禁止在多个分支各自 re-arm。

### 计时器代际守卫（Codex #1，先存在、被 B 放大的竞态）

现 `_silent_timeout_kill` / `_reset_silent_timer` 是裸 `threading.Timer` cancel+recreate，无守卫；已进入 kill 的旧 Timer 仍可能在迟到心跳后赢得竞态杀进程。B 把 re-arm 变热路径，必须加代际 token：

```
_silent_gen = 0
def _reset_silent_timer():
    nonlocal _silent_gen, silent_timer
    _silent_gen += 1; my_gen = _silent_gen
    silent_timer.cancel()
    silent_timer = Timer(silent_limit, lambda: _silent_timeout_kill(my_gen)); silent_timer.start()
def _silent_timeout_kill(gen):
    if gen != _silent_gen or result_received.is_set(): return   # 作废的旧代际不杀
```


### 事件 → 计时器重置映射（_run_streaming loop）

| 来源事件 | 现状 | 改动后 |
|----------|------|--------|
| 助手文本 (pending_output) | 重置 silent | 不变 |
| tool-status (pending_tool_status) | 重置 silent | 不变 |
| **pending_todo_update 落地** | **不重置** | **重置**（补 Codex 遗漏）|
| **pending_agent_launches 落地** | **不重置** | **重置**（补 Codex 遗漏）|
| **pi tool_execution_start** | **no-op** | **count+1 + 心跳重置（升至 1800）** |
| **pi tool_execution_end** | **no-op** | **count-1(clamp≥0) + 心跳重置（降回 480 或维持 latch）** |

`pending_silent_reset` 作为不携带 UI 语义的纯心跳信号，避免复用 `pending_tool_status` 造成 UI tool 卡片重复计数（`message_update.toolcall_*` 仍是唯一权威 UI 源）。

todo/agent 的重置在 `pending_*` 变为非 None 时即触发，**与 `on_todo_update`/`on_agent_update` 回调是否注册解耦**（Codex #6：避免 liveness 语义耦合 UI 回调注册）。

## 关键决策

1. **B 而非 A**：用计数 + 动态预算精确区分"工具在飞"与"模型 hang"，而非单向 latch 把整回合放宽。代价是 loop 的 silent_limit 升降逻辑从"只升"改为"取 max 后可升可降"，通过单一 `_recompute_silent_budget()` 收敛改写点。
2. **用 `tool_execution_start/end`（粗粒度执行生命周期）而非 `message_update.toolcall_*` 做活跃计数**：前者更贴近"正在执行"，后者用于 UI tool 卡片，两路职责分离。这是 **best-effort 信号**——事件仅带 toolName、无 id，不保证严格 start/end 平衡；error / cancel / 缺失 end 都可能留下不平衡计数，故必配 clamp≥0 + turn_end/finally/cancel 归零兜底（tasks 3.1 / 2.5），不作精确生命周期契约。
3. **计数而非 id 集合**：`tool_execution_start/end` 不携带 tool-call id（runtime_pi.py:110-114 既有注释），无法 id 关联，改用计数器 + clamp≥0 + turn_end 归零兜底不平衡。
4. **1800s 而非 3600s**：合法长工具与 hang 发现速度的折中；低于 bg-agent 的 3600s。后续若有更长工具需求可调常量或做成 config。
5. **fix #4 仅暴露元数据**：本 change 输出 `tool_was_active`，不改 worker 重试逻辑，控制 Critical 变更范围。

## 影响范围

| 文件 | 改动 | 类型 |
|------|------|------|
| feishu_bridge/runtime.py | 新增常量、StreamState 两字段、`_recompute_silent_budget()`、改写 _run_streaming silent 重置逻辑、Popen stdin=DEVNULL、silent-timeout 返回 dict 加 `tool_was_active` | [MOD] |
| feishu_bridge/runtime_pi.py | `tool_execution_start/end` 从 no-op 改为驱动计数 + 心跳 | [MOD] |
| feishu_bridge/worker.py | 不改（仅确认未消费新字段）| [UNCHANGED] |
| tests/unit/ | 新增 pi 动态预算单测；回归既有 timing 单测 | [NEW]/[MOD] |

回归保护：所有新增 StreamState 字段默认值令旧 runner（Claude/OMP）行为等价于改动前；`_recompute_silent_budget()` 对 `bg_agent_running` 取 max 保留既有 Claude 后台 agent 语义。
