# Design: pi-runner

## Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│                         Feishu Bridge                           │
│                                                                 │
│  worker.py ── BaseRunner.run(prompt, session_id, callbacks) ─┐  │
│                                                              │  │
│  commands.py: /agent /provider /model /status /stop          │  │
│  ui.py: cards, footer, usage, tool status                    │  │
└──────────────────────────────────────────────────────────────┼──┘
                                                               │
                                                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                           PiRunner                              │
│                                                                 │
│  command: pi                                                     │
│  mode: --mode json or --mode rpc                                │
│  cwd: configured workspace                                      │
│  default tools: read,grep,find,ls                               │
│  session id: Pi session path/id mapped by bridge SessionMap      │
│  output: Pi events -> RunResult + streaming callbacks            │
└──────────────────────────────────────────────────────────────┬──┘
                                                               │
                                                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Pi Agent Runtime                          │
│                                                                 │
│  Loads AGENTS.md / CLAUDE.md context files                      │
│  Maintains sessions and compaction                              │
│  Executes allowed tools                                         │
│  Loads custom provider extension/config                         │
└──────────────────────────────────────────────────────────────┬──┘
                                                               │
                                                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                            oMLX                                 │
│                                                                 │
│  OpenAI-compatible endpoint                                     │
│  Models: local-main, local-qwen, local-light                    │
└─────────────────────────────────────────────────────────────────┘
```

## Runner Contract

`PiRunner` must conform to the existing `BaseRunner` contract:

```python
class PiRunner(BaseRunner):
    DEFAULT_MODEL = "Qwen3.6-35B-A3B-mxfp4"
    ALWAYS_STREAMING = True

    def build_args(...)
    def parse_streaming_line(...)
    def parse_blocking_output(...)
    def get_model_aliases(...)
    def get_default_context_window(...)
    def supports_compact(...)
```

Implementation guidance:

- Prefer `ALWAYS_STREAMING=True`, because Pi session and tool events are event-oriented.
- Use existing `RunResult` fields; do not introduce Pi-specific return shapes into `worker.py`.
- Keep `worker.py` runner-agnostic. Any Pi event translation belongs in `PiRunner`.
- Keep provider-specific oMLX setup outside bridge core where possible; use Pi config/extension.

## Integration Mode Decision

Evaluate both Pi integration modes before implementation:

| Mode | Pros | Cons | Decision Gate |
|------|------|------|---------------|
| `pi --mode json` | Similar to existing Claude/Codex JSONL runner; easier to parse as subprocess stdout | May be less interactive for session operations | Choose if events include enough session/tool/output data |
| `pi --mode rpc` | Designed for process integration; stable command channel | Requires request/response client and lifecycle management | Choose if JSON mode cannot resume/session-control reliably |
| SDK (`@mariozechner/pi-coding-agent`) | Deepest integration, direct API | Requires Node service or Python<->Node bridge | Defer unless CLI modes fail |

MVP default: **start with `pi --mode json`**, switch to RPC only if JSON mode lacks required session control.

## Local Pi Source Baseline

Phase 0 uses the local checkout:

```text
/Users/feir/projects/pi-mono
```

Observed state on 2026-04-18:

```text
which pi                                      -> not found
/Users/feir/projects/pi-mono/node_modules/.bin/tsx -> present after npm install
/Users/feir/projects/pi-mono/packages/coding-agent/dist/cli.js -> present after package builds
node /Users/feir/projects/pi-mono/packages/coding-agent/dist/cli.js --version -> 0.67.68
```

Setup commands:

```bash
cd /Users/feir/projects/pi-mono
npm install
npm run build
```

Current build note:

- Root `npm run build` failed with `cd: packages/tui: No such file or directory`.
- Running the package builds individually in the root script order succeeded.
- Treat this as a Phase 0 npm lifecycle anomaly to investigate only if it blocks repeatable setup.

Source-run command after dependencies are installed:

```bash
/Users/feir/projects/pi-mono/pi-test.sh --mode json -p "Reply with exactly: PI_JSON_OK"
```

Global CLI command after build/link or npm global install:

```bash
pi --mode json -p "Reply with exactly: PI_JSON_OK"
```

## Phase 0 Findings

Pi can be connected to the local oMLX LaunchAgent through `~/.pi/agent/models.json`.

Local provider config:

```text
/Users/feir/.pi/agent/models.json
```

Provider shape:

```jsonc
{
  "providers": {
    "omlx": {
      "baseUrl": "http://127.0.0.1:8000/v1",
      "api": "openai-completions",
      "apiKey": "!jq -r '.auth.api_key' /Users/feir/.omlx/settings.json",
      "compat": {
        "supportsDeveloperRole": false,
        "supportsReasoningEffort": false
      },
      "models": [
        { "id": "gemma-4-26b-a4b-it-mxfp4" },
        { "id": "qwen-3.5-9b" },
        { "id": "Qwen3.6-35B-A3B-mxfp4" }
      ]
    }
  }
}
```

Runtime evidence:

| Artifact | Result |
|----------|--------|
| `event-samples/json-smoke.jsonl` | JSON mode returns `PI_JSON_OK`; usage input=369 output=3 |
| `event-samples/rpc-smoke.jsonl` | RPC mode returns `PI_RPC_OK`; keep stdin open until `agent_end` |
| `event-samples/context-smoke.jsonl` | From `/Users/feir/.claude`, Pi answers `Facts Before Words`, confirming `CLAUDE.md` context loading |
| `event-samples/readonly-tool-smoke.jsonl` | `ls` tool emits `toolcall_*`, `tool_execution_start`, `tool_execution_end`, and `toolResult` events |
| `event-samples/provider-error.jsonl` | Invalid model produces `stopReason="error"` with `errorMessage`; process exit code remains 0 |
| `event-samples/missing-file-tool-error.jsonl` | Missing-file `read` emits tool `isError=true`; final turn can still be normal |
| `event-samples/rpc-abort.jsonl` | RPC abort produces `stopReason="aborted"` plus command response success |

JSON mode event facts:

- starts with `session`, `agent_start`, `turn_start`
- user messages emit `message_start` and `message_end`
- assistant text emits `message_update` with `assistantMessageEvent.type` values:
  - `text_start`
  - `text_delta`
  - `text_end`
- final assistant message emits `message_end`, `turn_end`, `agent_end`
- usage is zero during deltas and populated on `text_end` / `message_end` / `turn_end`

Tool event facts:

- tool proposal emits assistant `message_update` events:
  - `toolcall_start`
  - `toolcall_delta`
  - `toolcall_end`
- execution emits top-level events:
  - `tool_execution_start`
  - `tool_execution_end`
- tool output is also represented as a `toolResult` message.

## Configuration

### Agent Type

Add `"pi"` to `_RUNNER_CLASSES`:

```python
_RUNNER_CLASSES = {
    "claude": ClaudeRunner,
    "codex": CodexRunner,
    "local": LocalHTTPRunner,
    "pi": PiRunner,
}
```

### Provider Profile

PiRunner should reuse existing provider profile fields:

- `workspace`
- `models.pi`
- `args_by_type.pi`
- `env_by_type.pi`
- `prompt` only for bridge-managed safety fragments; Pi context files remain Pi-owned

Example:

```jsonc
"agent": {
  "type": "pi",
  "command": "pi",
  "provider": "pi-local",
  "providers": {
    "pi-local": {
      "workspace": "/Users/feir/.claude",
      "models": {
        "pi": "Qwen3.6-35B-A3B-mxfp4"
      },
      "args_by_type": {
        "pi": [
          "--provider", "omlx",
          "--no-context-files",
          "--no-extensions",
          "--no-skills",
          "--no-prompt-templates",
          "--no-themes"
        ]
      }
    }
  }
}
```

## Phase 1 Implementation Notes

Implemented MVP behavior:

- `PiRunner` lives in `feishu_bridge/runtime_pi.py`.
- The runner always uses `pi --mode json` through `BaseRunner` streaming.
- The bridge keeps its own `session_id`; Pi receives a deterministic session file path under `<workspace>/state/feishu-bridge/pi-sessions/<session_id>.jsonl`.
- Unless config explicitly supplies `--tools` or `--no-tools`, PiRunner injects read-only tools: `read,grep,find,ls`.
- Text is accumulated from `message_update.assistantMessageEvent.type="text_delta"` and finalized from `turn_end.message.content`.
- Usage is normalized from Pi `input`, `output`, `cacheRead`, `cacheWrite` into bridge usage keys.
- `stopReason="error"` and `stopReason="aborted"` map to `is_error=True`.
- `wants_auth_file()` is false and `supports_compact()` is false for now.

Verification:

```bash
python3 -m pytest tests/unit/test_pi_runner.py
python3 -m pytest \
  tests/unit/test_bridge.py::test_create_runner_claude \
  tests/unit/test_bridge.py::test_create_runner_codex \
  tests/unit/test_bridge.py::test_create_runner_local_builds_http_runner \
  tests/unit/test_bridge.py::test_load_config_local_type \
  tests/unit/test_pi_runner.py
```

Real smoke through local oMLX:

```bash
pi --mode json --provider omlx --model Qwen3.6-35B-A3B-mxfp4 \
  --no-tools --no-context-files --no-extensions --no-skills \
  --no-prompt-templates --no-themes \
  --session /tmp/feishu-bridge-pi-runner-smoke-escalated.jsonl \
  -p "Reply with exactly: PI_RUNNER_SMOKE_OK"
```

Result: final assistant text `PI_RUNNER_SMOKE_OK`, usage input=364 output=7.

Actual `pi-local` config added on 2026-04-19:

- Config: `/Users/feir/.config/feishu-bridge/config.json`
- Backup: `/Users/feir/.config/feishu-bridge/config.json.bak-pi-runner-20260419005420`
- `commands.pi`: `/Users/feir/.local/bin/pi`
- `providers.pi-local.workspace`: `/Users/feir/.claude`
- `providers.pi-local.models.pi`: `Qwen3.6-35B-A3B-mxfp4`
- `providers.pi-local.args_by_type.pi`: `--provider omlx --no-context-files --no-extensions --no-skills --no-prompt-templates --no-themes`
- Direct smoke result: `PI_LOCAL_CONFIG_OK`, usage input=1370 output=4

## Context Policy

Pi should own context-file loading. Bridge must not duplicate Pi context injection.

Recommended file layout:

```text
~/.pi/agent/AGENTS.md              # global compact rules for Pi
/Users/feir/.claude/.pi/APPEND_SYSTEM.md
/Users/feir/.claude/CLAUDE.md      # loaded only if Pi supports CLAUDE.md from cwd chain
```

Rules:

- Keep always-on Pi context under **12 KB chars** unless benchmarked.
- Do not load `/Users/feir/.claude/rules/lessons.md` by default.
- Load task-specific lessons through a future retrieval extension, not in PiRunner MVP.
- Log the context files reported by Pi startup during Discovery; document actual behavior.

## Tool Policy

Default PiRunner tools:

```text
read,grep,find,ls
```

Disallowed by default:

```text
bash,write,edit
```

Opt-in plan:

1. Add config field `agent.pi.allow_write_tools: false` or reuse `args_by_type.pi`.
2. Require explicit config for `write/edit/bash`.
3. For `bash`, require a bridge-side denylist at minimum:
   - no restart/stop/reload of feishu-bridge
   - no destructive git reset/checkout
   - no publish/deploy commands without confirmation
4. Add smoke tests for each opt-in mode.

## Event Mapping

Discovery must capture real Pi JSON/RPC events and map them to this table:

| Pi Event | Bridge Output |
|----------|---------------|
| assistant text delta or final text | `on_output(text)` and `RunResult.result` |
| tool call started | `on_tool_status(name, running)` |
| tool call completed | `on_tool_status(name, completed)` |
| error | `RunResult.is_error=True`, result contains user-safe message |
| usage/context | `RunResult.usage`, `peak_context_tokens` if available |
| session started/resumed | `RunResult.session_id` |

If Pi emits only final assistant text in MVP, bridge should still return a correct final card and mark tool-status integration as Phase 3.

## Session Semantics

Expected mapping:

```text
Feishu chat/thread key -> bridge SessionMap -> Pi session id/path
```

Required behaviors:

- `/new`: clear bridge mapping and start a fresh Pi session.
- Resume: pass mapped Pi session id/path to Pi.
- Bridge restart: reload SessionMap and continue if Pi session still exists.
- Missing Pi session: create a fresh session and update SessionMap.
- Provider switch: use `session_identity(agent_cfg)` so `pi:pi-local` does not reuse incompatible sessions.

## Commands

| Command | PiRunner behavior |
|---------|-------------------|
| `/agent pi` | Switch runner to Pi; clear incompatible sessions |
| `/provider pi-local` | Switch Pi provider profile; rebuild runner |
| `/model` | Show Pi model aliases from config; do not assume Claude/Codex model names |
| `/status` | Include runner display name, workspace, provider, model, tool mode |
| `/stop` | Terminate active Pi process or send RPC cancel if using RPC |
| `/compact` | If Pi CLI supports compact command via integration, expose it; otherwise return unsupported |
| `/cost` | Local model cost is zero/unknown; display token usage if Pi provides it |

## Observability

Log these fields per turn:

- runner type: `pi`
- provider profile
- cwd/workspace
- command argv with secrets redacted
- Pi mode: `json` or `rpc`
- session id/path
- model
- enabled tools
- first token latency / total latency
- input/output tokens if available
- context files loaded if Pi reports them

## Validation Matrix

| Test | Command / Method | Expected |
|------|------------------|----------|
| Pi source checkout | `test -d /Users/feir/projects/pi-mono` | exists |
| Pi installed | `which pi` | non-empty, or use source runner path |
| Pi dependencies | `test -x /Users/feir/projects/pi-mono/node_modules/.bin/tsx` | present after `npm install` |
| Pi build output | `test -x /Users/feir/projects/pi-mono/packages/coding-agent/dist/cli.js` | present after `npm run build` |
| Pi version | `pi --version` | exits 0 |
| oMLX provider | `pi --list-models omlx` | lists local model or documents config gap |
| JSON mode | `pi --mode json -p "say hi"` | emits parseable JSONL |
| Context loading | run from `/Users/feir/.claude` | Pi reports/uses expected `CLAUDE.md` or `AGENTS.md` |
| Read-only tools | ask "list files" | tool succeeds without bash/write/edit |
| Forbidden write | ask to edit a file | MVP refuses or lacks write tool |
| Bridge smoke | send Feishu message | card completes with Pi response |
| Restart resume | restart bridge then continue chat | session resumes or fresh fallback is explicit |

## Rollback

- Existing `claude`, `codex`, and `local` runners remain unchanged.
- PiRunner is gated by `agent.type="pi"`.
- If PiRunner fails in production, switch `/agent claude` or edit config back to existing runner.
- Do not migrate session files in-place; use runner identity namespace to avoid cross-runner reuse.

## Self-check

- Completeness: Proposal, architecture, config, safety, session, commands, validation, rollback covered.
- Consistency: Pi is treated as agent runner; oMLX is treated as inference provider.
- Executability: Tasks document defines phased commands and acceptance checks.
- Auditability: Plan records start SHA and references concrete files/config fields.
