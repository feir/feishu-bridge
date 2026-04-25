# Design: agents-canonical-migration

## Current State

Observed on 2026-04-20:

```text
~/.agents/skills/
  plan/
    SKILL.md
    workflow.yaml
    schemas/plan-draft.schema.json
    scripts/spec-resolve.py
    scripts/spec-write.py
    prompts/draft.md
  done/
    SKILL.md
    workflow.yaml
  memory-gc/
    SKILL.md
    workflow.yaml

~/.claude/skills/
  plan/SKILL.md
  done/SKILL.md
  done/scripts/*
  done/assets/extraction-schema.json
  memory-gc/SKILL.md
  memory-gc/scripts/*
  save/*
  research/SKILL.md
  wiki/SKILL.md
  idea/SKILL.md
  retro/SKILL.md
  investigate/SKILL.md
  acpx/SKILL.md
  app-research/SKILL.md
  config-drift/SKILL.md
  security-review/SKILL.md
  paddleocr-*/...
  tdd-workflow/...
  things/...
```

Bridge code already has path policy in `feishu_bridge/paths.py`:

- `agents_home()`
- `claude_home()`
- `resolve_skill_source(name)` canonical-first
- `session_archive_root()`
- `project_ctx_dir(repo)`
- `legacy_project_ctx_dir(repo)`

This migration should use those existing APIs rather than adding another path resolver.

`feishu_bridge/workflows/agent_pool.py` 确立了 bridge 的 agent 语义基线：

> "does not create Claude Code subagents; it gives non-Claude runtimes a common
> surface for bounded reviewer prompts with explicit failure reporting and shared
> budget tracking."

这决定了 agent 迁移必须采用 dual-artifact（canonical prompt + Claude adapter）结构，见下方 Phase 2.5。

## Execution Environment

- 执行者：Captain，在**交互式 Claude CLI session** 内运行；
- 工作根目录：Captain 可在 `~/` 或 `~/.claude` / `~/.agents` 任一目录发起，路径一律用绝对路径或 `$AGENTS_HOME` / `$CLAUDE_HOME` 变量；
- bridge daemon 状态：在 Phase 1、Phase 2、Phase 2.5 开始前停止（`launchctl unload ~/Library/LaunchAgents/com.feishu-bridge.*.plist` 或等效命令），每个 Phase 完成并 smoke 后再恢复；
- 非交互式回放：本次迁移**不**支持 bridge 非交互模式回放（受 `~/.claude/` sensitive-file guard 限制）。自动化脚本若未来需要，需另起独立 shell 版本，不在本 change 范围。

## Target Layout

For a migrated skill:

```text
$AGENTS_HOME/skills/<name>/
  SKILL.md
  workflow.yaml               # only if bridge workflow exists
  scripts/
  schemas/
  prompts/
  assets/

$CLAUDE_HOME/skills/<name> -> ../.agents/skills/<name>  # conceptual; actual relative path must be valid
```

**Phase 0.6 结论：dir symlink 不可行**（见 `symlink-spike.md` Verdict）。`Skill(skill:"__spike_target__")` 返回 `Unknown skill`，证明 Claude Code skill loader 扫描 `~/.claude/skills/` 时不跟随 symlink。Phase 1 一律采用 **thin wrapper 模式 A**；Phase 2.5 agent 维持 dual-artifact。

Thin wrapper 模式 A（默认）：

```text
$AGENTS_HOME/skills/<name>/
  SKILL.md                    # runner-neutral stub（~1KB，含 runners: 块 → workflow.yaml）
  workflow.yaml               # 状态机
  scripts/<file>              # runner-共享脚本（唯一 source）

$CLAUDE_HOME/skills/<name>/
  SKILL.md                    # Claude 专属 body（~7KB，完整 Claude CLI 执行步骤）
                              # 与 canonical SKILL.md 形态不同，两侧分离，不做 byte-identical drift check
  scripts/<file>              # file-level symlink → $AGENTS_HOME/skills/<name>/scripts/<file>
                              # （Phase 1.0 已实证 Claude 运行时对 file-level symlink 透明）
```

**重要 nuance**：canonical SKILL.md 是 runner-neutral stub（~1KB），adapter SKILL.md 是 Claude 专属 full body（~7KB），两者 **形态不同**，迁移目标**不是**让 SKILL.md byte-identical。真正需要一致的是：scripts 唯一 source（canonical），adapter 通过 file-level symlink 引用。drift check 针对 scripts symlink 状态（adapter `scripts/<file>` 必须是 symlink 且 `resolve()` 指向 canonical），而不是 SKILL.md。

Thin wrapper 模式 B（保底）：仅当某 skill 含 canonical 无法承载的 Claude 专属脚本时启用——adapter `scripts/<file>` 为实文件（不 symlink），canonical 下不复制。drift check：adapter 独占脚本 enumerate 并允许存在。Phase 1.x 子任务若采用模式 B，必须在 manifest `notes` 中标注理由。

## Migration Strategy

### Phase 0 — Inventory and Guardrails

Deliverables:

- `scripts/agents-inventory.py` or equivalent bridge utility.
- Inventory report for `~/.agents/skills`、`~/.claude/skills`、`~/.claude/agents`、`~/.claude/rules`、`~/.claude/output-styles`。
- Classify each skill:
  - canonical-ready
  - duplicate-copy
  - claude-native-only
  - bridge-workflow
  - tool-heavy
  - unsafe-to-symlink

Checks:

- Detect duplicate `SKILL.md` files for the same skill.
- Detect scripts present only under `~/.claude`.
- Detect absolute `~/.claude` references.
- Detect symlink loops.
- Detect missing `workflow.yaml` for bridge workflow skills.

#### Phase 0.5 — Agent Dispatch Capability Matrix

Pi/Codex 不持有 Claude Code subagent 能力（见 `agent_pool.py` docstring）。agent 目录的迁移前置于此能力矩阵，否则无法决定 canonical 结构。

Deliverables:

- `.specs/changes/agents-canonical-migration/capability-matrix.md`，枚举 **全部** `~/.claude/agents/*.md` 文件（当前共 7 个，以 Phase 0.1 inventory.md 为准）并归档为三类：
  - **(A) Claude-only**：保留在 `~/.claude/agents/`，不迁移。
    - 确认项：`general-purpose`（Claude CLI 内置）、`Explore`（Claude 内置）、`statusline-setup`（Claude 内置）、`loop-operator`（frontmatter 含 `tools:` 白名单 + model=sonnet，Claude CLI only）。
  - **(B) bridge AgentPool-reusable**：`ALLOWED_REVIEWER_ROLES` 覆盖（`plan-reviewer`、`code-reviewer`、`security-reviewer`）——走 dual-artifact，Phase 2.5 迁移。
  - **(C) 待定（逐项决策）**：`build-error-resolver`、`database-reviewer`、`e2e-runner`。每项评估：是否用于自动化 pipeline？bridge 是否需要从 Pi 调用？默认先不迁，Phase 2.5 之后再决策。

Checks:

- 每条条目注明 prompt 长度、tools 白名单、是否依赖 Claude Code 独有 tool；
- 矩阵签字后 Phase 2.5 才可启动。

#### Phase 0.6 — Symlink Compatibility Spike

验证 Claude Code skill/agent loader 对 `~/.claude/skills/<name>` / `~/.claude/agents/<name>.md` 为 relative symlink 时是否正确解析。

Deliverables:

- 临时建立 `~/.claude/skills/__spike__` → `~/.agents/skills/__spike_target__`，SKILL.md 含 `/spike` slash；
- 在 Claude CLI 验证 slash 注册、scripts 相对路径、plugin namespace 均正常；
- 对 `~/.claude/agents/*.md` 重复同样试验；
- 输出 verdict：若 symlink 被 loader 正常识别 → Phase 1 使用 symlink；否则退化为 thin wrapper。
- **结论（2026-04-20）**：`Skill(skill:"__spike_target__")` 返回 `Unknown skill`，loader 不跟随 symlink。**Phase 1 选用 thin wrapper**（模式 A 默认，B 保底）；Phase 2.5 维持 dual-artifact。详见 `symlink-spike.md` §"✗ 已确认"。

#### Phase 0.0 — Bootstrap Freeze

`/plan` 与 `/done`（含 `session-history`）自身都是迁移对象，Phase 0 必须先冻结，否则迁移过程中写 Spec-Check evidence 会踩半迁移状态。

Actions:

- 快照 `/plan` skill：`cp -a ~/.agents/skills/plan ~/.agents/skills/.plan.snapshot.<YYYYMMDD>`；
- 快照 `/done` skill：`cp -a ~/.claude/skills/done ~/.claude/skills/.done.snapshot.<YYYYMMDD>`（canonical 尚未完全建立，先从 Claude 侧快照）；
- 快照 `session-history`：`cp -a ~/.claude/bin/session-history ~/.claude/bin/.session-history.snapshot.<YYYYMMDD>`；
- **迁移禁令**：Phase 1.2（迁 `/done`）开始到 Phase 2 全部验证通过前，**禁止调用 `/done`**；Spec-Check result 字段由 Captain 在 tasks.md 手动写入；
- **替代 Journal**：活动记录写入 `.specs/changes/agents-canonical-migration/migration-log.md`（非 session archive），记录每个 Phase 的操作、观察、回退点；
- 迁移期间禁止修改 `~/.agents/skills/plan` 下文件；若需更新 tasks.md，Captain 手动 Edit，不走 `spec-write.py`。

恢复条件：Phase 2 Spec-Check 标记 PASS 后，恢复 `/done` 与 `session-history` 的常规调用；`migration-log.md` 在 Phase 7.3 归档时并入 session archive。

### Phase 1 — Canonicalize Universal Skills

Scope:

- `plan`
- `done`
- `memory-gc`

Actions:

- Copy missing scripts/assets/schemas/prompts from `~/.claude/skills/<name>` to `~/.agents/skills/<name>`.
- Preserve file modes for executable scripts.
- Normalize scripts to read:
  - `AGENTS_HOME=${AGENTS_HOME:-$HOME/.agents}`
  - `CLAUDE_HOME=${CLAUDE_HOME:-$HOME/.claude}`
- Remove hardcoded `~/.claude` where behavior should be runner-neutral.
- Keep Claude-only behavior behind explicit `CLAUDE_HOME` adapter paths.
- Convert `~/.claude/skills/<name>` into thin wrapper（Phase 0.6 empirical evidence ruled out dir symlink — see `symlink-spike.md`）。

Validation:

- `resolve_skill_source("plan")` returns `$AGENTS_HOME/skills/plan`.
- `resolve_skill_source("done")` returns `$AGENTS_HOME/skills/done`.
- `resolve_skill_source("memory-gc")` returns `$AGENTS_HOME/skills/memory-gc`.
- Claude Code can still discover the three skills.
- Bridge `/plan`, `/done`, `/memory-gc --dry-run` tests still pass.

### Phase 2 — Memory and Session Script Migration

Scope:

- `~/.claude/bin/session-history`
- `done/scripts/session-done-apply.sh`
- `done/scripts/session-done-format.py`
- `done/scripts/memory-anchor-sync.sh`
- `done/scripts/spec-archive.sh`
- `memory-gc/scripts/memory-gc-*.sh`

Actions:

- Make scripts dual-read during migration:
  - primary `$AGENTS_HOME/memory/sessions`
  - fallback `$CLAUDE_HOME/memory/sessions`
- Make writes canonical:
  - sessions archive -> `$AGENTS_HOME/memory/sessions`
  - project ctx -> `<repo>/.agents/ctx`
  - legacy ctx adapter -> `<repo>/.claude/ctx` symlink or generated view
- Add explicit dry-run mode where absent.

Validation:

- `session-history` finds both old and new archives.
- `/done` archive written by bridge is visible to search（Phase 2 Spec-Check PASS 后才解冻 `/done`，此前用 migration-log.md 代替）。
- `memory-gc --dry-run` reads canonical scripts from `~/.agents`.
- No script writes new session archives into `~/.claude/memory/sessions`.
- `session-history rebuild` 全量重建 `index.jsonl`（atomic rename 写入 `$AGENTS_HOME/memory/sessions/index.jsonl`）；entries schema 升级为包含 `root: "agents"|"claude"` 标签字段；旧 Claude 根条目保留但标记 `root: "claude"`，便于 sunset 时统计。
- `session-history` 源码已具备 `AGENTS_SESSIONS_DIR` + `CLAUDE_SESSIONS_DIR` 双根遍历能力（见文件 line 22-30）；Phase 2 需要做的是：(a) 写路径改为 canonical；(b) entries 加 root 字段；(c) 读路径保持双根。
- 关键字检索在迁移前后命中率不下降（取迁移前 5 个高频关键词做基线）。

### Phase 2.5 — Agent Role Canonicalization

前置条件：Phase 0.5 capability-matrix.md 已签字。

Scope（仅 B 类：`AgentPool` 复用 reviewer 角色）：

- `plan-reviewer`
- `code-reviewer`
- `security-reviewer`

Actions:

- **2.5.0 Pre-extraction baseline**：对 3 个 B 类 reviewer，捕获：
  - `sha256(normalized_body)`：`<role>.md` 去除 frontmatter 后，统一行尾符 + strip 首尾空行 + 合并连续空行 + lowercase ASCII（仅用于 hash）后的 hash；
  - frontmatter metadata 完整快照（`name` / `description` / `tools` / `model` / `effort` / 其它字段）；
  - 写入 `.specs/changes/agents-canonical-migration/evidence/agent-prompt-baseline.json`。

- **Extract canonical prompt**：
  - 将 `~/.claude/agents/<role>.md` 的 body（去掉 frontmatter，保留原始格式，不做 lowercase）提取为 `~/.agents/agents/<role>/prompt.md`；
  - 同步写 `~/.agents/agents/<role>/meta.yaml` 记录 Claude frontmatter 中非 tools 的运行时 hint（如 `model`、`effort`、`maxTurns`）。

- **Refactor Claude adapter**：
  - `~/.claude/agents/<role>.md` 保留完整 frontmatter（含 `tools:` 白名单）；
  - body 替换为：`<!-- Canonical prompt: $AGENTS_HOME/agents/<role>/prompt.md -->` + 直接包含 canonical prompt 内容的拷贝（因为 Claude Code 对 agent body 不做动态 include；通过 loader contract 保证一致性）。

- **AgentPool loader contract**（Phase 2.5 的硬要求）：
  - 新增函数 `load_reviewer_prompt(role: str) -> str`，位于 `feishu_bridge/workflows/agent_pool.py`；
  - **优先级**：canonical path `$AGENTS_HOME/agents/<role>/prompt.md` 存在 → 读 body 原文；
  - **Fallback**：canonical 不存在时读 `$CLAUDE_HOME/agents/<role>.md` 并 strip frontmatter（YAML `---` 分隔）；
  - **拼接**：`AgentPool._wrap_prompt(task)` 内 `final_prompt = load_reviewer_prompt(task.role) + "\n\n" + task.prompt`（canonical prompt 在前，task prompt 在后）；
  - **错误模式**：role 不在 `ALLOWED_REVIEWER_ROLES` → 抛 `ValueError`；canonical 与 fallback 都不存在 → 抛 `FileNotFoundError` 并 log 具体搜索路径（fail loud，不 silent fallback 到空 prompt）；
  - **frontmatter 解析**：使用已有 YAML 解析器（PyYAML 已在 deps）；malformed frontmatter → 抛 `ValueError` 并给出文件名 + 行号。

- Claude-only agents（A 类）**零改动**。

Validation:

- **Phase 2.5.4 hash 对比**：
  - `sha256(normalized_body($AGENTS_HOME/agents/<role>/prompt.md))` 与 baseline 中 `<role>` 项的 hash 严格相等；
  - 若有意图修改，Captain 签字更新 baseline.json 的 `intentional_delta` 字段并记录 reason。
- Claude Code 仍可用 Task tool 调用三个 reviewer agent，行为与迁移前一致。
- bridge `AgentPool.run()` 单测覆盖：
  - canonical-first path hit；
  - canonical missing → fallback body extraction；
  - malformed frontmatter → `ValueError` raised；
  - missing role file both sides → `FileNotFoundError` raised；
  - unsupported role → `ValueError` raised；
  - prompt 拼接顺序为 `role_prompt + "\n\n" + task_prompt`。
- drift check 不把 `~/.claude/agents/` 下 A 类 agent 报告为"应迁移未迁移"。

### Phase 2.6 — Rules and CLAUDE.md Migration

Scope:

- `~/.claude/rules/*.md`
- `~/.claude/CLAUDE.md`

Actions:

- `~/.agents/rules/*.md` 成为 canonical rules 真源；
- `~/.claude/rules/<file>.md` 改为 **thin wrapper**（per-file byte-identical copy，drift 检查 SHA256）。rules 是纯文本规则注入（不同于 skill 的 slash 注册），Claude Code 只做文件读取，因此 copy 模式天然可行；symlink 亦无必要（Phase 0.6 已证明 skill 层不跟随 symlink，rules 层行为未独立验证，保守起见不依赖）。
- `~/.claude/CLAUDE.md` 重构为 adapter：
  - 顶部 include `~/.agents/AGENTS.md` canonical 内容（或由 generator 拼接）；
  - 尾部保留 Claude-only 段（`# auto memory` 路径、Claude CLI 专用的 output-style 说明等）。
- 非 Claude runner 通过 `$AGENTS_HOME/AGENTS.md` + `$AGENTS_HOME/rules/*.md` 消费规则。

Validation:

- 新 Claude CLI session 启动后 `rules/*.md` 全部注入 system prompt（抽样 3 条 lessons 作金丝雀）。
- Pi staging 读 `$AGENTS_HOME/AGENTS.md` 成功。
- **Phase 2.6 post-validation（cross-phase 回归）**：由于 `session-done-apply.sh` 等脚本硬编码 `~/.claude/bin/skill-validate` 与 `~/.claude/bin/session-history`，Phase 2.6 改 rules/CLAUDE.md 后必须回跑 Phase 1/2 smoke：
  - 开启**新** Claude CLI session（以免缓存失效）；
  - 重跑 `/plan --dry-run`、`/done --dry-run`、`/memory-gc --dry-run`；
  - 重跑 `session-history search <baseline_keyword>`、`session-history stats`；
  - grep canonical `~/.agents/AGENTS.md`、`~/.agents/rules/*.md`、`~/.claude/CLAUDE.md` 里的陈旧绝对路径 `~/.claude/bin/*`：adapter-aware 改写，不应再直接硬编码；
  - 任一检查失败 → Phase 2.6 Spec-Check BLOCK，回滚 rules/CLAUDE.md adapter。

Rollback-specific（补强 Rollback 段）：

- Phase 2.6.0 pre-action backup：
  - `cp ~/.claude/CLAUDE.md $AGENTS_HOME/adapters/claude/migration-backups/<ts>/CLAUDE.md`；
  - `cp -a ~/.claude/rules $AGENTS_HOME/adapters/claude/migration-backups/<ts>/rules/`；
  - manifest 记录 old/new sha256、symlink 创建命令、restore 命令（逐文件）；
- Phase 2.6 rollback-smoke：临时目录 dry-run restore → 3 条 lesson canary 仍注入。

### Phase 3 — Bridge Integration Tests

Scope:

- `feishu_bridge.paths.resolve_skill_source()` canonical-first 行为验证
- drift script 单测（使用临时 `$AGENTS_HOME` / `$CLAUDE_HOME`）
- bridge workflow tests：`test_workflow_registry.py`、`test_commands_workflow_wiring.py`、`test_done_workflow.py`、`test_memory_gc_workflow.py`、`test_agent_pool.py`
- Claude adapter smoke：迁移后的 skills / B 类 agents 在 `$CLAUDE_HOME` 下可 discovery 且指向 canonical

Validation:

- 单测通过率 100%；smoke 项目对照 evidence-matrix；drift 报告 0 duplicate。

### Phase 4 — Fallback Skill Migration

Scope:

- `save`
- `research`
- `wiki`
- `idea`
- `retro`

Actions:

- Move or copy to `$AGENTS_HOME/skills/<name>`.
- Keep Claude adapter symlink/wrapper.
- Update bridge command registry fallback metadata to point at canonical skill.
- Decide per skill whether bridge workflow support is planned or remains unsupported for Pi.

Validation:

- Claude native usage continues to work.
- Pi/Codex get explicit unsupported messages for non-migrated workflows.
- Drift check sees no duplicate mutable copies.

### Phase 5 — Tool-Heavy Skill Migration

Scope:

- OCR skills
- `things`
- `security-review`
- `config-drift`
- `app-research`
- `acpx`
- `investigate`
- `tdd-workflow`

Actions:

- Migrate one skill at a time.
- Preserve dependencies and requirements files.
- Keep large assets out of prompts.
- Add smoke commands only when dependencies are installed.

Validation:

- Skill-specific smoke tests pass or are documented as skipped.
- No Pi default prompt growth.

### Phase 6 — Workspace Default Migration

After canonical skills are stable:

- Make non-Claude runner default workspace use `default_runner_workspace(agent_type)`.
- Keep staging explicit workspace until tested.
- Add migration docs for users currently relying on `~/.claude` as Pi workspace.

## Drift Detection

Add a check that emits a table:

```text
skill       agents     claude            adapter_type   status
plan        dir        wrapper(SKILL.md) mode-A         ok
done        dir        wrapper(SKILL.md) mode-A         ok
memory-gc   dir        wrapper(SKILL.md) mode-A         ok
save        missing    dir               pre-migration  pending
```

Fail conditions:

- 模式 A 下 `$CLAUDE_HOME/skills/<name>/scripts/<file>` 不是 symlink，或 `resolve()` 未指向 `$AGENTS_HOME/skills/<name>/scripts/<file>`
- 模式 A 下同名 script 同时为 adapter 实文件 + canonical 实文件（drift；adapter 应为 symlink）
- 模式 B 下 adapter 独占 scripts 未在 manifest `notes` 列出
- SKILL.md 两侧分离设计下不做 byte-identical drift check；但 canonical `SKILL.md` 必须存在且可被 workflow.yaml 引用
- `workflow.yaml` exists only in `~/.claude`
- executable script exists only in `~/.claude` for a migrated bridge workflow skill
- 任何 `~/.claude/skills/<name>` 被重新引入 symlink（Phase 0.6 已排除，drift check 见 `symlinks` + 新增 no-skill-symlink regression check）

## Rollback

Before replacing any `~/.claude/skills/<name>` directory:

1. Write manifest:

```text
$AGENTS_HOME/adapters/claude/migration-manifests/<timestamp>-<name>.json
```

2. Move old directory to:

```text
$AGENTS_HOME/adapters/claude/migration-backups/<timestamp>/<name>
```

   （**不**放 `$CLAUDE_HOME/skills/` 下——Claude Code skill loader 会扫该目录，backup 会被误识别为 skill namespace。）

3. Create adapter symlink/wrapper.

Rollback restores the backup directory from `$AGENTS_HOME/adapters/claude/migration-backups/` and removes the adapter.

### Per-phase rollback granularity

- Phase 1/2.5 每个 skill / agent 都写独立 manifest，可单 skill 回滚；
- Phase 2 脚本修改走 git 分支，回滚 = `git checkout <pre-migration-sha>`；
- Phase 2.6 CLAUDE.md / rules/ 回滚路径：manifest 记录 symlink 创建命令，反向删除 symlink 并恢复 backup 目录。

## Bridge Daemon Drain / Restart Protocol

Phase 1、Phase 2、Phase 2.5 执行前后运行以下 checklist：

1. **Pre-migration**
   - `launchctl list | grep feishu-bridge` 确认当前运行态；
   - 通知活跃 bot（飞书自动消息或 Captain 手工）进入维护；
   - `launchctl unload ~/Library/LaunchAgents/com.feishu-bridge.*.plist`；
   - 等待进程完全退出（`pgrep -f feishu_bridge` 为空）。

2. **Migration**
   - 执行 Phase 任务；
   - 所有改动 git commit；
   - 运行 smoke tests（`pytest tests/unit/test_workflow_registry.py` 等）。

3. **Post-migration**
   - `launchctl load ~/Library/LaunchAgents/com.feishu-bridge.*.plist`；
   - 通过飞书 bot 执行 `/plan --dry-run`、`/done --dry-run`、`/memory-gc --dry-run` 作为 live smoke；
   - 观察 30 分钟 journal 无 canonical path 相关错误后标记 Phase 完成。

## Dual-Read Fallback Sunset

Phase 2 脚本引入 `$AGENTS_HOME/memory/sessions` 主路径 + `$CLAUDE_HOME/memory/sessions` fallback 双读。

Sunset 条件（三者均满足后移除 fallback 读取代码）：

- **task 6.3 staging validation PASS**（原"Phase 5 workspace default 完成"——按 tasks.md 最新编号，Phase 6 才是 Workspace Default；绑定到具体 task ID 避免 phase 重编号时再次错位）；
- 旧 `$CLAUDE_HOME/memory/sessions` 最新文件早于 60 天；
- `session-history stats` 在去除 fallback 后总计数不减。

Sunset 追踪：在 tasks.md `7.4 Fallback sunset` 节记录当前状态。

## Open Decisions

- **(已收敛)** `CLAUDE.md` 形态：handwritten adapter，顶部 include/拼接 canonical `AGENTS.md`，尾部保留 Claude-only 段（见 Phase 2.6）。
- **(已收敛)** agent 迁移策略：dual-artifact（canonical prompt + Claude adapter with frontmatter/tools），仅 B 类 reviewer 角色迁移，A 类 Claude-only 零改动（见 Phase 0.5 / 2.5）。
- **(已收敛)** `.migration-backup` 位置：`$AGENTS_HOME/adapters/claude/migration-backups/`，移出 `$CLAUDE_HOME/skills/` 扫描范围。
- **(已收敛)** 执行环境：Captain 交互式 Claude CLI；bridge daemon 停机迁移。
- **(新 / 待决策)** Phase 1 scope 校准：2026-04-20 Phase 1.1 pre-flight 实测 `plan` / `done` / `memory-gc` 三 skill canonical/adapter scripts 零重叠（详见 `migration-log.md` 同日条目）。模式 A "adapter `scripts/<file>` → canonical file-level symlink" 在当前代码库中**无可操作对象**。需在两条路径中选择：
  - **narrow**：Phase 1 降级为 manifest-only（写 `adapter_type`），plan 归 `claude-adapter`（canonical 已完整），done/memory-gc canonical 保留空 workflow stub，adapter scripts 保留实文件；drift check §Fail conditions 需放宽（允许 adapter 独占 scripts 无对应对）。
  - **broad**：把 13 个 adapter 脚本（9 done + 4 memory-gc）重写为 runner-neutral，迁 canonical，adapter 转 file-level symlink；需同步抽象 `~/.claude/bin/*` 依赖，可能超 Phase 1 单 change max=3 约束。
  - **混合**：逐脚本决策 runner-neutral vs Claude-only。
- **(遗留)** 项目级 `<repo>/.claude/ctx` symlink 是永久保留还是 Phase N 后删除。
- **(遗留)** `save/research/wiki/idea/retro` 是否成为 bridge workflow（Phase 3 决策）。
- **(遗留)** migration utility 放 feishu-bridge repo 还是 `$AGENTS_HOME/adapters/bridge/scripts`。
- **(遗留)** project-scoped memory（`~/.claude/projects/<project_id>/memory/`）如何映射到 runner-neutral layout——当前 Phase 不触碰，待单独 change 决策。

