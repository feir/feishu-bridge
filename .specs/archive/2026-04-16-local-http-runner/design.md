# Design: local-http-runner

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                       Worker                            │
│  - runner.run(...)                                      │
│  - runner.has_session(sid)  ← NEW (auto-heal on restart)│
│  - runner.wants_auth_file() ← NEW (gate /tmp file)      │
│  - runner.supports_compact() (existing, now used in alert)│
└────────────────────────┬────────────────────────────────┘
                         │ BaseRunner contract
        ┌────────────────┼─────────────────┐
   ClaudeRunner     CodexRunner    LocalHTTPRunner ◄── NEW
   (subprocess)    (subprocess)    (urllib HTTP)
                                          │
                              ProtocolAdapter (selected by config)
                              ┌───────────┴───────────┐
                       AnthropicAdapter         OpenAIAdapter
                       (POST /v1/messages)      (POST /v1/chat/completions)
                              │                       │
                       omlx, claude-proxy        ollama, omlx, vllm
```

## BaseRunner 契约扩展

新增两个 hook 方法（默认实现保持现有 runner 行为不变）：

```python
class BaseRunner(ABC):
    ...
    # NEW — override in LocalHTTPRunner
    def has_session(self, session_id: str) -> bool:
        """Whether this runner holds state for the given session_id.

        Default: True — assume external state exists (matches
        ClaudeRunner/CodexRunner which persist via CLI side-files).
        LocalHTTPRunner overrides to check its in-memory store.
        """
        return True

    def wants_auth_file(self) -> bool:
        """Whether worker should create /tmp/feishu_auth_*.json for this runner.

        Default: True — CLI runners need to read auth from env + file.
        LocalHTTPRunner overrides to return False (HTTP-only, no env inheritance).
        """
        return True
```

LocalHTTPRunner 额外 stub 三个 abstract 方法并附模块级注释说明契约边界（见 MEDIUM#2 缓解）。

## LocalHTTPRunner 接口

```python
class LocalHTTPRunner(BaseRunner):
    """Generic HTTP runner for local LLM endpoints.

    Stubbed abstract methods (never reached — run() is fully overridden):
      - build_args(): subprocess CLI args, N/A here
      - parse_streaming_line(): subprocess JSONL parsing, N/A
      - parse_blocking_output(): subprocess stdout parsing, N/A
    Assertion test in test_local_runner.py ensures these paths are never hit.
    """

    DEFAULT_MODEL = "gemma-4-26b"
    ALWAYS_STREAMING = False  # worker always passes on_output → effectively streams;
                              # blocking path kept for unit test simplicity

    def __init__(self, command, model, workspace, timeout,
                 max_budget_usd=None,
                 extra_system_prompts=None,
                 extra_cli_args=None,
                 fixed_env=None,
                 safety_prompt_mode="minimal",   # hard default for local
                 setting_sources=None,
                 *,
                 base_url: str,
                 protocol: str,                  # "anthropic" | "openai"
                 api_key: str = "",
                 max_tokens: int = 4096,
                 context_window: int = 8192,
                 openai_include_usage: bool = True,
                 model_aliases: Optional[dict[str, str]] = None):
        super().__init__(command, model, workspace, timeout, ...)
        self._base_url = base_url.rstrip("/")
        self._protocol = protocol
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._context_window = context_window
        self._model_aliases = model_aliases or {}
        self._adapter = _build_adapter(protocol, openai_include_usage)
        self._sessions = _SessionStore()

    # Stub abstracts (never reached)
    def build_args(self, *a, **kw): return []
    def parse_streaming_line(self, *a, **kw): pass
    def parse_blocking_output(self, *a, **kw) -> dict: return {}

    def get_model_aliases(self) -> dict[str, str]:
        return dict(self._model_aliases)

    def get_default_context_window(self) -> int:
        return self._context_window

    def get_display_name(self) -> str:
        return "Local LLM"

    def supports_compact(self) -> bool:
        return False

    # NEW hooks
    def has_session(self, session_id: str) -> bool:
        return self._sessions.exists(session_id)

    def wants_auth_file(self) -> bool:
        return False

    def cancel(self, tag: str) -> bool: ...   # HTTP cancel, see §Cancel
    def run(self, prompt, session_id=None, resume=False, tag=None,
            on_output=None, ...) -> dict: ...  # full override, see §Run
```

## Worker Integration

**Important — round 2 correction**: `process_message`, `_context_health_alert`,
`_write_auth_file` are **module-level functions** in `worker.py`, not methods. Runner
and session map are passed as parameters. Session key is the composite
`(bot_id, chat_id, thread_id)`. The inbound user message variable is `text`, not `prompt`.

### Stale session auto-heal (HIGH #1)

In `process_message` (worker.py:356). Real signature:
`process_message(item, bot_config, lark_client, session_map, runner, ...)`.

Insert after the existing sid lookup (before `runner.run(...)`):

```python
key = (bot_id, chat_id, thread_id)           # already computed at worker.py:387
existing_sid = session_map.get(key)
resume = bool(existing_sid)
sid = existing_sid or str(uuid.uuid4())

# NEW: if runner has no memory of this sid (e.g. process restart for local runner),
# demote to a fresh session and surface a visible notice.
stale_notice = None
if resume and not runner.has_session(sid):
    log.info("runner %s has no state for sid=%s — demoting resume=False",
             type(runner).__name__, sid[:8])
    resume = False
    stale_notice = "⚠️ 会话已重建（本地会话在 bridge 重启后未持久化）"

# `text` is the user prompt variable already set at worker.py:381.
result = runner.run(text, session_id=sid, resume=resume, tag=tag, ...)

if stale_notice and isinstance(result, dict) and not result.get("is_error"):
    result["result"] = stale_notice + "\n\n" + (result.get("result") or "")
```

SessionMap entry is untouched — the `(bot_id, chat_id, thread_id)` → sid mapping
survives; only the runner-side history restarts.

### AuthFile gate (HIGH #3)

Current code (worker.py:~470) unconditionally calls `_write_auth_file(chat_id, sender_id, user_token)`
and sets `FEISHU_AUTH_FILE` / `FEISHU_BOT_NAME` in env. Gate on runner capability:

```python
auth_file_path = None
env_extra = {}
if runner.wants_auth_file():
    auth_file_path = _write_auth_file(chat_id, sender_id, user_token)
    env_extra["FEISHU_AUTH_FILE"] = auth_file_path
    env_extra["FEISHU_BOT_NAME"] = bot_id
# else: LocalHTTPRunner doesn't need /tmp file, skip entirely.
```

Existing cleanup in the `finally` block (worker.py:920-923) already `os.unlink`s the
path when set; gating creation means `auth_file_path is None` and cleanup is a no-op.
No new cleanup code needed for claude/codex.

### Compact hint gating (MEDIUM #3)

`_context_health_alert(result, quota_snapshot=None)` at worker.py:244 is module-level and
has no runner reference. Add `runner` parameter and thread it through the one call site
in `process_message`:

```python
def _context_health_alert(result, quota_snapshot=None, runner=None) -> str | None:
    ...
    # Both 70% and 80% branches currently hardcode `/compact` hints.
    hint_compact = "`/compact` 压缩"
    hint_reset   = "`/agent reset` 开始新会话"
    tail = hint_compact if (runner is None or runner.supports_compact()) else hint_reset

    if pct >= 80:
        alert = f"🔴 Context {pct:.0f}% — 建议 `/new` 新会话或 {tail}"
        return "\n".join(filter(None, [alert, rate_alert]))
    if pct >= 70:
        alert = f"🟡 Context {pct:.0f}% — 可考虑 {tail}"
        return "\n".join(filter(None, [alert, rate_alert]))
```

Default `runner=None` preserves the `/compact` hint for existing call sites that
haven't been updated (defensive — the single call in `process_message` is updated
to pass `runner=runner`).

## 配置 Schema

### 推荐配置（user)

```json
{
  "agent": {
    "type": "local",
    "command": "local",                    // sentinel, skipped in PATH check
    "commands": {
      "claude": "/Users/feir/.local/bin/claude",
      "codex": "/opt/homebrew/bin/codex",
      "local": "local"
    },
    "providers": {
      "default": {                          // required even if unused when type=local
        "endpoint": {
          "base_url": "http://127.0.0.1:8000",
          "protocol": "anthropic"
        },
        "models": {"local": "gemma-4-26b"}
      },
      "omlx-local": {
        "endpoint": {
          "base_url": "http://127.0.0.1:8000",
          "protocol": "anthropic"
        },
        "models": {"local": "gemma-4-26b"},
        "max_tokens": 4096,
        "context_window": 8192
      },
      "ollama-local": {
        "endpoint": {
          "base_url": "http://127.0.0.1:11434",
          "protocol": "openai",
          "api_key": ""
        },
        "models": {"local": "qwen3:8b"},
        "openai_include_usage": false       // older ollama versions
      }
    }
  }
}
```

### Hard defaults for type=local (HIGH #2 — round 2 fix)

**Round-1 bug**: applying `setdefault` *after* `_normalize_prompt_config(fill_defaults=True)` is a no-op,
because that function already populates every missing key with the global defaults
(`feishu_cli=True, cron_mgr=True, safety="full"`). By the time `_apply_local_defaults`
runs, the keys exist.

**Round-2 fix**: bake the agent-type-aware defaults into `_normalize_prompt_config` itself,
which is the single normalization point used by `load_config` (main.py:422), `resolve_prompt_config`
(main.py:292-294), and `_normalize_provider_profiles` (main.py:222):

```python
# main.py:138 — replace existing signature
_LOCAL_PROMPT_DEFAULTS = {"safety": "minimal", "feishu_cli": False, "cron_mgr": False}
_GLOBAL_PROMPT_DEFAULTS = {"safety": "full", "feishu_cli": True, "cron_mgr": True}

def _normalize_prompt_config(
    prompt_cfg: object, *, fill_defaults: bool, agent_type: str | None = None
) -> dict[str, object]:
    raw = prompt_cfg if isinstance(prompt_cfg, dict) else {}
    base = _LOCAL_PROMPT_DEFAULTS if agent_type == "local" else _GLOBAL_PROMPT_DEFAULTS
    normalized: dict[str, object] = {}
    if fill_defaults or "safety" in raw:
        safety = str(raw.get("safety", base["safety"])).strip().lower()
        normalized["safety"] = safety if safety in {"full", "minimal", "off"} else base["safety"]
    if fill_defaults or "feishu_cli" in raw:
        normalized["feishu_cli"] = bool(raw.get("feishu_cli", base["feishu_cli"]))
    if fill_defaults or "cron_mgr" in raw:
        normalized["cron_mgr"] = bool(raw.get("cron_mgr", base["cron_mgr"]))
    if "setting_sources" in raw:
        normalized["setting_sources"] = str(raw["setting_sources"])
    return normalized
```

All existing call sites pass `agent_type=agent_cfg.get("type")`:

- `load_config` (main.py:422) — base config normalize
- `resolve_prompt_config` (main.py:292-294) — per-call resolution
- `_normalize_provider_profiles` (main.py:222) — provider overlay (`fill_defaults=False`
  so defaults only matter when user omits a key; agent_type still passed for consistency)

User explicit config (e.g. `"feishu_cli": true` under a local provider) wins, since
explicit values flow through the `raw.get(..., base[...])` path unchanged.

**Hot-swap coverage**: `switch_provider` (main.py:677) and `switch_agent` (main.py:724)
call `build_extra_prompts(next_cfg)`, which reads `next_cfg["prompt"]`. For `switch_agent`
the type is changing, so the stored normalized prompt from `load_config` is stale.
Add explicit re-normalize before `build_extra_prompts`:

```python
# switch_agent (main.py:~720), after next_cfg["type"] = agent_type:
next_cfg["prompt"] = _normalize_prompt_config(
    next_cfg.get("prompt"), fill_defaults=True, agent_type=agent_type
)
next_prompts = build_extra_prompts(next_cfg)

# switch_provider (main.py:~675), after provider set — no type change, but provider
# overlay may have injected prompt fields; reapply with current type to be safe:
next_cfg["prompt"] = _normalize_prompt_config(
    next_cfg.get("prompt"), fill_defaults=True, agent_type=next_cfg.get("type")
)
next_prompts = build_extra_prompts(next_cfg)
```

This replaces the round-1 `_apply_local_defaults` helper entirely.

### Endpoint normalization

`_normalize_provider_profiles` extended to recognize `endpoint` sub-object:

```python
def _normalize_endpoint_config(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {}
    base_url = str(raw.get("base_url", "")).strip().rstrip("/")
    protocol = str(raw.get("protocol", "anthropic")).strip().lower()
    if protocol not in ("anthropic", "openai"):
        raise ConfigError(f"endpoint.protocol must be 'anthropic' or 'openai', got {protocol}")
    return {
        "base_url": base_url,
        "protocol": protocol,
        "api_key": str(raw.get("api_key", "")),
    }
```

## Command resolver (HIGH #4 — round 2 reconciled)

**Round-2 reconciliation**: single source of truth is `resolve_effective_agent_command`.
`resolve_agent_command` demoted to raw lookup (no `shutil.which` bypass needed — callers
must go through the effective resolver):

```python
# main.py:262 — add local bypass BEFORE any shutil.which lookup
def resolve_effective_agent_command(agent_cfg, agent_type):
    if agent_type == "local":
        # Sentinel — never executed as subprocess.
        configured = (_provider_profile(agent_cfg).get("commands", {}).get("local")
                      or _normalize_agent_commands(agent_cfg).get("local")
                      or "local")
        return configured, configured
    configured = _provider_profile(agent_cfg).get("commands", {}).get(agent_type)
    if configured:
        return shutil.which(configured), configured
    return resolve_agent_command(agent_cfg, agent_type)
```

Call sites audited (grep `shutil.which` + `resolve_agent_command`):

| File:Line | Site | Action |
|-----------|------|--------|
| `main.py:262` | `resolve_effective_agent_command` | **add local bypass here (source of truth)** |
| `main.py:422` | `load_config` PATH check | already calls effective resolver — inherits bypass |
| `main.py:667` | `switch_provider` | ensure it calls effective resolver (refactor if it calls raw `shutil.which` directly) |
| `main.py:712` | `switch_agent` | same |
| `main.py:242` | `resolve_agent_command` (raw) | **no change** — internal helper, only reached via effective resolver |

Implementation note: during task T3.3, grep for other `shutil.which(` occurrences in
main.py and route each through `resolve_effective_agent_command`.

## 协议适配层

### AnthropicAdapter

**Request** (POST `{base_url}/v1/messages`)：
```json
{
  "model": "gemma-4-26b",
  "max_tokens": 4096,
  "system": "<safety prompt>",
  "messages": [{"role": "user", "content": "..."}],
  "stream": true
}
```

**Headers**: `x-api-key: <key>` (if set), `anthropic-version: 2023-06-01`, `Content-Type: application/json`

**Streaming SSE 事件**:
- `message_start` → input_tokens
- `content_block_delta` (type=text_delta) → append text
- `message_delta` → output_tokens, stop_reason
- `message_stop` → done

**Blocking** response: `{content: [{text}], usage: {input_tokens, output_tokens}, stop_reason}`

### SSE parser spec (MEDIUM — round 2)

Single assembler used by both adapters. Reads line-by-line from `http.client.HTTPResponse`
(iterated via `_sse_iter(response)`):

1. Strip trailing `\r\n` or `\n` (handle CRLF and LF).
2. **Empty line** → dispatch current buffered event: yield
   `(event_type or "message", "\n".join(data_lines))`; then clear buffers. This is the
   SSE frame boundary per W3C spec.
3. **Line starts with `:`** → comment/heartbeat; ignore.
4. **Line starts with `event:`** → set `event_type = line[6:].lstrip()`.
5. **Line starts with `data:`** → append `line[5:].lstrip(" ")` to `data_lines`
   (lstrip single space only, per spec; multi-line `data:` concatenated with `\n`).
6. **Line starts with `id:` / `retry:`** → ignore (we don't reconnect).
7. **Sentinel**: if assembled data payload equals `[DONE]` (OpenAI), yield done and stop.
8. **Malformed JSON inside `data:`** → log at DEBUG, skip frame, keep reading.
9. **Partial frame at EOF** (stream ended mid-event, no blank line) → discard buffer,
   finalize with whatever text has already been emitted; return with `stop_reason="incomplete"`.
10. **Connection closed** mid-stream → treat as EOF; combine with partial-frame rule.

Yielded events are consumed by the per-protocol adapter to extract `text_delta`, `usage`,
and terminal state. The assembler itself is protocol-agnostic.

### OpenAIAdapter

**Request** (POST `{base_url}/v1/chat/completions`)：
```json
{
  "model": "qwen3:8b",
  "max_tokens": 4096,
  "messages": [
    {"role": "system", "content": "<safety prompt>"},
    {"role": "user", "content": "..."}
  ],
  "stream": true,
  "stream_options": {"include_usage": true}   // conditional — see below
}
```

**Headers**: `Authorization: Bearer <key>` (if api_key), `Content-Type: application/json`

**Streaming SSE**:
- `data: {"choices":[{"delta":{"content":"..."}}]}` → append text
- `data: {"usage":{"prompt_tokens":N,"completion_tokens":M}}` → usage
- `data: [DONE]` → done

**Compatibility fallback (MEDIUM #5)**:
- `openai_include_usage=False` → omit `stream_options` from request entirely
- On HTTP 400 with first attempt → retry once without `stream_options`
- Missing usage → report `input_tokens=0, output_tokens=len(text)//4` (rough estimate) with `usage_estimated=True` flag in result

## Session 内存管理

```python
class _SessionStore:
    """In-memory message history. Process-local, LRU-evicting."""

    # Rationale: single-user bridge; 100 chats × 50 messages × ~500 tokens avg
    # ≈ 2.5M tokens in worst-case memory. Fits gemma-4-26b 8192-ctx window
    # per session (25 exchanges ≈ typical ceiling before context overflow).
    MAX_SESSIONS = 100
    MAX_MESSAGES_PER_SESSION = 50

    def __init__(self):
        self._sessions: OrderedDict[str, list[dict]] = OrderedDict()
        self._lock = threading.Lock()

    def exists(self, session_id: str) -> bool:
        """Used by LocalHTTPRunner.has_session for worker auto-heal."""
        with self._lock:
            return session_id in self._sessions

    def get(self, session_id: str) -> list[dict]:
        with self._lock:
            msgs = self._sessions.get(session_id)
            if msgs is not None:
                self._sessions.move_to_end(session_id)
            return list(msgs) if msgs else []

    def append(self, session_id: str, role: str, content: str) -> None: ...
    def reset(self, session_id: str) -> None: ...
```

Session ID generation: **caller must provide**. `run()` raises `ValueError` on missing sid (worker always supplies uuid4, so this is a guardrail for tests/misuse). Fallback removed per evaluation LOW#4.

`run()` session flow:
- `resume=False` → `self._sessions.reset(sid)`; history = `[user_msg]`
- `resume=True` → history = `self._sessions.get(sid) + [user_msg]`
- Success → `append(sid, "user", prompt); append(sid, "assistant", response)`
- Failure → no append (history unchanged)

## Cancel / Timeout

```python
class _HTTPCall:
    def __init__(self, url, headers, body, socket_timeout, wall_clock_timeout):
        self.url = url
        self.headers = headers
        self.body = body
        self.socket_timeout = socket_timeout       # per-read idle
        self.wall_clock_timeout = wall_clock_timeout  # total streaming budget
        self._cancel_event = threading.Event()
        self._response: Optional[http.client.HTTPResponse] = None
        self._t0 = time.monotonic()

    def cancel(self):
        self._cancel_event.set()
        if self._response:
            try: self._response.close()
            except Exception: pass

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def wall_clock_exceeded(self) -> bool:
        return (time.monotonic() - self._t0) > self.wall_clock_timeout
```

`LocalHTTPRunner.cancel(tag)` override: look up active `_HTTPCall` via `self._active[tag]`, call `.cancel()`. Returns True if a call was found and cancelled.

### Cancel registration spec (MEDIUM — round 2)

`LocalHTTPRunner` holds `self._active: dict[str, _HTTPCall]` guarded by `self._active_lock`.
Lifecycle:

1. `run(..., tag=tag)` constructs `_HTTPCall` **before** `urlopen`.
2. Under `_active_lock`: `self._active[tag] = call`. This must happen *before* the blocking
   `urlopen` call so that a concurrent `cancel(tag)` from the worker can observe it.
3. `urlopen(..., timeout=socket_timeout)`; assign `call._response = response`.
4. Iterate SSE. Each loop tick checks `call.is_cancelled()` and `call.wall_clock_exceeded()`.
5. `finally`: under `_active_lock`, `self._active.pop(tag, None)`; close response.

`socket_timeout` capped at **≤ 2s** so that a cancel issued during a long idle read
still takes effect within that window (urllib's `read()` blocks until socket timeout
fires; shorter timeout = tighter cancel latency). Cancel between bytes is immediate
via `response.close()` racing the reader thread.

Race: if `cancel(tag)` arrives *before* step 2, it's a no-op lookup. Acceptable —
the worker's cancel button is user-driven, and the window is microseconds (request
construction). No locking needed on the worker side beyond the dict lookup.

Streaming loop:
```python
for line in self._sse_iter(response):
    if call.is_cancelled():
        return {"result": "任务已取消。", "session_id": sid, "is_error": False, "cancelled": True}
    if call.wall_clock_exceeded():
        call.cancel()
        return {"result": f"Local LLM 总时长超限（{self.timeout}s）", ...}
    # ... parse event, append to state, emit on_output ...
```

**Documented limitation**: HTTP cancel closes client socket; upstream omlx/ollama may continue generating until its internal done event. For single-user local GPU this wastes compute, not user experience. For shared servers document separately. Future: abort token protocol if/when providers support it.

## main.py 改动 (consolidated)

```python
# Line 132 — register
_RUNNER_CLASSES = {"claude": ClaudeRunner, "codex": CodexRunner, "local": LocalHTTPRunner}

# resolve_effective_agent_command — see §Command resolver for full snippet (single
# bypass for agent_type == "local").

# Hard default for local type — handled inside _normalize_prompt_config via the new
# agent_type parameter; no separate _apply_local_defaults helper. See §Hard defaults.

# create_runner — dispatch endpoint config
def create_runner(agent_cfg, bot_cfg, extra_prompts):
    agent_type = agent_cfg["type"]
    runner_cls = _RUNNER_CLASSES[agent_type]
    ...
    kwargs = dict(command=..., model=..., workspace=..., timeout=..., ...)
    if agent_type == "local":
        profile = _provider_profile(agent_cfg)
        endpoint = profile.get("endpoint", {})
        kwargs.update(
            base_url=endpoint.get("base_url", "http://127.0.0.1:8000"),
            protocol=endpoint.get("protocol", "anthropic"),
            api_key=endpoint.get("api_key", ""),
            max_tokens=profile.get("max_tokens", 4096),
            context_window=profile.get("context_window", 8192),
            openai_include_usage=profile.get("openai_include_usage", True),
            model_aliases=profile.get("model_aliases", {}),
        )
    return runner_cls(**kwargs)
```

The agent-type-aware `_normalize_prompt_config` is called in:
- `load_config` (main.py:422) — pass `agent_type=agent_cfg.get("type")`
- `switch_provider` (~main.py:676) — re-normalize `next_cfg["prompt"]` with current type
- `switch_agent` (~main.py:723) — re-normalize `next_cfg["prompt"]` with new type
- `resolve_prompt_config` (main.py:292-294) — pass `agent_type=agent_cfg.get("type")`
- `_normalize_provider_profiles` (main.py:222) — pass type from outer `agent_cfg`

## commands.py 改动 (LOW #1)

```python
# /agent — derive options from registry
supported = " / ".join(sorted(_RUNNER_CLASSES))   # instead of hardcoded "claude / codex"

# /status — gate Claude-specific quota section
if isinstance(self.runner, ClaudeRunner):
    sections.append(self._claude_quota_section())

# /model — skip empty aliases
aliases = self.runner.get_model_aliases()
if aliases:
    lines.append(f"可选: {' / '.join(sorted(aliases))}")
```

## 测试策略

### Unit `tests/unit/test_local_runner.py`

1. **Adapter 协议 (4 cases)**
   - `AnthropicAdapter.build_request` 字段完整
   - `OpenAIAdapter.build_request` with/without `stream_options`
   - SSE 解析（Anthropic streaming、OpenAI streaming）
   - SSE 中途断流、malformed JSON、空响应

2. **Session store (4 cases)**
   - `exists()` true/false
   - `resume=False` 清零
   - `resume=True` 累积
   - LRU + 消息数上限截断
   - 失败不污染历史

3. **Cancel / Timeout (3 cases)**
   - `cancel(tag)` 触发 `_HTTPCall.cancel()`
   - Socket idle timeout 返回 error
   - Wall-clock timeout 触发 cancel + error

4. **End-to-end mock HTTP (4 cases)**
   - Anthropic streaming: 解析 input/output tokens，result.text 匹配
   - OpenAI blocking: result 字段
   - HTTP 503 → is_error=True，友好错误
   - OpenAI 400 "unknown stream_options" → 自动重试无此字段

5. **BaseRunner contract assertion (1 case)**
   - `LocalHTTPRunner.build_args/parse_streaming_line/parse_blocking_output` 永不被执行（pytest monkeypatch 或直接反射）

### Integration `tests/unit/test_bridge.py` (5 new cases, MEDIUM #9)

1. `test_load_config_local_type` — agent.type=local，command=local 不报 "not found"，prompt 硬默认生效
2. `test_switch_agent_to_local` — `/agent local` hot swap 成功，runner 实例变更
3. `test_switch_provider_to_local_endpoint` — `/provider omlx-local` 正确传递 endpoint 到 LocalHTTPRunner
4. `test_process_message_stale_local_sid` — SessionMap 有旧 sid 但 runner 内存空 → worker 检测到 has_session=False，resume 降级，用户收到 stale_notice
5. `test_cost_store_no_ops_for_local` — local runner 返回无 `total_cost_usd`，cost store 不 crash

### 手动集成

1. 用户配 `omlx-local` provider，`/agent local` + `/provider omlx-local`
2. "hi" → 延迟 ≤ 2s，omlx 日志 input_tokens ≤ 100
3. 多条测 session 累积
4. bridge 重启后再发 → 看到 `⚠️ 会话已重建` 提示
5. 长 prompt 测飞书取消按钮
6. omlx 停服 → 发消息 → 收到 endpoint 不可达错误
7. 切回 `/agent claude` 无回归

### 不测

- 真实模型推理质量（超出 runner 范畴）
- CI 不依赖外部 omlx / ollama 服务

## 验证标准

| 指标 | 现状 | 目标 |
|------|------|------|
| 单条消息 input_tokens | 25,900 | ≤ 200 |
| 端到端延迟 ("hi" @ gemma-4-26b) | ~100s | ≤ 2s |
| 单元测试覆盖率（local runner 模块） | N/A | ≥ 85% |
| 现有 claude/codex 行为 | 正常 | 无回归 |
| Stale session 自愈提示 | N/A | 用户可见 ⚠️ 提示 |
| Prompt 注入 token 数（type=local 默认） | ~3K (feishu-cli + cron-mgr) | 0 |
