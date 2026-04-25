# Tasks: agents-canonical-migration

> **Execution environment**：全部任务由 Captain 在交互式 Claude CLI 执行。bridge daemon 在 Phase 1 / 2 / 2.5 开始前停止（`launchctl unload ...`），Phase 结束后恢复。
> **Bootstrap**：Phase 0.0 冻结 `/plan` skill 快照；迁移期间不通过 `spec-write.py` 修改 tasks.md。
> **Spec-Check result 字段**：每个 Phase 完成后在对应小节末尾写 `- result: PASS|WARN|BLOCK`，以便 spec-archive 识别。

## Phase 0 — Inventory and Safety Rails

- [ ] 0.0 Bootstrap freeze `/plan` + `/done` + `session-history`
  - 快照 `/plan`：`cp -a ~/.agents/skills/plan ~/.agents/skills/.plan.snapshot.<YYYYMMDD>`
  - 快照 `/done`：`cp -a ~/.claude/skills/done ~/.claude/skills/.done.snapshot.<YYYYMMDD>`
  - 快照 `session-history`：`cp -a ~/.claude/bin/session-history ~/.claude/bin/.session-history.snapshot.<YYYYMMDD>`
  - **迁移期间禁令**：Phase 1.2 开始到 Phase 2 Spec-Check PASS 前，禁止调用 `/done`；Spec-Check result 由 Captain 手动写入 tasks.md
  - **替代 journal**：创建 `.specs/changes/agents-canonical-migration/migration-log.md`，记录每 Phase 操作、观察、回退点
  - 迁移期间禁止修改 `~/.agents/skills/plan/*`；tasks.md 若需改动走手动 Edit，不走 `spec-write.py`
  - 解冻触发：Phase 2 Spec-Check result=PASS 后

- [ ] 0.1 Capture current skill / agent / rules / output-styles inventory
  - Inputs: `$AGENTS_HOME/skills`, `$CLAUDE_HOME/skills`, `$CLAUDE_HOME/agents`, `$CLAUDE_HOME/rules`, `$CLAUDE_HOME/output-styles`
  - Output: `.specs/changes/agents-canonical-migration/inventory.md`
  - **必须枚举实际文件**（不依赖设计假设）：
    - `~/.claude/skills/done/scripts/*` 现有 9 个：`memory-anchor-sync.sh`、`memory-gc-check.sh`、`session-done-apply.sh`、`session-done-commit.sh`、`session-done-format.py`、`spec-archive-validate.py`、`spec-archive.sh`、`spec-check-write.py`、`stale-ctx-check.sh`
    - `~/.claude/skills/memory-gc/scripts/*` 现有 4 个：`memory-gc-archive.sh`、`memory-gc-maintain.sh`、`memory-gc-route.sh`、`memory-gc-stats.sh`
    - `~/.claude/agents/*.md` 现有 7 个：`build-error-resolver`、`code-reviewer`、`database-reviewer`、`e2e-runner`、`loop-operator`、`plan-reviewer`、`security-reviewer`（designer 已于 2026-04-20 移除，不纳入迁移）
  - inventory.md 作为 Phase 2.1/2.2 脚本列表的真源
  - 每 Phase 完成后回来更新 status 列

- [ ] 0.2 Add drift detection script
  - **Canonical path**: `scripts/agents-skill-drift.py` in `feishu-bridge` repo（CI 与 `tests/unit/` 集成，与 `paths.py` 共版本；Pi 运行时发现可通过 `$AGENTS_HOME/adapters/bridge/scripts/` 软链二次暴露）
  - Detect: duplicate mutable dirs, missing canonical scripts, symlink loops, hardcoded `~/.claude`, CLAUDE.md/rules adapter drift, session-history index root divergence, backup dir 出现在 `$CLAUDE_HOME/skills/` 下, executable bit 丢失, agent dual-artifact body hash 不匹配
  - PASS: reports current migration state without modifying files

- [ ] 0.3 Define adapter manifest format
  - Path: `$AGENTS_HOME/adapters/claude/migration-manifests/*.json`
  - Include: skill/agent name, backup path, adapter path, canonical path, timestamp, rollback commands

- [ ] 0.4 Confirm canonical metadata rule
  - `SKILL.md` frontmatter is metadata truth source
  - `workflow.yaml` is execution/TTL truth source only
  - Agent canonical prompt 是 `~/.agents/agents/<role>/prompt.md`；Claude adapter 含 frontmatter + `tools:`

- [ ] 0.5 Pi agent dispatch capability matrix
  - 产出：`.specs/changes/agents-canonical-migration/capability-matrix.md`
  - **必须枚举 Phase 0.1 inventory.md 列出的全部 7 个 agent**，每个打上 A / B / C 标签
  - 默认分类（Phase 0.5 验证并签字）：
    - **A 类（Claude-only，保留）**：`general-purpose` / `Explore` / `statusline-setup`（Claude 内置）、`loop-operator`
    - **B 类（dual-artifact 迁移）**：`plan-reviewer`、`code-reviewer`、`security-reviewer`（`ALLOWED_REVIEWER_ROLES` 覆盖）
    - **C 类（逐项决策）**：`build-error-resolver`、`database-reviewer`、`e2e-runner`——默认先不迁
  - 依据：`feishu_bridge/workflows/agent_pool.py` 的 `ALLOWED_REVIEWER_ROLES` + 每个 agent frontmatter 的 `tools:` 列表
  - 矩阵签字后 Phase 2.5 方可启动

- [ ] 0.6 Symlink compatibility spike
  - 临时建 `~/.claude/skills/__spike__` → `~/.agents/skills/__spike_target__`；在 Claude CLI 验证 slash 注册与 scripts 解析
  - 对 `~/.claude/agents/__spike__.md` symlink 做同样验证
  - Verdict 写入 `.specs/changes/agents-canonical-migration/symlink-spike.md`，**格式为 Decision Matrix**（行 × 列）：
    - 行：skill directory symlink / agent file symlink / scripts/ 相对路径解析 / slash 注册 / plugin namespace / relative vs absolute symlink
    - 列：works as-is / works with caveat / fails → wrapper required
  - Phase 1.1 / 1.2 / 1.3 / 2.5.2 子任务明确引用该矩阵对应行决定实现方式
  - 若 symlink 失败 → Phase 1/2.5 降级为 thin wrapper 模式

- [ ] 0.7 Execution environment declaration
  - 在 inventory.md 顶部记录 bridge daemon stop/restart 命令
  - 列出本地 LaunchAgent plist 路径与 `pgrep -f feishu_bridge` 校验

- [x] Phase 0 Spec-Check
  - 2026-04-20 Round 1 — Captain self-review + advisor cross-check
  - 已交付 deliverables：
    - `migration-log.md`（替代 journal，记录 0.0–0.6）
    - `inventory.md`（§0 exec env + §1–7 skills/agents/rules/output-styles/bin/memory/adapters）
    - `scripts/agents-skill-drift.py`（8 checks，`--list` 枚举契约，`--json` 输出）
    - `tests/unit/test_agents_skill_drift.py`（5 用例全 PASS）
    - `SCHEMA.md` + 2 samples（`~/.agents/adapters/claude/migration-manifests/`）
    - `metadata-rule.md`、`capability-matrix.md`（7 个 agent 全分类 A=1/B=3/C=3）
    - `symlink-spike.md`（Phase 0.6 verdict：skill dir symlink 经 `Skill(skill:"__spike_target__")` 实测 `Unknown skill`，RULED OUT → Phase 1 改用 thin wrapper 模式 A；design.md / tasks.md Phase 1.1/1.2/1.3 已同步）
    - spike artefacts 已清理（4 个 path rm）
  - Drift 检查全量通过（`backup_in_skills=OK` / `hardcoded_claude_paths=OK` / `duplicate_skills=WARN×3` 是 mid-migration 预期 / `executable_bits=OK` 等）
  - Gated items 状态（2026-04-20 Captain 决策后）：
    1. **~~CI wiring~~**：**降级为 infra backlog（Captain 选项 iii）**。feishu-bridge repo 当前无 pytest CI（`.github/workflows/` 只有 publish.yml）；drift 单测 5/5 本地 PASS，靠 pre-commit / 手动 pytest 保护。开 test CI 归为独立改进项，不阻 Phase 1
    2. **~~Phase 0.5 Captain signoff~~**：**已清**。2026-04-20 Captain 确认 A=1/B=3/C=3 分类，接受 `plan-reviewer` / `code-reviewer` body 的 `TodoWrite` 改写代价；capability-matrix.md Sign-off 段已更新
    3. **Rules 层 symlink 未独立验证**：保持保守 per-file copy 策略（已写入 design.md + tasks.md Phase 2.6），不阻 Phase 1
  - result: WARN → **effectively cleared for Phase 1 start**（gated items 均已处置，仅保留 result=WARN 作为 Phase 0 历史标签）

## Phase 1 — Universal Skills Canonicalization

> **⚠ BLOCKED：scope-premise mismatch（2026-04-20 Phase 1.1 pre-flight 发现）**
>
> Phase 1.1/1.2/1.3 现文本假设 adapter `scripts/<file>` 存在可替换的 canonical 对应文件。实测三个 skill canonical/adapter scripts **零重叠**：
> - `plan`：adapter 无 `scripts/` 目录；canonical `scripts/` 完整但 adapter 不引用
> - `done`：canonical `scripts/` 不存在且 `workflow.yaml steps: []`；adapter 9 个脚本均 Claude 专属
> - `memory-gc`：canonical `scripts/` 不存在且 `workflow.yaml steps: []`；adapter 4 个脚本均 Claude 专属
>
> 证据与选项（narrow / broad / 混合）详见 `migration-log.md` 2026-04-20 Phase 1.1 条目。
>
> **1.1 / 1.2 / 1.3 在 Captain 选定 scope 前保持 BLOCKED**；1.0 / 1.4 / 1.5 不受影响但需等 1.1/1.2/1.3 达成一致再启动。

- [ ] 1.0 Bridge daemon drain
  - `launchctl unload ~/Library/LaunchAgents/com.feishu-bridge.*.plist`
  - `pgrep -f feishu_bridge` 为空后再开工

- [ ] 1.1 Canonicalize `plan` **[BLOCKED — 见 Phase 1 header scope 说明]**
  - Pre-flight 实测（2026-04-20）：canonical 已完整（`spec-resolve.py`、`spec-write.py`、prompts、schemas、workflow steps 全配），adapter 无 `scripts/` 目录、SKILL.md body 仅跨 skill 引用 `done/scripts/spec-archive-validate.py`。当前文本 "adapter scripts/<file> → canonical file-level symlink" 无操作对象 → 待 scope 决定后改写
  - 注：Phase 0.0 已 freeze plan skill snapshot，本步在 `~/.agents/skills/plan` 现有结构基础上补齐缺失 scripts/schemas/prompts
  - Replace `$CLAUDE_HOME/skills/plan` with **thin wrapper 模式 A**：保留 adapter 现有 Claude 专属 SKILL.md body（不与 canonical stub byte-identical 比较），`scripts/` 下每个 runner-共享脚本改为 file-level symlink → `$AGENTS_HOME/skills/plan/scripts/<file>`（Phase 1.0 已实证运行时透明）；Claude 专属脚本（若有）保留为 adapter 实文件并在 manifest `notes` 标注（模式 B 混用）
  - **原子替换**（必须）：每个 script 从实文件切换为 symlink 时采用 `os.symlink(canonical, tmp_path)` → `os.rename(tmp_path, adapter_path)` 模式，避免 `rm + symlink` 竞态窗口。选项 B 下 bridge 运行中，飞书并发 user 触发 skill 不可豁免
  - Validate Claude native `/plan` still works；写 manifest `adapter_type: "wrapper"`

- [ ] 1.2 Canonicalize `done` **[BLOCKED — 见 Phase 1 header scope 说明]**
  - Pre-flight 实测（2026-04-20）：canonical 侧 `workflow.yaml steps: []` + 无 `scripts/` 目录；adapter 9 个脚本（`session-done-*.sh`、`spec-archive-*`、`memory-anchor-sync.sh`、`memory-gc-check.sh`、`stale-ctx-check.sh`）+ `assets/extraction-schema.json` 均 Claude 专属（硬编码 `~/.claude/bin/*`）。"Move/copy runner-shared scripts" 需先决定哪些脚本抽象为 runner-neutral（选项 Y）还是保持 Claude-only（选项 X）
  - **inventory 补录**：原 Phase 0.1 未列 `~/.claude/skills/done/assets/extraction-schema.json`，本 task scope 需含 assets/
  - Move/copy scripts and assets from `$CLAUDE_HOME/skills/done` to `$AGENTS_HOME/skills/done`
  - Preserve executable bits
  - Replace `$CLAUDE_HOME/skills/done` with **thin wrapper 模式 A**：adapter `scripts/<file>` 改为 file-level symlink → canonical；SKILL.md 两侧分离保留（adapter Claude 专属 body 不改）。当前 9 个 done 脚本（见 Phase 2.1）全部 runner-共享，默认模式 A；若发现 Claude 专属脚本，切换模式 B 并在 manifest `notes` 标注
  - **原子替换**（必须）：沿用 Phase 1.1 定义的 `os.symlink + os.rename` 模式
  - Validate bridge `/done` tests and Claude native `/done`

- [ ] 1.3 Canonicalize `memory-gc` **[BLOCKED — 见 Phase 1 header scope 说明]**
  - Pre-flight 实测（2026-04-20）：canonical 侧 `workflow.yaml steps: []` + 无 `scripts/` 目录；adapter 4 个脚本（`memory-gc-stats.sh` / `memory-gc-route.sh` / `memory-gc-archive.sh` / `memory-gc-maintain.sh`）均 Claude 专属。同 Phase 1.2，需先决定 scope
  - Move/copy `memory-gc-*.sh` scripts into `$AGENTS_HOME/skills/memory-gc/scripts`
  - Preserve executable bits
  - Replace `$CLAUDE_HOME/skills/memory-gc` with **thin wrapper 模式 A**：adapter `scripts/<file>` 改为 file-level symlink → canonical；SKILL.md 两侧分离保留；4 个 memory-gc 脚本（见 Phase 2.2）默认 runner-共享
  - **原子替换**（必须）：沿用 Phase 1.1 定义的 `os.symlink + os.rename` 模式
  - Validate `/memory-gc --dry-run`

- [ ] 1.4 Add rollback backups
  - Backup original Claude skill dirs before replacing with adapters
  - **Backup location: `$AGENTS_HOME/adapters/claude/migration-backups/<timestamp>/<name>`**
    （**不**放 `$CLAUDE_HOME/skills/.migration-backup/` 下——会被 Claude skill loader 扫到）
  - Store manifests under `$AGENTS_HOME/adapters/claude/migration-manifests`

- [ ] 1.5 Bridge daemon restart + live smoke
  - `launchctl load ...`
  - 飞书 bot 跑 `/plan --dry-run`、`/done --dry-run`、`/memory-gc --dry-run`
  - 30 分钟观察 journal

- [ ] Phase 1 Spec-Check
  - result: <PASS|WARN|BLOCK>

## Phase 2 — Script Path Migration

- [ ] 2.0 Bridge daemon drain（同 1.0）

- [ ] 2.1 Update done scripts for `AGENTS_HOME` / `CLAUDE_HOME`
  - 源清单以 Phase 0.1 inventory.md 为准，当前实际 9 个：
    - `session-done-apply.sh`
    - `session-done-commit.sh`
    - `session-done-format.py`
    - `memory-anchor-sync.sh`
    - `memory-gc-check.sh`
    - `spec-archive.sh`
    - `spec-archive-validate.py`
    - `spec-check-write.py`
    - `stale-ctx-check.sh`
  - 每个脚本顶部加 `AGENTS_HOME=${AGENTS_HOME:-$HOME/.agents}` / `CLAUDE_HOME=${CLAUDE_HOME:-$HOME/.claude}`
  - 移除硬编码 `~/.claude`；Claude-only 行为显式走 `$CLAUDE_HOME`
  - 保留可执行位（`chmod +x` 检验）

- [ ] 2.2 Update memory-gc scripts for `AGENTS_HOME` / `CLAUDE_HOME`
  - 源清单以 Phase 0.1 inventory.md 为准，当前实际 4 个：
    - `memory-gc-stats.sh`
    - `memory-gc-route.sh`
    - `memory-gc-archive.sh`
    - `memory-gc-maintain.sh`

- [ ] 2.3 Update `session-history`（JSONL 索引，不是 SQLite）
  - 源码已具备双根 `AGENTS_SESSIONS_DIR` + `CLAUDE_SESSIONS_DIR` 遍历能力（line 22-30），本 task 补以下改动：
  - 写路径改为 canonical：`$AGENTS_HOME/memory/sessions/index.jsonl` （atomic rename）
  - index.jsonl entries schema 升级，每行 JSON 新增 `root: "agents"|"claude"` 字段
  - 读路径保持双根遍历；search/list/read/stats 四个子命令在 `root` 字段上可区分过滤
  - PASS: finds bridge `/done` archives（agents root）and legacy Claude archives（claude root），搜索覆盖率不降

- [ ] 2.4 Normalize project ctx writes
  - Canonical: `<repo>/.agents/ctx`
  - Adapter: `<repo>/.claude/ctx` symlink or generated view
  - Ensure session archives do not enter repo ctx

- [ ] 2.5 Rebuild session-history JSONL index
  - `session-history rebuild` 全量重建 `$AGENTS_HOME/memory/sessions/index.jsonl`（atomic rename 写入）
  - 验收：取迁移前 5 个高频关键词（参考 lessons 常用词、近期 session 标题）做基线检索，迁移后命中数 ≥ 基线；entries `root` 字段覆盖 agents + claude 双根
  - 记录检索对比结果到 `.specs/changes/agents-canonical-migration/evidence/session-history-verify.md`

- [ ] 2.6 Rules + CLAUDE.md migration
  - **2.6.0 Pre-action backup**（必须）：
    - `cp ~/.claude/CLAUDE.md $AGENTS_HOME/adapters/claude/migration-backups/<ts>/CLAUDE.md`
    - `cp -a ~/.claude/rules $AGENTS_HOME/adapters/claude/migration-backups/<ts>/rules/`
    - 写 manifest：每文件 sha256（迁前 + 迁后）+ symlink 创建命令 + restore 命令
  - **2.6.1 Canonical 建立**：
    - populate `~/.agents/rules/*.md`（当前为空目录，是一次性 greenfield 复制）
    - 复制前 grep 每条 rule 对 Claude-specific 工具（如 `skill-validate`）的引用，必要时重写为 adapter-aware 路径
  - **2.6.2 Adapter 切换**：
    - `~/.claude/rules/<file>.md` 改为 **per-file byte-identical copy**（Phase 0.6 排除 skill 层 symlink；rules 层保守起见同走 copy 模式，drift 校验 SHA256 等价于真源）
    - `~/.claude/CLAUDE.md` 重构为 adapter（顶部 include/拼接 `~/.agents/AGENTS.md`，尾部保留 Claude-only 段）
  - **2.6.3 金丝雀**：新 Claude CLI session 抽 3 条 lessons 验证仍注入 system prompt；Pi staging 读 `$AGENTS_HOME/AGENTS.md` 成功
  - **2.6.4 Cross-phase 回归**（Critical）：**开启新 Claude CLI session**，重跑：
    - `/plan --dry-run`、`/done --dry-run`、`/memory-gc --dry-run`
    - `session-history search <5 个基线关键词>`、`session-history stats`
    - grep `~/.agents/AGENTS.md` / `~/.agents/rules/*.md` / `~/.claude/CLAUDE.md` 里的陈旧 `~/.claude/bin/*` 引用，确认已改写为 adapter-aware 路径
  - **2.6.5 Rollback-smoke**：临时目录 dry-run restore → 3 条 lesson canary 仍注入
  - 任一检查失败 → Phase 2.6 Spec-Check BLOCK，按 manifest 回滚 adapter

- [ ] 2.7 Bridge daemon restart + live smoke（同 1.5）

- [ ] Phase 2 Spec-Check
  - result: <PASS|WARN|BLOCK>
  - 注：Phase 2 result=PASS 才解冻 `/done` 与 `session-history`（见 Phase 0.0 解冻触发条件）

## Phase 2.5 — Agent Role Canonicalization

> 前置：Phase 0.5 capability-matrix.md 已签字。仅迁移 B 类（`AgentPool`-reusable）角色。A 类 Claude-only 零改动。

- [ ] 2.5.0 Bridge daemon drain

- [ ] 2.5.0a Pre-extraction hash baseline（必须先于 2.5.1）
  - 对 3 个 B 类 reviewer 捕获：
    - `sha256(normalized_body(~/.claude/agents/<role>.md))`：strip frontmatter + 统一行尾 + 合并连续空行 + strip 首尾空行 + lowercase（仅用于 hash）
    - frontmatter metadata 完整快照（`name`/`description`/`tools`/`model`/`effort`/`maxTurns`/其它字段）
  - 写入 `.specs/changes/agents-canonical-migration/evidence/agent-prompt-baseline.json`

- [ ] 2.5.1 Extract canonical prompts
  - `plan-reviewer` body（原格式，不 lowercase）→ `~/.agents/agents/plan-reviewer/prompt.md`
  - `code-reviewer` body → `~/.agents/agents/code-reviewer/prompt.md`
  - `security-reviewer` body → `~/.agents/agents/security-reviewer/prompt.md`
  - 各角色同步写 `~/.agents/agents/<role>/meta.yaml`（`model`、`effort`、`maxTurns` 等非 tools 运行时 hint）

- [ ] 2.5.2 Refactor Claude adapters
  - `~/.claude/agents/<role>.md` 保留完整 frontmatter（含 `tools:` 白名单——Claude Code 必需）
  - body 头部加 `<!-- Canonical prompt: $AGENTS_HOME/agents/<role>/prompt.md -->` 注释
  - body 内容为 canonical prompt 的拷贝（Claude Code agent body 不支持动态 include；由 Phase 2.5.4 hash 检查保证一致）

- [ ] 2.5.3 Update `agent_pool.py` — loader contract
  - 新增 `load_reviewer_prompt(role: str) -> str`：
    - canonical-first：`$AGENTS_HOME/agents/<role>/prompt.md` 存在 → 读原文
    - fallback：canonical 不存在 → 读 `$CLAUDE_HOME/agents/<role>.md` 并 strip YAML `---` frontmatter
    - role ∉ `ALLOWED_REVIEWER_ROLES` → 抛 `ValueError`
    - canonical + fallback 均缺 → 抛 `FileNotFoundError`（fail loud，log 具体搜索路径）
    - malformed frontmatter → 抛 `ValueError`（含文件名 + 行号）
  - 修改 `AgentPool._wrap_prompt(task)` 为 `load_reviewer_prompt(task.role) + "\n\n" + task.prompt`
  - 新增单测覆盖：
    1. canonical-first path hit
    2. canonical missing → fallback body extraction
    3. malformed frontmatter → ValueError
    4. canonical + fallback 均缺 → FileNotFoundError
    5. unsupported role → ValueError
    6. 拼接顺序为 `role_prompt + "\n\n" + task_prompt`

- [ ] 2.5.4 Validate
  - hash 对比：`sha256(normalized_body(~/.agents/agents/<role>/prompt.md))` 与 `agent-prompt-baseline.json` 中 `<role>` 项相等（若有意图修改，Captain 签字写入 `intentional_delta` 字段并记 reason）
  - Claude CLI Task() 调用三个 reviewer 行为与迁移前对齐
  - bridge `AgentPool.run()` 新增单测通过
  - drift check 不把 A 类 agent（`general-purpose`/`Explore`/`statusline-setup`/`loop-operator`）标为"应迁未迁"

- [ ] 2.5.5 Rollback backup
  - 原 `~/.claude/agents/<role>.md` 备份到 `$AGENTS_HOME/adapters/claude/migration-backups/<timestamp>/agents/`

- [ ] 2.5.6 Bridge daemon restart + live smoke

- [ ] Phase 2.5 Spec-Check
  - result: <PASS|WARN|BLOCK>

## Phase 3 — Bridge Integration Tests

- [ ] 3.1 Update `resolve_skill_source` tests if needed
  - Canonical-first behavior remains required

- [ ] 3.2 Add drift script unit/smoke tests
  - Use temp `$AGENTS_HOME` / `$CLAUDE_HOME`
  - Cover ok symlink, duplicate dirs, loop, missing scripts, agent dual-artifact mismatch

- [ ] 3.3 Run bridge workflow tests
  - `test_workflow_registry.py`
  - `test_commands_workflow_wiring.py`
  - `test_done_workflow.py`
  - `test_memory_gc_workflow.py`
  - `test_agent_pool.py`（canonical prompt path 单测）

- [ ] 3.4 Run targeted Claude adapter smoke
  - Discover migrated skills under `$CLAUDE_HOME/skills`
  - Discover migrated B 类 agents under `$CLAUDE_HOME/agents`
  - Confirm symlink/wrapper points to canonical target

- [ ] Phase 3 Spec-Check
  - result: <PASS|WARN|BLOCK>

## Phase 4 — Fallback Skills

- [ ] 4.1 Inventory fallback commands
  - `save`
  - `research`
  - `wiki`
  - `idea`
  - `retro`

- [ ] 4.2 Decide support policy per fallback skill
  - Claude native pass-through
  - Pi explicit unsupported
  - Bridge workflow future candidate

- [ ] 4.3 Migrate fallback skill files to `$AGENTS_HOME/skills`
  - One skill per patch
  - Keep adapters in `$CLAUDE_HOME/skills`

- [ ] Phase 4 Spec-Check
  - result: <PASS|WARN|BLOCK>

## Phase 5 — Tool-Heavy Skills

- [ ] 5.1 Inventory tool-heavy skills
  - OCR skills
  - `things`
  - `security-review`
  - `config-drift`
  - `app-research`
  - `acpx`
  - `investigate`
  - `tdd-workflow`

- [ ] 5.2 Migrate one tool-heavy skill at a time
  - Preserve requirements files and assets
  - Add smoke tests only where dependencies are installed

- [ ] Phase 5 Spec-Check
  - result: <PASS|WARN|BLOCK>

## Phase 6 — Workspace Default Migration

- [ ] 6.1 Change non-Claude default workspace
  - Use `default_runner_workspace(agent_type)` when bot config lacks explicit workspace
  - Keep explicit staging workspace untouched

- [ ] 6.2 Document migration
  - README or operations doc explains `~/.agents`, `~/.claude`, and `~/.feishu-bridge` responsibilities

- [ ] 6.3 Staging validation
  - Pi staging still answers `/model`, `/status`, `/plan`, `/done`, `/memory-gc --dry-run`

- [ ] Phase 6 Spec-Check
  - result: <PASS|WARN|BLOCK>

## Graduation

- [ ] 7.1 Final drift check is clean for migrated skills + B 类 agents
- [ ] 7.2 Final review
- [ ] 7.3 Archive spec
- [ ] 7.4 Dual-read fallback sunset tracking
  - Sunset 条件（三者全满足才移除 fallback 读取代码）：
    1. **task 6.3 staging validation result=PASS**（绑定具体 task ID，避免 Phase 编号调整时错位）
    2. 旧 `$CLAUDE_HOME/memory/sessions` 最新文件 mtime 早于 60 天
    3. `session-history stats` 在去除 fallback 后总计数不减
  - 满足时移除 `session-history` 的 `CLAUDE_SESSIONS_DIR` 遍历分支与 done scripts 的 `CLAUDE_HOME` fallback 读取代码
- [ ] 7.5 Final Spec-Check
  - result: <PASS|WARN|BLOCK>

