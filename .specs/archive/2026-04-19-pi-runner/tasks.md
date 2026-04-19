# Tasks: pi-runner

## Phase 0 — Discovery and Evidence

- [x] 0.1 Locate Pi source checkout
  - Path: `/Users/feir/projects/pi-mono`
  - Evidence: directory contains `README.md`, `package.json`, `AGENTS.md`, `pi-test.sh`, `packages/coding-agent/package.json`

- [x] 0.2 Install/build Pi CLI entrypoint
  - Command: `which pi`
  - Current result: `pi not found`
  - Source runner: `/Users/feir/projects/pi-mono/pi-test.sh`
  - Initial blockers:
    - `/Users/feir/projects/pi-mono/node_modules/.bin/tsx` missing
    - `/Users/feir/projects/pi-mono/packages/coding-agent/dist/cli.js` missing
  - Setup:
    ```bash
    cd /Users/feir/projects/pi-mono
    npm install
    npm run build
    ```
  - Result:
    - `npm install` PASS: 529 packages installed, npm audit found 0 vulnerabilities
    - root `npm run build` FAIL: npm lifecycle reported `cd: packages/tui: No such file or directory` even though the directory exists
    - package-by-package build PASS in root script order: `tui`, `ai`, `agent`, `coding-agent`, `mom`, `web-ui`, `pods`
    - `/Users/feir/projects/pi-mono/node_modules/.bin/tsx` present
    - `/Users/feir/projects/pi-mono/packages/coding-agent/dist/cli.js` present and executable
    - pi-mono worktree changed `packages/ai/src/models.generated.ts` from model generation: `google/gemini-2.0-flash-lite-001.contextWindow` `1000000 -> 1048576`
  - PASS: built dist CLI is runnable via `node /Users/feir/projects/pi-mono/packages/coding-agent/dist/cli.js`

- [x] 0.3 Capture Pi version and docs baseline
  - Command: `pi --version`
  - Source fallback: `/Users/feir/projects/pi-mono/pi-test.sh --version`
  - Result:
    - `pi-test.sh --version` under sandbox FAIL: `tsx` IPC pipe `listen EPERM`
    - Built dist command PASS: `node /Users/feir/projects/pi-mono/packages/coding-agent/dist/cli.js --version`
  - Version: `0.67.68`

- [x] 0.4 Verify Pi JSON mode
  - Command: `pi --mode json -p "Reply with exactly: PI_JSON_OK"`
  - Source fallback: `/Users/feir/projects/pi-mono/pi-test.sh --mode json -p "Reply with exactly: PI_JSON_OK"`
  - PASS: stdout is JSONL and contains final assistant text
  - Artifact: `.specs/changes/pi-runner/event-samples/json-smoke.jsonl`
  - Result:
    - Provider: `omlx`
    - Model: `qwen-3.5-9b`
    - Flags: `--no-tools --no-context-files --no-extensions --no-skills --no-prompt-templates --no-themes --no-session`
    - JSONL lines: 14
    - Final text: `PI_JSON_OK`
    - Final usage: input=369, output=3, totalTokens=372

- [x] 0.5 Verify Pi RPC mode
  - Command: `pi --mode rpc`
  - Source fallback: `/Users/feir/projects/pi-mono/pi-test.sh --mode rpc`
  - PASS: protocol can initialize and send one prompt
  - Artifact: `.specs/changes/pi-runner/event-samples/rpc-smoke.jsonl`
  - Result:
    - Input command: `{"id":"req-1","type":"prompt","message":"Reply with exactly: PI_RPC_OK"}`
    - Must keep stdin open until assistant turn finishes; closing stdin immediately only captures prompt acceptance/user-message events
    - JSONL lines: 14
    - Final text: `PI_RPC_OK`
    - Final usage: input=369, output=3, totalTokens=372
  - Decision: JSON mode is sufficient for MVP; RPC remains viable if we need long-lived process control

- [x] 0.6 Verify context file loading
  - Run from: `/Users/feir/.claude`
  - PASS: Pi startup/logs show expected `AGENTS.md` or `CLAUDE.md` files, or behavior is documented
  - Artifact: `.specs/changes/pi-runner/event-samples/context-smoke.jsonl`
  - Result:
    - Prompt asked: "what is core principle number 2 named?"
    - Final text: `Facts Before Words`
    - Evidence: phrase exists in `/Users/feir/.claude/CLAUDE.md`
    - Usage increased to input=1015 vs no-context smoke input=369, confirming context file injection
  - Decision: Pi context discovery works for `CLAUDE.md`; still create compact Pi global context before production

- [x] 0.7 Verify oMLX provider integration
  - Add Pi custom provider or models config for `http://127.0.0.1:8000/v1`
  - Command: `pi --provider omlx --model local-main -p "say local ok"`
  - Source fallback: `/Users/feir/projects/pi-mono/pi-test.sh --provider omlx --model local-main -p "say local ok"`
  - PASS: response comes from oMLX endpoint
  - Config file: `/Users/feir/.pi/agent/models.json`
  - Auth: config references `OMLX_API_KEY`; smoke commands populate it from `/Users/feir/.omlx/settings.json`
  - oMLX LaunchAgent:
    - label: `com.omlx.server`
    - state: running
    - port: `127.0.0.1:8000`
    - model dir: `/Users/feir/models/mlx-community`
    - discovered models: 5
  - Pi-visible models:
    - `gemma-4-26b-a4b-it-mxfp4`
    - `qwen-3.5-9b`
    - `Qwen3.6-35B-A3B-mxfp4`
  - PASS: JSON/RPC smoke tests completed through provider `omlx`

- [x] 0.8 Capture event schema samples
  - Scenarios: plain reply, read tool, missing file, model error, cancel
  - Artifact: `.specs/changes/pi-runner/event-samples/`
  - Captured:
    - `json-smoke.jsonl`: plain JSON mode reply
    - `rpc-smoke.jsonl`: plain RPC mode reply
    - `context-smoke.jsonl`: `CLAUDE.md` context loading
    - `readonly-tool-smoke.jsonl`: `ls` tool call with `tool_execution_start` / `tool_execution_end`
    - `provider-error.jsonl`: invalid model emits `turn_end` with `stopReason="error"` and process exit code 0
    - `missing-file-tool-error.jsonl`: failed `read` tool emits `tool_execution_end.result.isError=true`; final assistant turn remains normal unless Pi itself fails
    - `rpc-abort.jsonl`: RPC abort emits assistant `stopReason="aborted"` and an RPC response success line

## Phase 1 — PiRunner Skeleton

- [x] 1.1 Add `PiRunner` module or class
  - File: `feishu_bridge/runtime_pi.py`
  - Requirement: subclass `BaseRunner`

- [x] 1.2 Register runner type
  - File: `feishu_bridge/main.py`
  - Add `_RUNNER_CLASSES["pi"] = PiRunner`
  - PASS: `agent.type="pi"` loads without config error

- [x] 1.3 Build command argv
  - Include workspace cwd
  - Include provider/model args from `resolve_agent_args()`
  - Include safe default tools if config does not override
  - PASS: unit test asserts argv

- [x] 1.4 Parse minimal output
  - Map final assistant text to `RunResult.result`
  - Map error event to `is_error=True`
  - Preserve bridge session id and map it to a deterministic Pi session file
  - PASS: parser unit tests cover text, usage, provider error, and tool status events

- [x] 1.5 Implement cancel
  - JSON subprocess mode: kill process tree through existing `BaseRunner` helpers
  - RPC mode is deferred; JSON subprocess mode is the MVP
  - PASS: uses inherited `BaseRunner.cancel()` process-tree behavior

## Phase 2 — Read-only Bridge MVP

- [x] 2.1 Configure `pi-local` provider profile
  - File: actual config + README example
  - Default tools: `read,grep,find,ls`
  - Workspace: `/Users/feir/.claude` for local deployment example
  - Actual config: `/Users/feir/.config/feishu-bridge/config.json`
  - Backup: `/Users/feir/.config/feishu-bridge/config.json.bak-pi-runner-20260419005420`
  - Smoke: direct Pi/oMLX JSON call returned `PI_LOCAL_CONFIG_OK`; usage input=1370 output=4
  - Model update: `pi-local.models.pi` switched to `Qwen3.6-35B-A3B-mxfp4`; direct smoke returned `PI_QWEN36_OK`

- [x] 2.2 Wire `worker.py` through existing runner contract
  - Constraint: no Pi-specific branches in worker unless unavoidable
  - PASS: existing worker contract reused; no Pi-specific worker branches added

- [x] 2.3 Add command behavior
  - `/agent pi`
  - `/provider pi-local`
  - `/model` displays configured Pi models
  - `/status` displays runner/workspace/tools
  - Feishu-side validation:
    - `/agent pi`: staging bot running as PiRunner with session identity `pi:pi-local`
    - `/provider pi-local`: staging config active after restart; LaunchAgent starts with `agent.type=pi` and provider `pi-local`
    - `/model`: returned current model `Qwen3.6-35B-A3B-mxfp4`; aliases `pi / qwen / gemma / qwen35b`
    - `/status`: returned context usage `1,646 / 32,768 tokens` and model `Qwen3.6-35B-A3B-mxfp4`
  - Result: PASS (Captain confirmed staging command tests complete)

- [x] 2.4 Feishu smoke test
  - Send: "用 pi 列出当前 workspace 顶层文件"
  - PASS: Pi uses read-only file tools and bridge returns final card
  - Staging bot: `feishu-bridge-staging`
  - LaunchAgent: `com.feishu-bridge-staging`
  - Result:
    - First message `hi` completed through PiRunner
    - Tool smoke resumed the same session and called Pi `ls`
    - Tool result `isError=false`
    - Final card update succeeded with `is_error=False`
    - Session map identity: `pi:pi-local`

- [x] 2.5 Restart/resume smoke test
  - Start Pi session through bridge
  - Restart bridge
  - Continue same chat
  - PASS: session resumes or fresh fallback is explicit and logged
  - Result:
    - Restarted `com.feishu-bridge-staging` after first two turns
    - Bridge loaded session map with `_agent_type=pi:pi-local`
    - Follow-up message logged `Pi: resume=True sid=1be90cbf`
    - Pi answered from history: previous tool was `ls`; listed path was `/Users/feir/.claude`

## Phase 3 — Context and Safety Hardening

- [x] 3.1 Create compact Pi global context
  - Candidate: `~/.pi/agent/AGENTS.md`
  - Content: Chinese response policy, safety constraints, bridge subprocess warning, minimal execution policy
  - Limit: <= 12 KB chars
  - Result: created `/Users/feir/.pi/agent/AGENTS.md` with read-only execution policy, compact context policy, and bridge workflow boundary

- [x] 3.2 Add project-local Pi context
  - Candidate: `/Users/feir/.claude/.pi/APPEND_SYSTEM.md`
  - Content: dotclaude-specific compact rules
  - Explicitly exclude full `rules/lessons.md`
  - Result: created `/Users/feir/.claude/.pi/APPEND_SYSTEM.md`; it points Pi at runner-neutral `~/.agents` and explicitly avoids broad rules/lessons loading

- [x] 3.3 Add read-only default enforcement
  - If `args_by_type.pi` omits `--tools`, PiRunner injects `--tools read,grep,find,ls`
  - PASS: argv test
  - Result: `PiRunner.READONLY_TOOLS = "read,grep,find,ls"` and `build_args()` injects it unless config already supplies `--tools` or `--no-tools`

- [x] 3.4 Add write/bash opt-in config
  - Config: `agent.pi.allow_write_tools` or documented `args_by_type.pi` override
  - PASS: default remains read-only
  - Result: README documents `args_by_type.pi` override path; staging/production remains read-only and actual write/bash enablement is deferred to Phase 5 gates

- [x] 3.5 Security review gate
  - Review focus: remote Feishu input -> Pi tools -> filesystem/shell
  - Output: findings fixed or deferred with explicit rationale
  - Artifact: `.specs/changes/pi-runner/security-review.md`
  - Result: PASS for read-only usage; write/edit and shell enablement explicitly deferred to Phase 5

## Phase 4 — Event Fidelity and UX

- [x] 4.1 Tool status mapping
  - Map Pi tool start/end events to bridge `on_tool_status`
  - PASS: Feishu card shows read/grep/find/ls status
  - Result: `PiRunner.parse_streaming_line()` maps `toolcall_start`, `tool_execution_start`, and `tool_execution_end` into `StreamState.pending_tool_status`; `BaseRunner._run_streaming()` drains it to `on_tool_status`
  - Test: `python3 -m pytest -q tests/unit/test_pi_runner.py`

- [x] 4.2 Usage/context mapping
  - Map token usage if Pi emits it
  - If unavailable, mark usage as unknown; do not fabricate values
  - Result: Pi usage maps to `usage`, `last_call_usage`, and `modelUsage` with Pi's 32,768 default context window
  - Test: `python3 -m pytest -q tests/unit/test_pi_runner.py`

- [x] 4.3 `/compact` behavior
  - Verify Pi compact support via CLI/RPC
  - Implement or return "PiRunner does not support bridge-triggered compact"
  - Result: `PiRunner.supports_compact()` returns `False`; `/compact` returns `此 Agent 不支持 /compact 命令。`; context alerts omit `/compact` for non-compact runners
  - Test: `python3 -m pytest -q tests/unit/test_pi_runner.py`

- [x] 4.4 Error formatting
  - User-safe messages for provider unavailable, model missing, tool denied, JSON/RPC protocol error
  - PASS: unit tests for each error class
  - Result: `PiRunner._format_error()` classifies auth, missing model, provider unavailable, tool denied, and protocol errors; top-level Pi `error` events now become terminal error results
  - Test: `python3 -m pytest -q tests/unit/test_pi_runner.py`

- [x] 4.5 Documentation update
  - README: Pi runner setup
  - README: oMLX custom provider setup
  - README: security caveats and read-only default
  - README: rollback to Claude/Codex/local
  - Result: README documents Pi/oMLX setup, read-only default, compact behavior, usage mapping, short context files, and write/shell test gating

## Phase 5 — Graduation

- [x] 5.1 Local dogfood
  - Run PiRunner as default for non-destructive Feishu tasks for 3 sessions
  - Record failures in `.specs/changes/pi-runner/dogfood.md`
  - Artifact: `.specs/changes/pi-runner/dogfood.md`
  - Result: PASS for read-only staging usage; no failures recorded

- [ ] 5.2 Optional write/edit enablement
  - Enable only after dogfood and security review
  - Add explicit config example
  - Add smoke test that edits a disposable temp file
  - Status: deferred by security decision; not required for read-only PiRunner graduation

- [ ] 5.3 Optional bash enablement
  - Require command policy design before enabling
  - Add denylist and/or allowlist
  - Add tests for denied bridge restart and destructive git commands
  - Status: deferred by security decision; not required for read-only PiRunner graduation

- [x] 5.4 Final review
  - Code review: runner contract, subprocess lifecycle, parser robustness
  - Security review: tool exposure and workspace boundary
  - Docs review: setup path reproducible
  - Artifact: `.specs/changes/pi-runner/final-review.md`
  - Result: PASS for read-only PiRunner; write/edit and shell remain deferred

- [x] 5.5 Archive spec
  - Complete `summary.md`
  - Move spec to `.specs/archive/<date>-pi-runner/`
  - Artifact: `.specs/changes/pi-runner/summary.md`
  - Result: archived to `.specs/archive/2026-04-19-pi-runner/` after `v2026.04.19.7`

## Phase 6 — Runner-Neutral Command Runtime

- [x] 6.0 Land initial migration plan
  - File: `.specs/changes/pi-runner/command-runtime-plan.md`
  - Scope: make `/plan`, `/memory-gc`, and `/done` available through bridge-owned workflows so Pi does not depend on Claude Code slash-command runtime

- [x] 6.0a Revise plan for universal skills and runner-specific command policy
  - File: `.specs/changes/pi-runner/command-runtime-plan.md`
  - Decisions:
    - ClaudeRunner defaults to native pass-through for Claude Code skills
    - Pi/Codex use bridge workflow for migrated commands
    - `~/.agents/skills` becomes the canonical universal skill/script layer
    - session journal moves before `/plan`
    - JSON-dependent workflows fail closed on invalid output

- [x] 6.1 Add universal skill registry and command policy
  - Commands: `/plan`, `/memory-gc`, `/done`
  - Fallback commands: `/save`, `/research`, `/wiki`, `/idea`, `/retro`
  - Requirement: scaffold `~/.agents/{skills,rules,memory,adapters}` before parsing registry metadata
  - Requirement: create initial `SKILL.md` frontmatter and `workflow.yaml` skeletons for plan/done/memory-gc
  - Requirement: parse metadata from `SKILL.md` frontmatter and executable steps from `workflow.yaml`
  - Requirement: keep bridge-native commands and workflow commands separate in `/help`
  - Requirement: `workflow.intercept=auto` passes Claude-native skills through for `agent.type=claude`
  - Requirement: Pi/Codex return explicit unsupported messages for known but unmigrated skills
  - Validate: `python3 -m pytest tests/unit/test_workflow_registry.py -q`
  - Spec-Check (2026-04-19):
    - 已扫描：`~/.agents/{skills,rules,memory,adapters}` + `AGENTS.md`
    - 已落地：`SKILL.md` + `workflow.yaml` (plan / done / memory-gc)
    - 已落地：`~/.agents/adapters/bridge/command-registry.yaml` fallback 列表（save/research/wiki/idea/retro）
    - 已落地：`feishu_bridge/workflows/{__init__.py,registry.py}` 解析器 + 三决策 `CommandPolicy`
    - 已落地：`workflow.intercept` config (auto/always/never) 加入 `load_config`
    - 已落地：`FeishuBot.__init__` 启动时加载 registry → `self.command_policy`，异常回退空 policy
    - 已落地：`main.py` slash 分发新增 workflow 分类块；`commands.py` 新增 `workflow-stub` / `workflow-unsupported` 占位 + `/help` 拆分
    - 验证：`pytest tests/unit/test_workflow_registry.py -q` → **18 passed**
    - 回归：`pytest tests/unit -q --ignore bg_supervisor --ignore bg_tasks_db` → **517 passed**
    - 依赖：`pyproject.toml` 声明 `pyyaml>=6.0`；venv 已安装 `pyyaml==6.0.3`
    - 遗留：群聊未 @mention 时 workflow 命令不绕过 group-gate（Phase 6.2 扩展 `_BRIDGE_CMD_EXACT` 前属已知行为）
    - result: PASS

- [x] 6.2 Define workspace policy, symlink adapters, and memory routing
  - Requirement: `~/.claude` is treated as Claude Code home, not the long-term default workspace for non-Claude runners
  - Requirement: bridge runtime state uses `FEISHU_BRIDGE_HOME` / `~/.feishu-bridge`
  - Requirement: universal skills use `AGENTS_HOME` / `~/.agents`
  - Requirement: `~/.agents` is canonical and `.claude` adapters use relative symlink by default; wrappers require explicit reason
  - Requirement: `AGENTS.md` is canonical for universal rules; `CLAUDE.md` is generated/symlink/adapter
  - Requirement: project long-term memory writes to `<repo>/.agents/ctx`, with `<repo>/.claude/ctx` kept as migration adapter
  - Requirement: raw journals/session archives do not become repo-local project ctx by default
  - Validate: `python3 -m pytest tests/unit/test_workspace_policy.py -q`

### Spec-Check 6.2 (2026-04-19)
- 实现：新增 `feishu_bridge/paths.py` 作为 workspace policy resolver（15 个导出符号，纯路径计算，无文件系统副作用）
  - `agents_home()` / `claude_home()` / `bridge_home()` 三个 env-var 解析器（`AGENTS_HOME` / `CLAUDE_HOME` / `FEISHU_BRIDGE_HOME`），tilde 自动展开
  - `default_runner_workspace(runner_type)`：Claude → `claude_home()`，其他（pi/codex/local/unknown）→ `bridge_home()/workspaces/default`；case-insensitive；空字符串 fallback 到非 Claude 路径
  - `project_ctx_dir(repo)` / `legacy_project_ctx_dir(repo)`：canonical `<repo>/.agents/ctx` + legacy adapter `<repo>/.claude/ctx`
  - `session_archive_root()`：`$AGENTS_HOME/memory/sessions`，杜绝 session archive 落入 repo ctx
  - `resolve_skill_source(name)`：优先 `~/.agents/skills/<name>`，fallback `~/.claude/skills/<name>`，均不存在返回 None
  - `resolve_agents_md()`：canonical `$AGENTS_HOME/AGENTS.md`，未 seeded 时返回 None（调用方可 fallback 到 CLAUDE.md）
  - `is_safe_project_ctx_target(path, repo)`：只接受 `<repo>/.agents/ctx/*` 或 `<repo>/.claude/ctx/*`，拒绝含 `sessions` / `archive` 段的路径（防 session archive 污染 project ctx）
- 集成：`feishu_bridge/workflows/registry.py` 原有 `AGENTS_HOME_ENV` + `_DEFAULT_AGENTS_HOME` + `resolve_agents_home()` 三个定义重复了 `paths.py`，已替换为 `from feishu_bridge.paths import AGENTS_HOME_ENV, agents_home as resolve_agents_home`，消除 dual source of truth；`workflows/__init__.py` 原有 re-export 保持对外兼容
- 测试：新增 `tests/unit/test_workspace_policy.py`（29 个 test，100% 通过）+ 原有 `test_workflow_registry.py`（18 个 test，继续全部通过）
- 回归：`pytest tests/ -q --ignore=tests/unit/bg_supervisor --ignore=tests/unit/bg_tasks_db` → **687 passed, 3 skipped**
- 遗留（延后处理，不阻塞 Phase 6.2 收尾）：
  - 文件系统迁移延后：`~/.claude/skills/{plan,done,memory-gc}` 与 `~/.agents/skills/*` 仍是独立副本而非 symlink；Captain 的 `/plan` 正在使用，贸然切换风险高；`paths.py` 已提供 `resolve_skill_source` 的 canonical-first 语义，为后续迁移铺路
  - `main.py:create_runner` 的 `bot_cfg["workspace"]` 默认值未改动：`default_runner_workspace()` 已导出但尚未强制替换，留给 Phase 6.3+ 按 runner_type 改造 bot 配置装载
  - `feishu_bridge/bg_paths.py:bg_home()` 使用独立的 `FEISHU_BRIDGE_BG_HOME` env var 与 `paths.bridge_home()` 的 `FEISHU_BRIDGE_HOME` 并存；bg-tasks 模块当前需要独立 DB/socket 隔离（多 bridge 实例场景），统一延后至 bg-tasks 最终落定后再合并
- result: PASS

- [x] 6.3 Add bridge-owned minimal session journal
  - Requirement: persist user turns, assistant final responses, runner identity, command/workflow events, and artifact paths
  - Requirement: bridge journal is observational only for Claude-native `/done`
  - Requirement: survive bridge restart enough for Pi `/plan` and future `/done`
  - Validate: `python3 -m pytest tests/unit/test_session_journal.py -q`

### Spec-Check 6.3 (2026-04-19)
- 实现：新增 `feishu_bridge/session_journal.py`（~260 行，append-only JSONL）
  - Scope key `(bot_id, chat_id, thread_id)` → SHA1 hex filename；存储根 `$FEISHU_BRIDGE_HOME/journals/`
  - 四类 entry：`user_turn` / `assistant_turn` / `workflow_event` / `artifact`，均含 `ts` + `runner_type` + optional `provider/model/session_id`
  - 隐私控制：user 16KB / assistant 32KB byte-limit 截断 + 四条 redaction 正则（`sk-ant-…` / `sk-…` / bearer / hex ≥40）；`_sanitize` 顺序为 redact-before-truncate，防止半截 secret 溢出
  - 容量控制：`MAX_ENTRIES=500` prune-on-write，`tempfile.mkstemp` + `os.replace` 保证原子 tail-keep
  - POSIX O_APPEND：依赖 `open(..., "a")` 行级原子，未上跨文件锁
- 集成：`feishu_bridge/worker.py` 在 turn flow 末尾写入；split-write 语义 — `user_turn` 无条件写（用户消息真实存在），`assistant_turn` 仅在 `not result.get("is_error")` 时写；整块 `try/except` 包裹，journal 失败降级为 WARN 日志不阻塞 turn
  - 前置变量 `text` 在 worker.py:536 定义 + 多处重写（565/570/579/596/612/614/628/630/797/922），journal 调用点（~1017）任何分支都保持 bound
  - 日志字段改用 `chat_id` 而非 `effective_sid[:8]`，error turn（`effective_sid=None`）不会 NoneType subscript crash
- 测试：`tests/unit/test_session_journal.py` 共 27 个 test，覆盖 scope 哈希稳定性 / append 回路 / 重启持久化 / 截断 / 各 redactor / scope 隔离 / prune tail / read API / 全 runner_type 写入 / workflow+artifact kind / error-turn 语义
- 回归：`pytest tests/unit -q --ignore bg_supervisor --ignore bg_tasks_db` → **703 passed**（6.2 基线 687 + 新增 16 个 journal test + 1 个 error-turn test，净差 +16 ≈ module 规模）
- 遗留（Round 1 延后，以下 Round 2 已解决部分）:
  - ~~hex-blob redactor `\b[a-f0-9]{40,}\b` 会误伤 commit SHA / UUID~~ → **Round 2 已修**：narrowed to `\b[a-f0-9]{64,}\b`，仅截 SHA-256+；新增 `test_does_not_redact_commit_sha` / `test_does_not_redact_uuid_without_hyphens`
  - error-turn 语义只有 module-level test 覆盖；未补 worker-level 集成测试（项目无 `test_worker.py`，142KB `test_bridge.py` 集成成本高，continued deferral → Phase 6.7 实际消费 journal 前回补）
  - 取消（cancel）turn 按设计跳过 journal（continued deferral，6.7 `/done` 若需 cancellation 可见性再补 `append_user_turn` 到取消分支）

### Round 2 — Codex review 回归（2026-04-19）
Codex review（acpx codex exec）针对 6.3 原实现返回 NEEDS-WORK，共 8 条：1 BUG（#1 flock）+ 3 WARN（#2 redactor/#3 sanitize/#4 journal divergence）+ 2 NOTE（#5 worker test / #6 cancellation deferrals）+ 1 STYLE（#7 fd leak）+ 1 NOTE（#8 truncation OK）。Round 2 修复：

- **#1 BUG — flock**：新增 `_scope_lock(path)` context manager + 每 scope 的 sidecar `<journal>.lock` 文件；`_maybe_prune_locked` 在 LOCK_EX 持有时执行 read + tail-keep + `os.replace`，关闭原 O_APPEND + replace 的两段式竞争窗口。Codex 独立跑 2-thread flock sanity 验证（`acq 1 0.0s` / `acq 0 0.475s` 顺序获取）。
- **#2 WARN — redactor 精度**：扩充至 10 条正则，新增 AWS (`AKIA|ASIA`)、Slack (`xox[baprs]-`)、GitHub classic (`ghp_`)、fine-grained (`github_pat_`)、OAuth (`gh[ousr]_`)、raw JWT (`eyJ…`)；generic hex 从 40 → 64 窄化；新增 `test_does_not_redact_commit_sha`/`test_does_not_redact_uuid_without_hyphens` 保护工程文本。
- **#3 WARN — workflow/artifact 脱敏**：新增 `WORKFLOW_COMMAND_MAX_BYTES=256` / `WORKFLOW_DECISION_MAX_BYTES=256` / `ARTIFACT_PATH_MAX_BYTES=2048` 常量；`append_workflow_event` + `append_artifact` 走 `_sanitize`（redact → truncate），entry 携带 `truncated` + `redactions` 字段与 user_turn/assistant_turn 对齐。
- **#4 WARN — worker 重定位**：journal block 从 `effective_sid` 解析后移到 Status-line strip 之后（`worker.py:1098`）；write 位置的 `result.get("result", "")` 即用户实际收到的 assistant 文本。grep 验证其他三处 `result["result"] = …`（stale notice / cancel / soft-timeout）全部发生在 1096 strip 之前。
- **#7 STYLE — mkstemp fd leak**：`_maybe_prune_locked` 初始化 `tmp_fd=None, tmp_name=None`，将 `mkstemp` + `fdopen` + `writelines` + `replace` 包入单一 try/except；`fdopen` 成功后 `tmp_fd=None` 转移所有权；失败路径关闭 fd + unlink temp。
- **测试扩展**：共 44 个 test（Round 1 27 + Round 2 17）。Round 2 新增覆盖：全部新 redactor + 正向 passthrough（40-char SHA / 32-char UUID）、workflow/artifact sanitize 双路径、flock 结构性 spy（LOCK_EX→LOCK_UN 成对）、4-thread × 50-append 并发测（MAX_ENTRIES=20 边界稳定）、多字节 codepoint 截断边界、fd-leak resilience（monkeypatched `os.fdopen` 抛错）。

Round 2 验证：
- `python3 -m pytest tests/unit/test_session_journal.py` → 44 passed（Codex 本地独立跑过同数字）
- 全量回归（via venv python）：`tests/unit` 非 journal 667 pass + cli_bg 27 pass + update_doc 26 pass = **720 passed**, 0 regression

Round 2 剩余 Codex NOTE（不阻塞）：
- 更高价值 secret 家族（GitLab `glpat-` / Stripe `sk_live_` / Datadog 32-hex / npm token）尚未覆盖 — 建议作为未来 privacy-hardening 独立 pass，避免再次泛化 hex 规则
- delivery-layer banner（context_alert footer、update banner）仍可能在 journal 之后追加 — 现有 journal 语义定义为"bridge cleanup 后的 assistant 文本"，comment 已明确不承诺 byte-for-byte rendered output

**Codex Round 2 verdict: READY-WITH-NITS**（both nits accepted as future work）

- result: PASS

- [x] 6.4 Implement `/plan` workflow MVP for non-Claude runtimes
  - Requirement: draft plan first, wait for confirmation, then write `.specs/changes/<name>/proposal.md` and `tasks.md`
  - Requirement: source canonical metadata/prompts from `~/.agents/skills/plan`
  - Requirement: use recent bridge journal context
  - Requirement: with `agent.type=claude` and `workflow.intercept=auto`, pass `/plan` through to Claude Code
  - Requirement: pending confirmation expires after `/plan` TTL declared in `workflow.yaml` (default target: 7d)
  - Validate: `python3 -m pytest tests/unit/test_plan_workflow.py -q`

### Spec-Check 6.4

- bridge-owned runtime: `feishu_bridge/workflows/runtime.py` — state enum, `WorkflowContext`, `WorkflowResult`, 3-strike `request_json_with_policy`, `parse_ttl_seconds`
- persistence: `feishu_bridge/workflows/storage.py` — SQLite WAL, one active workflow per scope, `mark_expired_waiting`, 30-day terminal retention
- PlanWorkflow: `feishu_bridge/workflows/plan_workflow.py` — draft via runner, spec-resolve 3-slot guard, render preview, confirm → `scripts/spec-write.py` writes proposal.md + tasks.md; cancel drops without writing
- dispatch: `feishu_bridge/main.py` adds `/confirm` → `workflow-confirm`, `DECISION_BRIDGE_WORKFLOW` → `workflow-run` with `_workflow_skill` carried separately so `cmd_arg` preserves the user's goal text; `_heavy_cmds` gains `workflow-run`/`workflow-confirm` so LLM + SQLite writes serialize with normal turns per scope
- handlers: `feishu_bridge/commands.py` — `_handle_workflow_run` (scope invariant + stale sweep + storage.create), `_handle_workflow_confirm` (storage.active_for_scope → resume_confirm → terminal update), `_cancel_waiting_workflow` hooked into `/stop`
- journal integration: `_handle_workflow_run` / `_handle_workflow_confirm` / `_cancel_waiting_workflow` now append `workflow_event` entries, and confirmed artifacts append `artifact` entries, so Phase 6.3's journal surface is actually populated by bridge-owned workflows rather than only unit-callable
- claude pass-through: CommandPolicy resolves `/plan` to `DECISION_CLAUDE_NATIVE` when `runners.claude=native` and `workflow.intercept=auto`, covered by `test_workflow_registry.py::test_plan_native_fallthrough_claude_auto` (unchanged)
- TTL: `PlanWorkflow` honours `workflow.yaml` ttl via `parse_ttl_seconds` (default 7d); `WorkflowStorage.mark_expired_waiting` promotes expired rows to `expired`

**Validation deviations (declared — Captain approved 2026-04-19):**
- Confirmation mechanism tightened from plain-text reply to explicit `/confirm` bridge command. Rationale: deterministic routing through the existing CommandRouter; avoids NLU on arbitrary text during the 7-day TTL window. Captain explicitly approved the `/confirm` tightening on 2026-04-19; Phase 6.5 (`/memory-gc`) may build on the same contract.

**Validation result (bridge-side unit tests):**

```
python3 -c "import pytest; raise SystemExit(pytest.main([
    'tests/unit/test_plan_workflow.py',
    'tests/unit/test_commands_workflow_wiring.py',
    'tests/unit/test_workflow_registry.py',
    '-q',
]))"
44 passed in 1.52s
```

- `test_plan_workflow.py` (17 tests): workflow internals — start/resume_confirm/resume_cancel, 3-strike JSON policy, storage CRUD + expiry, TTL parsing
- `test_commands_workflow_wiring.py` (11 tests, NEW): BridgeCommandHandler dispatch for `workflow-run` / `workflow-confirm` / `workflow-unsupported` / `/stop` + `_handle_workflow_run` input-validation branches + workflow/artifact journal append coverage + `_scope_key_from_item ≡ WorkflowContext.scope_key` invariant (closes advisor-flagged wiring-coverage gap)
- `test_workflow_registry.py` (18 tests): regression on CommandPolicy resolution
- Not unit-covered: main.py `cmd == "/confirm"` → `workflow-confirm` mapping. Parse is a single exact-match branch in the dispatcher; adding a unit test would require heavy setup of `_process_message`. Manually exercised end-to-end during bridge dry-run; regression risk flagged in Spec-Check rather than locked by test.

Broader `tests/unit` regression: 509 passed, 1 pre-existing subprocess timeout in `test_update_doc.py` (Feishu re-auth under system Python 3.14), 1 pre-existing `ModuleNotFoundError: requests` in `test_cli_bg.py` under system Python 3.14 — both environmental, unrelated to Phase 6.4.

Post-review validation (2026-04-19):
- Targeted Phase 6.1-6.4 suite under repo venv: `119 passed`
- Wider non-bg/socket unit slice under repo venv: `626 passed`
- `test_cli_bg.py` under repo venv: `26 passed, 1 sandbox AF_UNIX bind failure` (`PermissionError: [Errno 1] Operation not permitted`), unrelated to workflow runtime

- result: PASS (code + tests complete; `/confirm` deviation approved by Captain 2026-04-19)

- [x] 6.5 Implement `/memory-gc --dry-run`
  - Requirement: run stats script, ask runner only for bounded classification JSON, do not mutate files in dry-run
  - Requirement: owner/allowlist permission required in group chats
  - Requirement: pending confirmation expires after `/memory-gc` TTL declared in `workflow.yaml` (default target: 24h)
  - Requirement: invalid JSON retries and then fails closed without mutation
  - Validate: `python3 -m pytest tests/unit/test_memory_gc_workflow.py -q`

### Spec-Check 6.5 (2026-04-19)

- 实现：新增 `feishu_bridge/workflows/memory_gc_workflow.py`
  - 仅支持 `/memory-gc --dry-run`；不带 `--dry-run` 时 fail closed，提示写入模式尚未实现
  - 运行 `memory-gc-stats.sh` 获取 deterministic stats；优先 `~/.agents/skills/memory-gc/scripts/`，迁移期 fallback 到 `~/.claude/skills/memory-gc/scripts/`
  - daily lesson 文件按 bounded excerpt 读取：最多 8 个文件，每个 2500 chars；curated lessons 最多 120 条
  - LLM 只负责输出分类 JSON：`summary` / `daily` / `curated` / `recommendations`
  - JSON policy 复用 3-strike 机制；失败时返回 error report，未修改任何文件
  - dry-run 直接 `completed`，不创建 waiting confirmation；apply/write mode 留给后续阶段
- 集成：`commands.py`
  - `workflow-run` 支持 `memory-gc`
  - `workflow-confirm` / `/stop` 具备 memory-gc 分支，当前 confirm 会明确拒绝 apply（不会写文件）
  - 群聊安全门：`/memory-gc` 仅允许 group owner 或显式 `allowed_users` 用户执行；DM 不额外限制
  - workflow start/confirm/cancel 继续写入 session journal
- 测试：
  - `tests/unit/test_memory_gc_workflow.py` 覆盖 write-mode rejection、健康空跑、daily/curated 分类、JSON failure fail-closed、bad stats JSON
  - `tests/unit/test_commands_workflow_wiring.py` 覆盖 memory-gc dispatch 和群聊非 owner 拒绝
  - Phase 6.1-6.5 targeted suite: `126 passed`
- 遗留：
  - `~/.agents/skills/memory-gc/scripts/` 尚未建立 canonical 脚本 symlink；当前代码迁移期 fallback 到 `~/.claude`，避免阻塞 bridge runtime
  - `/memory-gc` apply/write mode、route/archive/maintain 脚本 AGENTS_HOME 化留到后续 memory migration pass

- result: PASS (dry-run MVP complete; apply mode intentionally deferred)

Post-review fix:
- `shlex.split()` malformed args now fail closed with a user-visible parse error instead of propagating an exception.

- [x] 6.6 Add workflow state visibility and cleanup
  - Requirement: `/status` shows active workflow id, command, state, current step, expiration, and last error
  - Requirement: expired waiting confirmations are marked `expired`
  - Requirement: `/stop` cancels `waiting_confirmation` workflows without writing files
  - Validate: `python3 -m pytest tests/unit/test_workflow_status.py -q`

### Spec-Check 6.6 (2026-04-19)

- 实现：`/status` 调用 workflow storage `mark_expired_waiting()` 后读取当前 scope active workflow
  - 无活跃 normal session 时仍会展示 active workflow，不再被 “当前没有活跃会话” 提前吞掉
  - 展示字段：`/<skill>`、state、short id、current step、plan slug、expires-in、last_error
  - 有 normal session/cost 数据时，workflow section 插入 Context section 后
- 清理：expired waiting rows 由 `WorkflowStorage.mark_expired_waiting()` 统一处理，`/status`、workflow start、workflow confirm 都会触发 sweep
- 取消：`/stop` 已在 Phase 6.4 接入 `_cancel_waiting_workflow()`；Phase 6.5 后同时支持 plan / memory-gc 分支，cancel 不写文件并写 journal event
- 测试：新增 `tests/unit/test_workflow_status.py`
  - active workflow 在无 session 时可见
  - 无 workflow 时保留既有 no-session 文案
- Validation:
  - `test_workflow_status.py + workflow command/workflow tests` → `37 passed`
  - Phase 6.1-6.6 targeted suite → `128 passed`

- result: PASS

- [x] 6.7 Implement `/done` MVP for non-Claude runtimes
  - Requirement: extract structured JSON from bridge journal, validate schema, then reuse or port existing done scripts under `~/.agents/skills/done`
  - Requirement: `session-done-apply.sh` and related scripts support `AGENTS_HOME` / `CLAUDE_HOME` or bridge wrappers
  - Requirement: `session-history` reads both `$AGENTS_HOME/memory/sessions` and `$CLAUDE_HOME/memory/sessions` during migration
  - Requirement: route raw archives to local memory and project ctx to `<repo>/.agents/ctx`
  - Requirement: route global lessons to `~/.agents/memory/lessons.md` or `~/.agents/rules/lessons.md`
  - Requirement: pending confirmation expires after `/done` TTL declared in `workflow.yaml` (default target: 2h)
  - Requirement: no auto-commit or review worker in MVP unless explicitly configured
  - Requirement: with `agent.type=claude` and `workflow.intercept=auto`, pass `/done` through to Claude Code
  - Requirement: invalid JSON must not corrupt memory files
  - Validate: `python3 -m pytest tests/unit/test_done_workflow.py -q`

### Spec-Check 6.7 (2026-04-19)

- 实现：新增 `feishu_bridge/workflows/done_workflow.py`
  - 从 bridge `SessionJournal` 读取 bounded excerpt，runner 只负责提取结构化 JSON
  - JSON required keys：`title` / `activities` / `decisions` / `lessons` / `open_loops` / `noise_filtered`
  - start 阶段只生成草稿并进入 `waiting_confirmation`；`/confirm` 前不写文件
  - confirm 阶段 deterministic write：
    - session archive → `$AGENTS_HOME/memory/sessions/<project>/<YYYY-MM-DD>.md`
    - daily lessons → `$AGENTS_HOME/memory/lessons/<YYYY-MM-DD>.md`
    - project ctx timeline → `<repo>/.agents/ctx/session-timeline.md`
  - 写入失败或 JSON failure fail closed；不 auto-commit，不启动 review worker
- 集成：`commands.py`
  - `workflow-run` / `workflow-confirm` / `/stop` 支持 `done`
  - ClaudeRunner 在 `workflow.intercept=auto` 下仍由 CommandPolicy pass-through 到 Claude Code native `/done`
  - start/confirm/cancel 均通过 Phase 6.4 journal hook 记录 workflow events/artifacts
- session-history：
  - `/Users/feir/.claude/bin/session-history` 已改为迁移期双读 `$AGENTS_HOME/memory/sessions` + `$CLAUDE_HOME/memory/sessions`
  - index 仍写到 Claude legacy index 文件，record id 使用 `agents:<rel>#n` / `claude:<rel>#n` 防止双根 collision
- 测试：
  - `tests/unit/test_done_workflow.py` 覆盖 extract wait-confirm、confirm 写入 AGENTS_HOME + repo ctx、invalid JSON fail closed、empty journal failure
  - `python3 -m py_compile /Users/feir/.claude/bin/session-history` PASS
  - temp HOME/AGENTS_HOME smoke: `session-history rebuild` indexed 2 sections from both roots; `list` showed both `agents` and `claude` sessions
  - Phase 6.1-6.7 targeted suite → `132 passed`
  - Wider non-bg/socket unit slice → `639 passed`
- 遗留：
  - 旧 `session-done-apply.sh` 仍硬编码 `~/.claude`；bridge MVP 通过 Python workflow port 避免误写，后续可将 shell scripts 正式 AGENTS_HOME 化
  - anchor sync 目前以 `.agents/ctx/session-timeline.md` 形式落地，未覆盖旧 `MEMORY.md` anchor 同步

- result: PASS (bridge MVP complete; Claude-native `/done` unchanged)

Post-review fix:
- `/done` confirm now re-validates stored payload before writing and wraps `OSError` as workflow failure, so corrupted payload/filesystem errors do not bubble out of the handler.

- [x] 6.8 Add AgentPool for review delegation
  - Requirement: support plan-reviewer/code-reviewer/security-reviewer as bridge-managed worker calls
  - Requirement: worker failure must be visible and must not be reported as success
  - Requirement: AgentPool and JSON fallback runner calls share unified budget/rate-limit accounting

### Spec-Check 6.8 (2026-04-19)

- 实现：新增 `feishu_bridge/workflows/agent_pool.py`
  - 支持 roles：`plan-reviewer` / `code-reviewer` / `security-reviewer`
  - `AgentPoolTask` / `AgentPoolResult` / `AgentPoolBudget` 提供明确输入输出和批次预算
  - reviewer call 通过当前 workflow runner 执行，tag 使用 `agent-pool:<role>`；因此复用同一 LLM client / runner 配置 / rate-limit 面
  - runner `is_error=True` 时返回 `ok=False`，error text 可见，不会被包装成成功
  - unsupported role 和 budget exhausted 都返回失败结果且不调用 runner
- 集成：`workflows/__init__.py` re-export AgentPool API；尚未默认接入 `/done` 自动 review，避免改变 MVP 行为
- 测试：新增 `tests/unit/test_agent_pool.py`
  - supported reviewers run through active runner
  - runner error visible
  - unsupported role no runner call
  - shared call budget exhaustion visible
- Validation:
  - Phase 6.1-6.8 targeted suite → `138 passed`
  - Wider non-bg/socket unit slice → `645 passed`

- result: PASS (infrastructure complete; workflow opt-in integration deferred)

## Test Plan

```bash
pytest tests/unit/test_bridge.py
pytest tests/unit/test_local_runner.py
pytest tests/unit/test_task_runner.py
```

Add new tests:

```text
tests/unit/test_pi_runner.py
tests/unit/test_pi_runner_events.py
tests/unit/test_bridge_pi_config.py
```

Manual smoke:

```text
1. Start oMLX server.
2. Run Pi direct smoke from /Users/feir/.claude.
3. Start feishu-bridge with agent.type=pi.
4. Send Feishu message requiring file read.
5. Send `/new`, continue chat.
6. Restart bridge, continue chat.
7. Send write request and verify read-only refusal in MVP.
```

## Self-check

- Completeness: phases cover discovery, implementation, safety, UX, graduation.
- Consistency: task names use PiRunner for bridge integration and oMLX for model provider.
- Executability: each phase has commands or PASS criteria.
- Auditability: artifacts are named for JSON/RPC samples and dogfood results.
