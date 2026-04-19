# Plan: universal skills and runner-neutral workflow runtime

## Status

- Change: `pi-runner`
- Scope: follow-up plan after PiRunner read-only MVP
- Revision: 2026-04-19
- Baseline commit: `d576b21ef00c17f13a128372a36dfc7f8db8b962`
- Existing Claude Code home: `/Users/feir/.claude`
- Proposed universal agent home: `/Users/feir/.agents`
- Target bridge modules:
  - `feishu_bridge/main.py`
  - `feishu_bridge/commands.py`
  - `feishu_bridge/worker.py`
  - new `feishu_bridge/workflows/`
  - new `feishu_bridge/skills/` or `feishu_bridge/agent_skills.py`

## Problem

PiRunner can answer Feishu messages, but it does not inherit the Claude Code
runtime that currently powers `/plan`, `/done`, `/memory-gc`, workflow dispatch,
memory maintenance, hooks, and subagent review.

The missing layer is not just prompt text. It is a runtime plus a skill/script
library:

| Current Claude Code asset | Current role | Pi/Codex gap |
|---|---|---|
| `/plan` | Critical task planning and `.specs` persistence | Plain text unless bridge intercepts it |
| `/done` | Session archival, lessons extraction, anchor sync, commit, GC | No Claude transcript or Stop hook |
| `/memory-gc` | Memory routing, pruning, archiving, count sync | Model can classify, but should not drive scripts directly |
| `TodoWrite` | Progress state | No bridge-visible equivalent |
| `Agent` / subagents | Review and work delegation | No Claude Code subagent runtime |
| Stop / PreCompact / TaskCompleted hooks | Lifecycle automation | Bridge must own lifecycle events for non-Claude runners |
| `~/.claude/skills/*` | Skill definitions and scripts | Claude-specific location and tool semantics |
| `~/.claude/memory/*` | Session history and lessons store | Needs controlled cross-runner access |

## Corrected Direction

The bridge workflow layer is a compatibility layer for runners that do not have
Claude Code's native runtime. It must not downgrade ClaudeRunner.

```text
ClaudeRunner
  -> default: pass through Claude-native slash commands and skills
  -> optional: bridge workflow only if explicitly configured

PiRunner / CodexRunner / LocalHTTPRunner
  -> use bridge workflow runtime for supported workflow commands
  -> reject unsupported workflow/skill commands explicitly

Universal skill layer
  -> canonical workflow metadata, prompts, schemas, and scripts
  -> adapters expose the same skills to Claude Code, bridge, Pi, and Codex
```

## Goal

Create a runner-neutral workflow runtime and universal skill/script layer so
non-Claude runners can use core workflows without depending on Claude Code
internals.

```text
Feishu message
  |
  v
CommandRouter
  |-- bridge-native command: /new /status /provider /model
  |-- Claude-native command: pass through when runner is ClaudeRunner
  |-- bridge workflow command: /plan /memory-gc /done for Pi/Codex/local
  |-- unsupported skill command: explicit rejection for non-Claude runners
  `-- normal message -> BaseRunner

WorkflowRuntime
  |-- deterministic steps: Python, shell, filesystem, git, sqlite
  |-- LLM steps: runner.generate_text / runner.generate_json
  |-- progress state: bridge-owned replacement for TodoWrite
  |-- confirmation state: waits for user approval before mutating
  |-- session journal: bridge-owned recent context
  |-- skill registry: ~/.agents/skills
  `-- agent pool: optional Claude/Codex/Pi worker calls
```

## Non-Goals

- Do not make Pi emulate Claude Code internals.
- Do not force bridge workflow interception for ClaudeRunner by default.
- Do not inject full `CLAUDE.md`, all skills, all rules, and all lessons into Pi.
- Do not migrate every `.claude/skills/*` skill in the first release.
- Do not enable write/bash tools for Pi as part of the command runtime MVP.
- Do not remove ClaudeRunner or Claude Code slash command support.

## Design Principles

1. **Claude native stays native**: when `agent.type=claude`, `/plan`, `/done`,
   `/memory-gc`, `/save`, `/research`, and similar commands default to
   pass-through so Claude Code keeps its native Skill, hook, rules, and subagent
   behavior.
2. **Bridge fills runtime gaps**: Pi/Codex/local use bridge workflows only for
   commands registered as supported.
3. **`~/.agents/skills` becomes SSoT**: canonical skill metadata, prompts,
   schemas, and scripts move to an agent-neutral location. `~/.claude/skills`
   becomes an adapter layer over time.
4. **Bridge owns control flow**: state, retries, confirmation, file writes, and
   script execution are deterministic bridge code.
5. **Runners only do model work**: Pi classifies, summarizes, drafts, and emits
   JSON. It must not decide which scripts to execute.
6. **Load only active step context**: Pi receives the current step prompt,
   schema, compact journal excerpt, and required file excerpts.
7. **Fail closed on invalid model output**: invalid JSON or schema failures must
   not mutate memory, specs, git, or user files.

## Universal Agent Skills Layer

`~/.agents` should hold the runner-neutral skill library. It is not a second
Claude Code home. It is a canonical workflow and script library with thin
adapters for each runtime.

```text
~/.agents/
  AGENTS.md
  README.md
  rules/
    common.md
    security.md
    token-budget.md
  skills/
    plan/
      SKILL.md
      workflow.yaml
      schemas/
        plan-draft.schema.json
      prompts/
        draft.md
        persist.md
      scripts/
        spec-resolve.py
        spec-write.py
    done/
      SKILL.md
      workflow.yaml
      schemas/
        extraction.schema.json
      prompts/
        extract.md
      scripts/
        session-done-apply.sh
        memory-anchor-sync.sh
        session-done-format.py
    memory-gc/
      SKILL.md
      workflow.yaml
      schemas/
        classify.schema.json
      prompts/
        classify.md
      scripts/
        memory-gc-stats.sh
        memory-gc-route.sh
        memory-gc-archive.sh
  adapters/
    claude/
      skills/
      CLAUDE.md
    bridge/
      command-registry.yaml
    pi/
      AGENTS.md
```

Recommended migration policy:

- Scaffold `~/.agents/{skills,rules,memory,adapters}` before registry parsing.
- Make `~/.agents/skills/<name>/` canonical for migrated skill files.
- Prefer relative symlinks from `~/.claude/skills/<name>/` to canonical
  `~/.agents/skills/<name>/` files during migration. This prevents a copied
  script or prompt from going stale when one side changes.
- Use wrappers only when symlink behavior is unsafe for a specific file.
- For repo-local ctx, make `<repo>/.agents/ctx/` canonical and keep
  `<repo>/.claude/ctx/` as symlinks or adapter files during migration.
- Use `AGENTS_HOME="${AGENTS_HOME:-$HOME/.agents}"` in new scripts.
- Keep `CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"` for memory files that still
  live under Claude home.
- Bridge reads `workflow.yaml`, prompts, and schemas from `~/.agents`.
- Claude Code can continue using native skills while gradually adopting the
  canonical scripts.
- `~/.agents/AGENTS.md` is canonical for universal agent rules. Claude-specific
  `CLAUDE.md` files should be generated views, symlinks, or thin adapters; do
  not maintain a third independent rules source.

## Skill Contract

Each universal skill has three layers.

### 1. Metadata

Machine-readable frontmatter lives in `SKILL.md`. `workflow.yaml` must not
duplicate these fields; it only describes executable steps. Claude Code ignores
unknown frontmatter fields, so universal metadata can be added without breaking
Claude-native usage.

```yaml
name: done
user_invocable: true
triggers:
  - /done
  - 结束会话
  - 归档
capabilities:
  - read
  - write
  - shell
  - git
  - memory
runners:
  claude: native
  pi: bridge_workflow
  codex: bridge_workflow
  local: unsupported
```

### 2. Workflow spec

Bridge-executable state machine in `workflow.yaml`. Keep metadata out of this
file to avoid two sources of truth.

```yaml
steps:
  - id: read_journal
    type: deterministic
    action: journal.read

  - id: extract_json
    type: llm_json
    prompt: prompts/extract.md
    schema: schemas/extraction.schema.json

  - id: apply
    type: script
    command: scripts/session-done-apply.sh
    input: extract_json.output
```

### 3. Step prompt

Small prompt for one LLM step, not a full skill body:

```text
Extract session archive JSON.
Return only JSON matching the schema.
Do not write files. Bridge will validate and execute scripts.
```

## Command Policy

Default policy:

| Command class | ClaudeRunner | PiRunner | CodexRunner | LocalHTTPRunner |
|---|---|---|---|---|
| Bridge native: `/new`, `/status`, `/provider`, `/model` | bridge handles | bridge handles | bridge handles | bridge handles |
| Claude-native skills: `/save`, `/research`, `/wiki`, `/idea`, `/retro` | pass-through | reject unless migrated | reject unless migrated | reject |
| `/plan` | pass-through by default | bridge workflow | bridge workflow | bridge workflow or reject |
| `/memory-gc` | pass-through by default | bridge workflow, dry-run first | bridge workflow, dry-run first | reject by default |
| `/done` | pass-through by default | bridge workflow after journal exists | bridge workflow after journal exists | reject by default |

Config override:

```jsonc
"workflow": {
  "intercept": "auto" // auto | always | never
}
```

`auto` means:

- ClaudeRunner uses pass-through for Claude-native skills.
- Pi/Codex/local use bridge workflows when supported.
- Unsupported non-Claude commands return a clear error, not a normal model turn.

## JSON Reliability Policy

Pi and other local models may produce invalid JSON for long or complex prompts.
Every JSON-dependent workflow must use the same fail-closed policy.

1. First attempt: request strict JSON.
2. Second attempt: send validation error and ask for corrected JSON only.
3. Third attempt: allow extraction from a fenced `json` block.
4. If still invalid: stop workflow with `failed`, show an actionable error, and
   do not mutate files.
5. Optional fallback runner:
   - disabled by default for privacy and cost
   - configurable per workflow, for example `fallback_runner=claude`
   - fallback still requires schema validation before mutation

## Workflow Contract

Each workflow is a Python class with deterministic state transitions.

```python
class Workflow:
    name: str
    version: int

    def start(self, ctx: WorkflowContext, arg: str) -> WorkflowResult: ...
    def resume(self, ctx: WorkflowContext, user_text: str) -> WorkflowResult: ...
```

`WorkflowContext` includes:

- `bot_id`, `chat_id`, `thread_id`, `sender_id`, `chat_type`
- `group_owner` or owner/allowlist resolver
- `workspace`
- `session_key`
- current `BaseRunner`
- command policy
- `ResponseHandle`
- compact `session_journal`
- `agents_home`
- `claude_home`
- allowed script roots
- mutation permission state

`WorkflowResult` includes:

- `state`: `running | waiting_confirmation | completed | failed | expired`
- `user_message`: Feishu-facing text
- `progress`: current step list
- `next_expected_input`: optional confirmation or selection prompt
- `expires_at`: set for waiting states
- `artifacts`: paths created or updated

## Phase Plan

### Phase 0 - Universal skill registry and command policy

Deliverables:

- Scaffold:
  - `~/.agents/skills`
  - `~/.agents/rules`
  - `~/.agents/memory`
  - `~/.agents/adapters`
- Add initial MVP skill directories:
  - `~/.agents/skills/plan`
  - `~/.agents/skills/done`
  - `~/.agents/skills/memory-gc`
- Add initial `SKILL.md` frontmatter and `workflow.yaml` skeleton for the three
  MVP skills.
- Add workflow command registry for:
  - `/plan`
  - `/done`
  - `/memory-gc`
- Add fallback policy for known but unmigrated commands:
  - `/save`
  - `/research`
  - `/wiki`
  - `/idea`
  - `/retro`
- Parse skill metadata from `~/.agents/skills/*/SKILL.md` frontmatter.
- Parse executable workflow steps from `~/.agents/skills/*/workflow.yaml`.
- Bootstrap may import from `~/.claude/skills/*/SKILL.md` once to create the
  canonical `~/.agents/skills/*/SKILL.md`; after that, symlink or adapter policy
  prevents long-lived copy drift.
- Add `workflow.intercept` config with default `auto`.
- Add per-workflow wait TTL in `workflow.yaml`:
  - `/plan`: 7 days
  - `/memory-gc`: 24 hours
  - `/done`: 2 hours

Validation:

```bash
python3 -m pytest tests/unit/test_workflow_registry.py -q
```

Acceptance:

- `/help` lists bridge-native commands and workflow commands separately.
- `~/.agents` skeleton exists before registry parsing.
- The three MVP skills have `SKILL.md` metadata and `workflow.yaml` step
  skeletons.
- `.claude` adapter files use relative symlinks or documented wrappers, not
  silent copies.
- With `agent.type=claude`, `/plan` and `/done` pass through by default.
- With `agent.type=pi`, unsupported commands like `/save` return an explicit
  unsupported message.
- Existing `/new`, `/status`, `/provider`, `/model`, `/compact` behavior is
  unchanged.

### Phase 0.5 - Minimal session journal

Why before `/plan`:

`/plan` often needs recent user context, prior decisions, and constraints
discussed before the explicit `/plan` message. Without a bridge-owned journal,
Pi can only plan from the trigger message.

Deliverables:

- Persist per-turn journal entries under bridge state:
  - user message
  - assistant final response
  - runner type/provider/model
  - bridge command/workflow event
  - workflow artifact paths
- Keep ClaudeRunner journal observational only. It is not canonical for
  Claude-native `/done`.
- Add privacy rules:
  - do not persist secrets from config/env
  - truncate large content
  - record redaction markers

Validation:

```bash
python3 -m pytest tests/unit/test_session_journal.py -q
```

Acceptance:

- Normal Pi turns append journal entries.
- Restarting bridge preserves recent journal entries.
- `/status` can show whether a journal exists and the latest entry timestamp.

### Phase 1 - `/plan` workflow MVP for non-Claude runtimes

Implementation:

- Add `PlanWorkflow`.
- Source canonical metadata and prompts from `~/.agents/skills/plan/`.
- During bootstrap, `~/.agents/skills/plan/SKILL.md` may be copied from or
  adapted from `/Users/feir/.claude/skills/plan/SKILL.md`.
- Reuse or port the existing spec resolver:
  - current source: `/Users/feir/.claude/skills/done/scripts/spec-archive-validate.py --mode resolve`
  - future source: `~/.agents/skills/plan/scripts/spec-resolve.py`
- Split planning into states:
  - `draft`: produce WHY, WHAT, NOT, risks, approaches, acceptance criteria
  - `waiting_confirmation`: wait for deterministic confirmation
  - `persist`: bridge writes `proposal.md`, `tasks.md`, optional `design.md`
- Add waiting TTL:
  - `/plan`: 7 days, declared in `workflow.yaml`
  - expired workflows become `expired` and require a new `/plan`

Validation:

```bash
python3 -m pytest tests/unit/test_plan_workflow.py -q
```

Acceptance:

- `/plan <goal>` through Pi returns a structured plan using recent journal
  context and waits for confirmation.
- Before confirmation, no files are written.
- After confirmation, bridge writes `.specs/changes/<name>/proposal.md` and
  `tasks.md`.
- With `agent.type=claude` and `workflow.intercept=auto`, `/plan` is passed to
  Claude Code instead of bridge workflow.

### Phase 2 - `/memory-gc --dry-run`

Why second:

- Most steps are deterministic scripts.
- The model only classifies lessons and emits JSON.
- `--dry-run` gives a safe validation path before memory mutation.

Implementation:

- Add `MemoryGcWorkflow`.
- Require owner/allowlist permission in group chats.
- Source canonical metadata from `~/.agents/skills/memory-gc/`.
- Step 1 runs stats script:
  - bootstrap source: `/Users/feir/.claude/skills/memory-gc/scripts/memory-gc-stats.sh`
  - future source: `~/.agents/skills/memory-gc/scripts/memory-gc-stats.sh`
- Step 2 sends bounded lesson batches to runner for classification:
  - `KEEP`
  - `ABSORBED`
  - `DUPLICATE`
  - `OUTDATED`
- Step 3 validates JSON with retry/fenced-block fallback.
- Step 4 renders a dry-run report.
- Defer write mode until classification is stable.

Validation:

```bash
python3 -m pytest tests/unit/test_memory_gc_workflow.py -q
```

Acceptance:

- `/memory-gc --dry-run` reports current stats and proposed actions.
- No files are modified in dry-run mode.
- Invalid runner JSON fails closed with an actionable error.
- Non-owner group users cannot run memory-affecting workflows.

### Phase 3 - Workflow state visibility and cleanup

Implementation:

- Add workflow state to `/status`:
  - active workflow id
  - command
  - state
  - current step
  - waiting expiration time
  - last error
- Add cleanup for expired `waiting_confirmation` workflows.
- Add `/workflow cancel` only if needed; otherwise `/stop` can cancel active
  running workflow processes but not persisted completed history.
- Define `/stop` behavior for waiting workflows:
  - active running workflow: stop subprocess/workflow execution
  - `waiting_confirmation`: mark wait as `cancelled` and remove it from active
    `/status`

Validation:

```bash
python3 -m pytest tests/unit/test_workflow_status.py -q
```

Acceptance:

- A pending `/plan` is visible in `/status`.
- Expired waits are marked `expired`.
- `/stop` cancels a `waiting_confirmation` workflow and reports that no files
  were written after cancellation.
- Debugging a hung workflow does not require reading sqlite manually.

### Phase 4 - `/done` MVP for non-Claude runtimes

Precondition:

- Phase 0.5 journal is complete enough for session summarization.
- ClaudeRunner remains pass-through by default to avoid double archive writes.
- `session-done-apply.sh` and related done scripts support `AGENTS_HOME` and
  `CLAUDE_HOME` or have bridge-side wrappers that route inputs correctly.
- `session-history` migration strategy is implemented:
  - canonical session archives live under `$AGENTS_HOME/memory/sessions`
  - migration-period search/rebuild reads both `$AGENTS_HOME/memory/sessions`
    and `$CLAUDE_HOME/memory/sessions`
  - new writes go to `$AGENTS_HOME/memory/sessions`

Implementation:

- Add `DoneWorkflow`.
- Source canonical metadata from `~/.agents/skills/done/`.
- Use session journal plus schema:
  - bootstrap source: `/Users/feir/.claude/skills/done/assets/extraction-schema.json`
  - future source: `~/.agents/skills/done/schemas/extraction.schema.json`
- Ask runner for structured JSON:
  - `activities`
  - `decisions`
  - `lessons`
  - `open_loops`
  - `noise_filtered`
- Validate JSON with the shared reliability policy.
- Reuse or port deterministic scripts:
  - `session-done-apply.sh`
  - `memory-anchor-sync.sh`
  - `stale-ctx-check.sh`
  - `session-done-format.py`
- Defer automatic commit/review until a later subphase unless explicitly
  enabled.

Validation:

```bash
python3 -m pytest tests/unit/test_done_workflow.py -q
```

Acceptance:

- `/done` through Pi writes a raw session archive from bridge journal to
  `$AGENTS_HOME/memory/sessions/<project>/`.
- `session-history` can find bridge-created archives during migration.
- `/done` updates memory anchors through existing scripts or wrappers.
- Extracted global lessons route to `~/.agents/memory/lessons.md` or
  `~/.agents/rules/lessons.md`.
- Extracted project ctx updates route to `<repo>/.agents/ctx/` with
  `<repo>/.claude/ctx/` kept as adapter/symlink if needed.
- Short/noise-only sessions exit without writing misleading archives.
- Invalid JSON does not corrupt memory files.
- With `agent.type=claude` and `workflow.intercept=auto`, `/done` passes through
  to Claude Code, so bridge journal does not become a second canonical archive.

### Phase 5 - AgentPool and review delegation

Implementation:

- Add `AgentPool` as a bridge service.
- Share budget/rate-limit accounting with JSON fallback runner calls through the
  same LLM client accounting path.
- Support fixed worker roles:
  - `plan-reviewer`
  - `code-reviewer`
  - `security-reviewer`
- Each worker is a runner invocation with:
  - role prompt
  - bounded input files or diff
  - explicit output schema
- Allow worker backend selection:
  - Claude for high-risk review
  - Codex for code review cross-check
  - Pi for local low-risk review or classification

Acceptance:

- `/plan` can optionally call plan-reviewer for non-Claude runtimes.
- `/done` can optionally call code-reviewer for touched project diffs.
- Worker failure degrades to a reported warning, not silent success.
- AgentPool and JSON fallback runner usage are visible in the same budget and
  rate-limit counters.

## Data and State

Recommended state layout:

```text
~/.feishu-bridge/
  workflows.db
  journals/
    <session-key>.jsonl
  workflow-artifacts/
    <workflow-id>/
      inputs/
      outputs/

~/.agents/
  skills/
  rules/
  memory/
    sessions/
    lessons.md

<repo>/.agents/
  ctx/
    project-overview.md
    architecture.md
    known-pitfalls.md
    decisions.md
    timeline.md

<repo>/.claude/
  ctx/
    README.md or adapter files during migration
```

Bridge implementation may keep the same files under an explicit
`FEISHU_BRIDGE_HOME`; it must not rely on the active runner workspace as the
state root.

Runtime state layout:

```text
${FEISHU_BRIDGE_HOME:-~/.feishu-bridge}/
  workflows.db
  journals/
    <session-key>.jsonl
  workflow-artifacts/
    <workflow-id>/
      inputs/
      outputs/
```

`workflows.db` tables:

| Table | Purpose |
|---|---|
| `workflow_runs` | command, state, chat/thread, runner identity, created/updated |
| `workflow_steps` | ordered step state and progress |
| `workflow_artifacts` | generated file paths and validation results |
| `workflow_waits` | pending confirmation or selection state, including `expires_at` |

Canonical data rules:

- For ClaudeRunner native commands, Claude Code remains canonical for its own
  transcript, hooks, and `/done` output.
- Bridge journal is canonical only for bridge workflows and non-Claude runners.
- Do not write both Claude-native `/done` and bridge `/done` for the same turn.
- Bridge journal is runtime state, not canonical long-term memory.
- Raw session archives are local memory, not repo-local project context.
- Global reusable lessons and universal rules belong under `~/.agents`.
- Project-specific long-term memory belongs under each repo's `.agents/ctx/`.
- Existing `<repo>/.claude/ctx/` files remain Claude Code adapter/compatibility
  files during migration.
- File relationship policy:
  - `~/.agents` files are canonical after migration.
  - `.claude` skill/script adapter files should be relative symlinks to
    `~/.agents` whenever Claude Code behavior permits.
  - `.claude` wrappers are allowed only for files that need Claude-specific
    behavior.
  - Silent copy is bootstrap-only and must not be a long-lived relationship.
- `~/.agents/AGENTS.md` is canonical for universal rules; Claude-specific
  `CLAUDE.md` is a generated view, symlink, or thin adapter.

Recommended `/done` write routing:

| Output | Canonical target | Notes |
|---|---|---|
| Recent turn journal | `~/.feishu-bridge/journals/` | short-lived runtime context |
| Raw session archive | `~/.agents/memory/sessions/<project>/` | local archive, not default repo content |
| Global lessons | `~/.agents/memory/lessons.md` or `~/.agents/rules/lessons.md` | cross-project reusable knowledge |
| Project ctx | `<repo>/.agents/ctx/*.md` | repo-local project knowledge, suitable for git tracking |
| Claude adapter ctx | `<repo>/.claude/ctx/*` | compatibility only, not the new canonical target |

Session archive migration:

- New bridge `/done` archives write to `$AGENTS_HOME/memory/sessions`.
- Existing Claude archives under `$CLAUDE_HOME/memory/sessions` remain readable.
- During migration, `session-history rebuild/search/read` must index both roots.
- After migration, `$CLAUDE_HOME/memory/sessions` may become an adapter or
  symlink to `$AGENTS_HOME/memory/sessions` if Claude Code behavior is verified.

## Workspace Policy

Current staging may use `/Users/feir/.claude` as Pi workspace because it exposes
existing `CLAUDE.md`, skills, rules, and memory for validation. This is a
temporary migration shortcut.

Long-term defaults:

- `~/.claude` is Claude Code home, not the default workspace for non-Claude
  runners.
- `~/.agents` is the universal skill/rule/script home.
- `~/.feishu-bridge` is bridge runtime state home.
- Pi default workspace should be either:
  - a bridge-managed neutral workspace, for example
    `~/.feishu-bridge/workspaces/default`, or
  - a resolved project repo, for example `/Users/feir/projects/feishu-bridge`.
- Project work should run in the project repo when possible.
- Management/meta tasks may run in a neutral bridge workspace.

Recommended environment variables:

```text
AGENTS_HOME=${AGENTS_HOME:-$HOME/.agents}
CLAUDE_HOME=${CLAUDE_HOME:-$HOME/.claude}
FEISHU_BRIDGE_HOME=${FEISHU_BRIDGE_HOME:-$HOME/.feishu-bridge}
```

Example future Pi provider profile:

```jsonc
{
  "workspace": "/Users/feir/.feishu-bridge/workspaces/default",
  "env_by_type": {
    "pi": {
      "AGENTS_HOME": "/Users/feir/.agents",
      "CLAUDE_HOME": "/Users/feir/.claude",
      "FEISHU_BRIDGE_HOME": "/Users/feir/.feishu-bridge"
    }
  }
}
```

The workspace resolver is responsible for mapping a session or task to a repo
workspace when a project-specific task is detected.

## Safety

- Treat Feishu as a remote command surface.
- Workflow scripts need an allowlist rooted in `~/.agents/skills/*/scripts` and
  approved legacy `.claude` script paths during migration.
- Pi write/bash tools remain disabled by default.
- Workflow file writes happen in bridge code, not model-generated shell.
- `/memory-gc`, `/done`, and global memory writes require owner/allowlist
  permission in group chats.
- Destructive steps require explicit confirmation.
- Model output must be schema-validated before any mutation.

## Test Strategy

Unit tests:

- command registry parsing
- runner-specific command policy
- unsupported command fallback
- workflow state transitions
- JSON validation retry and fail-closed behavior
- confirmation waits and TTL expiration
- prompt budget truncation
- session journal persistence
- owner/allowlist permission checks
- workspace resolver and memory routing
- relative symlink/adapters for `.claude` skill and ctx compatibility
- session-history dual-root indexing during migration

Integration tests:

- ClaudeRunner `/plan` pass-through under `workflow.intercept=auto`
- Pi `/plan`, confirm, verify files
- Pi `/memory-gc --dry-run` with fixture lessons
- Pi `/done` with fixture journal and temp agent homes
- session-history search finds both legacy Claude archives and new bridge
  archives
- restart recovery for pending workflow

Manual staging tests:

1. Switch staging bot to Pi.
2. Send unsupported command `/save <url>` and verify explicit rejection.
3. Send `/plan add small test feature`.
4. Confirm persistence.
5. Send `/memory-gc --dry-run`.
6. Run a short normal conversation, then `/done`.
7. Switch staging bot to Claude and verify `/plan` is passed through.

## Open Decisions

| Decision | Default |
|---|---|
| ClaudeRunner bridge workflow interception | `auto` means no interception for Claude-native skills |
| Canonical skill source | `~/.agents/skills`, with `~/.claude/skills` as adapter during migration |
| `~/.agents` vs `~/.claude` file relationship | `~/.agents` canonical; `.claude` relative symlink by default, wrapper only when required |
| Universal rules source | `~/.agents/AGENTS.md` canonical; `CLAUDE.md` generated/symlink/adapter |
| Non-Claude default workspace | neutral bridge workspace or resolved repo, not `~/.claude` |
| Project memory target | `<repo>/.agents/ctx` |
| Bridge journal role | runtime context only, not long-term memory |
| Session archive physical root | `$AGENTS_HOME/memory/sessions`; migration-period `session-history` reads both agents and Claude roots |
| Non-MVP skill fallback | Claude pass-through; Pi/Codex explicit unsupported response |
| Workflow DB path | use existing bridge state dir, not Pi session dir |
| Waiting confirmation TTL | declared per workflow: `/plan` 7d, `/memory-gc` 24h, `/done` 2h |
| `/done` auto-commit | defer behind explicit config flag |
| Review worker backend | Claude/Codex by default, Pi opt-in after dogfood |
| Full skill parser | defer; use `SKILL.md` frontmatter plus `workflow.yaml` steps |
| JSON fallback runner | disabled by default; configurable per workflow |

## Self-check

- Completeness: covers `~/.agents`, symlink/adapters, session-history
  migration, workspace policy, memory routing, command policy, Claude
  pass-through, journal before `/plan`, JSON fallback, per-workflow TTL,
  permissions, `/status`, `/plan`, `/memory-gc`, `/done`, and AgentPool.
- Consistency: bridge workflow is a non-Claude compatibility layer; ClaudeRunner
  native skills remain default.
- Executability: every phase has concrete files/modules, validation commands,
  and acceptance criteria.
- Auditability: baseline commit, source `.claude` paths, proposed `~/.agents`
  paths, and migration defaults are recorded.
- Risk boundary: write/bash tools, memory mutation, auto-commit, and fallback
  runner use remain explicitly gated.
