# Design: multi-agent-runner

## 技术方案

### 架构图

```
                        ┌─────────────────────────────────────┐
                        │          FeishuBot (main.py)         │
                        │                                     │
                        │  config["agent"]["type"] ──┐        │
                        │                            ▼        │
                        │                  create_runner()     │
                        │                      │         │     │
                        └──────────────────────┼─────────┼─────┘
                                               │         │
                    ┌──────────────────────────┐│         │┌───────────────────────────┐
                    │                          ││         ││                           │
                    ▼                          ▼│         │▼                           ▼
┌─────────────────────────────────┐  ┌────────────────────────────────┐  ┌─────────────┐
│        BaseRunner (ABC)         │  │ worker.py                      │  │ commands.py  │
│                                 │  │                                │  │              │
│ + run(prompt, sid, ...) → dict  │  │ runner: BaseRunner             │  │ /model       │
│ + cancel(tag) → bool            │  │ process_message(...)           │  │ /cost        │
│ + build_args() → list  [abc]    │  │ _context_health_alert(...)     │  │ /context     │
│ + parse_streaming_line() [abc]  │  │                                │  │              │
│ + parse_blocking_output() [abc] │  │ 统一接口，不关心 Runner 类型     │  │ 查询 Runner  │
│ + get_model_aliases() [abc]     │  └────────────────────────────────┘  │ 获取模型列表  │
│ + get_default_context_window()  │                                     └─────────────┘
│ # _run_streaming(proc, state)   │
│ # _run_blocking(proc, sid)      │
│ # _kill_proc_tree()             │
│ # _cleanup_tag()                │
└────────┬───────────┬────────────┘
         │           │
         ▼           ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ ClaudeRunner │  │ CodexRunner  │  │ FutureRunner │
│              │  │              │  │ (后续扩展)    │
│ build_args:  │  │ build_args:  │  │              │
│  claude -p   │  │  codex exec  │  │ ...          │
│  --settings  │  │  --json      │  │              │
│  --output-   │  │  -C <dir>    │  │              │
│  format      │  │  -c model_   │  │              │
│  stream-json │  │  instructions│  │              │
│  --append-   │  │  _file=<f>   │  │              │
│  system-     │  │              │  │              │
│  prompt      │  │ parse:       │  │              │
│              │  │  thread.*    │  │              │
│ parse:       │  │  item.*      │  │              │
│  result      │  │  turn.*      │  │              │
│  assistant   │  │              │  │              │
│  stream_evt  │  │ session:     │  │              │
│              │  │  thread_id   │  │              │
│ session:     │  │  (首事件)     │  │              │
│  result.     │  │  bad id →    │  │              │
│  session_id  │  │  静默新建     │  │              │
│  (末事件)     │  │              │  │              │
│              │  │ models:      │  │              │
│ models:      │  │  gpt-5.2-    │  │              │
│  opus/sonnet │  │  codex 等    │  │              │
│  /haiku      │  │              │  │              │
│              │  │ no settings  │  │              │
│ settings:    │  │ no budget    │  │              │
│  --settings  │  │ no delta     │  │              │
│  bridge-     │  │              │  │              │
│  settings.   │  │              │  │              │
│  json        │  │              │  │              │
└──────────────┘  └──────────────┘  └──────────────┘
```

### 数据结构

```python
@dataclass
class RunResult:
    """Runner 统一返回结构。"""
    result: str = ""
    session_id: str | None = None
    is_error: bool = False
    cancelled: bool = False
    usage: dict | None = None           # 累计 usage
    last_call_usage: dict | None = None # 最近一次调用 usage
    model_usage: dict | None = None     # modelUsage (Claude only)
    total_cost_usd: float | None = None # 费用 (Claude only)
    peak_context_tokens: int = 0
    compact_detected: bool = False
    default_context_window: int = 200_000  # 由 run() 从 get_default_context_window() 填入

    def to_dict(self) -> dict:
        """向后兼容：转为 dict，与现有 worker.py 代码对接。"""
        d = {k: v for k, v in asdict(self).items() if v is not None}
        # 保持 camelCase key 向后兼容（worker.py/commands.py 使用 "modelUsage"）
        if "model_usage" in d:
            d["modelUsage"] = d.pop("model_usage")
        return d


@dataclass
class StreamState:
    """流式解析过程中的可变状态。"""
    accumulated_text: str = ""
    session_id: str | None = None
    final_result: dict | None = None
    last_call_usage: dict | None = None
    peak_context_tokens: int = 0
    compact_detected: bool = False
    done: bool = False
    pending_output: list[str] = field(default_factory=list)
    # parse_streaming_line() 追加文本到 pending_output，
    # _run_streaming() 循环中 drain 后调用 on_output()
```

### 配置结构演进

```
旧格式 (向后兼容):                     新格式:
{                                      {
  "bots": [...],                         "bots": [...],
  "claude": {                  →         "agent": {
    "command": "claude",                   "type": "claude",      ← 新增
    "timeout_seconds": 300                 "command": "claude",
  }                                        "timeout_seconds": 300
}                                        }
                                       }

                                       Codex 示例:
                                       {
                                         "bots": [...],
                                         "agent": {
                                           "type": "codex",
                                           "command": "codex",
                                           "timeout_seconds": 300
                                         }
                                       }
```

Session 文件 schema 演进:
```
旧格式:                                  新格式:
{                                        {
  "chat_123": "session_abc",               "_agent_type": "claude",   ← 新增
  "chat_456": "session_def"                "chat_123": "session_abc",
}                                          "chat_456": "session_def"
                                         }
```
加载时检测逻辑:
1. `_agent_type` 缺失 + `agent.type == "claude"` → 视为旧版 Claude schema，
   静默写入 `_agent_type: "claude"`，**保留**现有 sessions（向后兼容）
2. `_agent_type` 缺失 + `agent.type != "claude"` → 清空 sessions + 写入新 `_agent_type`
3. `_agent_type` 存在但与 `agent.type` 不同 → log warning + 清空 sessions + 写入新 type
4. `_agent_type` 存在且匹配 → 正常加载

迁移逻辑 (`load_config()`):
```python
if "claude" in config and "agent" not in config:
    claude_cfg = config.pop("claude")
    config["agent"] = {"type": "claude", **claude_cfg}
```

### 流式协议对比（实测验证）

```
Claude Code:                              Codex (实测 2026-03-20):
─────────────────────────────             ─────────────────────────────
{"type":"assistant",                      {"type":"thread.started",
 "message":{"usage":{...}}}               "thread_id":"uuid"}
                                           → session_id 在此获取
{"type":"stream_event",
 "event":{"type":"content_block_delta",   {"type":"turn.started"}
  "delta":{"type":"text_delta",
   "text":"H"}}}                          {"type":"item.completed",
                                           "item":{"type":"agent_message",
{"type":"stream_event",                     "text":"完整回复文本"}}
 "event":{"type":"content_block_delta",    → 一次性完整文本，无增量
  "delta":{"type":"text_delta",
   "text":"ello"}}}                       {"type":"item.completed",
                                           "item":{"type":"command_execution",
{"type":"stream_event",                     "command":"...",
 "event":{"type":"message_delta",           "aggregated_output":"...",
  "delta":{"context_management":            "exit_code":0}}
   {"applied_edits":[...]}}}}              → 工具执行事件（中间产物）

{"type":"result",                         {"type":"turn.completed",
 "result":"Hello world",                   "usage":{"input_tokens":N,
 "session_id":"uuid",                       "cached_input_tokens":N,
 "total_cost_usd":0.015,                    "output_tokens":N}}
 "modelUsage":{                            → 结束信号 + usage
  "claude-opus-4-6":{
   "contextWindow":200000}}}              错误:
                                          {"type":"error",
 → session_id 在末事件获取                  "message":"..."}
 → 有增量 delta                           {"type":"turn.failed",
 → 有 cost/contextWindow/compact           "error":{"message":"..."}}
```

### System Prompt 注入对比（实测验证）

```
Claude Code:                              Codex (实测 2026-03-20):
─────────────────────────────             ─────────────────────────────
CLI flag:                                 配置覆盖:
  --append-system-prompt "..."              -c model_instructions_file=
                                              "<tmp_file_path>"

优点:                                     优点:
- 直接 flag，无需临时文件                  - 效果等价，指令被正确注入
- 不修改用户 prompt                       - 不修改用户 prompt

缺点:                                     缺点:
- 仅 Claude Code 支持                     - 需创建/清理临时文件
                                          - 文件内容为纯文本指令

实测验证：
  echo "Always start with 'INJECTED:'" > /tmp/test.txt
  codex exec -c model_instructions_file="/tmp/test.txt" "say hello"
  → 回复: "INJECTED: Hello!"  ✅
```

### Session 恢复对比（实测验证）

```
Claude Code:                              Codex (实测 2026-03-20):
─────────────────────────────             ─────────────────────────────
新建:                                     新建:
  claude -p --session-id <id> -- msg        codex exec --json ... msg
  (worker 传入 uuid, 直接使用)               (worker 传入 uuid, CodexRunner 丢弃
                                             → Codex 自行分配 thread_id)

恢复:                                     恢复:
  claude -p --resume <id> -- msg            codex exec ... resume <id> msg
                                            (resume 是子命令，不是 flag)

无效 session:                             无效 session:
  stderr 含 "session not found" 等          静默创建新 session（新 thread_id）
  → 需要 session_not_found_signatures      → 不需要错误签名检测
  → worker.py 自动 heal                    → 自然愈合

session_id 获取:                          session_id 获取:
  result 事件的 session_id 字段             thread.started 事件的 thread_id
  (末事件)                                 (首事件)
```

### 执行路径矩阵

```
Runner          on_output有值   on_output=None   session_id 来源
──────────────  ─────────────   ──────────────   ──────────────────
ClaudeRunner    _run_streaming  _run_blocking    result 事件 (末)
CodexRunner     _run_streaming  _run_streaming   thread.started (首)
```

CodexRunner **始终** 走 `_run_streaming()`，因为 `session_id` 必须从首事件
`thread.started` 提取，`_run_blocking()` 无此能力。`CodexRunner.parse_blocking_output()`
实现为 `raise NotImplementedError`（不应被调用）。

BaseRunner.run() 中的路径选择逻辑:
```python
if on_output or self.ALWAYS_STREAMING:
    return self._run_streaming(proc, sid, tag, on_output)
else:
    return self._run_blocking(proc, sid, tag)
```
`ALWAYS_STREAMING: ClassVar[bool] = False`，CodexRunner 覆写为 `True`。

### BaseRunner 接口设计

```python
class BaseRunner(ABC):
    """Abstract base for AI Agent CLI runners."""

    DEFAULT_MODEL: ClassVar[str]  # 子类必须定义（如 "claude-opus-4-6", "gpt-5.2-codex"）

    def __init__(self, command, model, workspace, timeout,
                 max_budget_usd=None, extra_system_prompts=None):
        # 共享字段: command, model, workspace, timeout, ...
        # 共享状态: _active (进程追踪), _cancelled (取消标记)

    # ── 子类必须实现 ──
    ALWAYS_STREAMING: ClassVar[bool] = False  # CodexRunner 覆写为 True

    @abstractmethod
    def build_args(self, prompt, session_id, resume, streaming: bool) -> list:
        """构建 CLI 命令行参数列表。streaming 决定输出格式（如 stream-json vs json）。"""

    @abstractmethod
    def parse_streaming_line(self, event: dict, state: StreamState) -> None:
        """解析单行流式 JSONL 事件，更新 StreamState。"""

    @abstractmethod
    def parse_blocking_output(self, stdout: str, session_id: str) -> dict:
        """解析阻塞模式的完整 stdout，返回标准 result dict。"""

    @abstractmethod
    def get_model_aliases(self) -> dict[str, str]:
        """返回 {alias: full_model_name} 映射。"""

    @abstractmethod
    def get_default_context_window(self) -> int:
        """默认 context window 大小（用于 context alert）。"""

    # ── 子类可选覆写 ──
    def get_session_not_found_signatures(self) -> list[str]:
        """返回表示 session 不存在的错误签名列表。默认空。"""
        return []

    def get_extra_env(self) -> dict:
        """额外环境变量。默认空。"""
        return {}

    def get_display_name(self) -> str:
        """用户可见的 Agent 名称（如 "Claude Code", "Codex"）。默认 "AI Agent"。"""
        return "AI Agent"

    def supports_compact(self) -> bool:
        """是否支持 /compact 命令。默认 True（ClaudeRunner），CodexRunner 返回 False。"""
        return True

    # ── 基类实现（共享） ──
    def run(self, prompt, session_id=None, resume=False, tag=None,
            on_output=None, env_extra=None) -> dict:
        """主入口。调用 build_args → subprocess → parse。
        注意: 临时资源（如 CodexRunner 的 instructions 文件）在 run() 内
        per-invocation 创建和清理（finally block），不在实例级管理，确保线程安全。"""

    def cancel(self, tag) -> bool: ...
    def _run_streaming(self, proc, sid, tag, on_output) -> dict:
        """循环读取 JSONL，调用 parse_streaming_line(event, state)。
        每次 parse 后 drain state.pending_output → on_output()。"""
    def _run_blocking(self, proc, sid, tag) -> dict: ...
    def _kill_proc_tree(proc, graceful_timeout=15): ...
```

### Runner 工厂

```python
# main.py
_RUNNER_REGISTRY: dict[str, type[BaseRunner]] = {
    "claude": ClaudeRunner,
    "codex": CodexRunner,
}

def create_runner(agent_cfg: dict, bot_cfg: dict,
                  extra_system_prompts: list[str]) -> BaseRunner:
    agent_type = agent_cfg["type"]
    runner_cls = _RUNNER_REGISTRY.get(agent_type)
    if not runner_cls:
        log.error("Unknown agent type: '%s'. Available: %s",
                  agent_type, list(_RUNNER_REGISTRY.keys()))
        sys.exit(1)
    return runner_cls(
        command=agent_cfg["_resolved_command"],
        model=bot_cfg.get("model", runner_cls.DEFAULT_MODEL),
        workspace=bot_cfg["workspace"],
        timeout=agent_cfg.get("timeout_seconds", DEFAULT_TIMEOUT),
        max_budget_usd=agent_cfg.get("max_budget_usd"),
        extra_system_prompts=extra_system_prompts,
    )
```

## 关键决策

1. **subprocess 管理留在 BaseRunner** — spawn、timeout、kill、cancel 逻辑对所有 Agent 通用，不需要子类覆写。子类只负责"构建什么命令"和"怎么解析输出"。

2. **StreamState 而非回调链** — 用可变 dataclass 传递流式解析状态，避免子类管理复杂闭包。BaseRunner 的 `_run_streaming` 循环调用 `parse_streaming_line(event, state)` 并在 `state.done` 时退出。

3. **System prompt 注入策略由 Runner 决定** — `ClaudeRunner` 用 `--append-system-prompt` CLI flag，`CodexRunner` 用 `-c model_instructions_file="<path>"`（写入临时文件）。两种方式都不修改用户 prompt。

4. **模型别名由 Runner 提供，未知别名透传** — 避免 commands.py hardcode 模型列表。已知别名走映射，未知别名直接透传给 CLI（允许用户指定新模型名）。

5. **Agent 类型显式配置** — `agent.type` 字段必填（`"claude"` 或 `"codex"`），不用 `shutil.which` 启发式猜测。Runner 工厂对未知 type 直接 fail-fast。

6. **RunResult dataclass 保持向后兼容** — 返回 dict 接口不变（`to_dict()`），但内部使用 dataclass 确保字段完整性和类型安全。`to_dict()` 保留 `modelUsage` camelCase key（现有 worker.py/commands.py 依赖此名称）。

7. **Usage 字段在 Runner 层归一化** — `RunResult.usage` 统一使用 `cache_read_input_tokens` key。`CodexRunner` 在 `parse_streaming_line` 中将 Codex 的 `cached_input_tokens` 映射为此名称，确保 worker.py `_context_health_alert()` 和 commands.py `/cost` `/context` 均无需感知差异。

8. **on_output 通过 StreamState.pending_output 传递** — `parse_streaming_line` 将文本追加到 `state.pending_output`，`_run_streaming` 循环中 drain 后调用 `on_output()`，避免 ABC 签名引入回调参数。

9. **临时文件生命周期限定在 run() 内** — `CodexRunner` 的 `model_instructions_file` 在 `run()` 的 `try` 块创建、`finally` 块删除，不在实例级 `__init__`/`cleanup()` 管理。共享 runner 实例跨线程并发调用时互不干扰。

10. **用户可见字符串去品牌化** — `EMPTY_RESULT_MESSAGE` 和 `fallback_name` 从 hardcoded "Claude" 改为通用文本或从 Runner 获取 `get_display_name()`。

## 影响范围

### 直接修改文件

| 文件 | 变更类型 |
|------|----------|
| `feishu_bridge/runtime.py` | 重构（RunResult/StreamState + BaseRunner 提取 + CodexRunner 新增） |
| `feishu_bridge/config.py` | 增强（Agent 选择 + 配置结构） |
| `feishu_bridge/main.py` | 修改（Runner 工厂 + 配置 key 变更 + SESSION_NOT_FOUND 泛化） |
| `feishu_bridge/commands.py` | 修改（/model 动态化 + /cost 泛化） |
| `feishu_bridge/worker.py` | 轻微（类型注解 + context alert 泛化） |
| `tests/unit/test_runtime.py` | 新增/修改（CodexRunner 测试 + RunResult 测试） |
| `tests/unit/test_config.py` | 新增（配置迁移测试） |

### 不修改

| 文件 | 原因 |
|------|------|
| `feishu_bridge/api/*` | 飞书 API 层与 Runner 无关 |
| `feishu_bridge/cli.py` | feishu-cli 与 Agent 类型无关 |
| `feishu_bridge/parsers.py` | 消息解析与 Runner 无关 |
| `feishu_bridge/ui.py` | 渲染层与 Runner 无关 |
| `feishu_bridge/data/cli_prompt.md` | 工具描述对所有 Agent 通用 |
