---
branch: master
status: draft
scope: Critical
created: 2026-04-20
---

# Proposal: agents-canonical-migration

## WHY

`pi-runner` 已经把 Feishu Bridge 推向 runner-neutral control plane：Pi、Codex 等非 Claude Code runtime 需要复用 `/plan`、`/done`、`/memory-gc`、rules、scripts、memory 和 journal，而不能依赖 Claude Code 私有目录。

当前文件系统仍处于迁移期：

- `~/.agents/skills/{plan,done,memory-gc}` 已存在，但只有 bridge workflow 需要的骨架和少量脚本。
- `~/.claude/skills/done/scripts/*` 仍承载完整 `/done` 相关 shell/python 工具。
- `~/.claude/skills/memory-gc/scripts/*` 仍承载完整 memory-gc route/archive/maintain/stats 工具。
- `~/.claude/skills/*` 还有多个 Claude-native skills，如 `save`、`research`、`wiki`、`idea`、`retro`、OCR 等。
- 如果继续双份维护，Pi/bridge workflow 和 Claude Code native skill 会出现脚本 drift、规则 drift、memory 路径 drift。

需要将 `~/.agents` 正式确立为 runner-neutral canonical home，并让 `~/.claude` 成为 Claude Code adapter，而不是另一个真源。

## WHAT

建立 `~/.agents` canonical migration：

```text
~/.agents/
  AGENTS.md                  # runner-neutral global rules
  skills/<name>/             # canonical skill body/scripts/assets/schemas/prompts
  rules/                     # runner-neutral rules
  memory/                    # global runner-neutral memory
  adapters/
    bridge/                  # Feishu Bridge command registry and adapters
    claude/                  # Claude Code views/wrappers/generator outputs

~/.claude/
  CLAUDE.md                  # Claude adapter view of AGENTS.md + Claude-specific notes
  skills/<name>              # symlink or thin wrapper to ~/.agents/skills/<name>
```

Migration priorities:

1. MVP universal skills: `plan`, `done`, `memory-gc`
2. Claude-native but bridge-known fallback skills: `save`, `research`, `wiki`, `idea`, `retro`
3. Tool-heavy skills: OCR, `things`, `security-review`, `config-drift`, `app-research`, etc.
4. Project-local ctx adapters: `<repo>/.agents/ctx` canonical, `<repo>/.claude/ctx` compatibility view.
5. Agent role definitions (dual-artifact, 见 Decisions):
   - Phase A: 保持 `~/.claude/agents/` 原状，zero touch；等能力矩阵确定
   - Phase B: 只迁移 bridge `AgentPool` 复用的 reviewer 角色
     （`plan-reviewer` / `code-reviewer` / `security-reviewer`）
   - Claude-native 专用 agents（`general-purpose`、`Explore`、`statusline-setup` 等）
     永久留在 `~/.claude/agents/`，不迁移

## Execution Environment

- 迁移由 Captain 在**交互式 Claude CLI session**中执行（`cwd=~/.claude` 或 `cwd=~/.agents`）；
- bridge daemon 在 Phase 1 / Phase 2 / Phase 2.5 期间必须停止，避免运行中读到半迁移状态；
- 本 change 的迁移流程**不在** bridge 非交互模式内可回放——bridge 受 `~/.claude/` sensitive-file guard 限制，无法无人值守动这些文件；
- 未来若需要自动化回放，需另起独立 shell 脚本版本，不在本 change 范围。

## NOT

- 不在本 change 里启用 Pi write/edit/bash。
- 不改动 Claude Code native skill behavior，除非 symlink/wrapper 后验证等价。
- 不一次性迁移所有 skills；先迁移被 bridge workflow 依赖的 `plan/done/memory-gc`。
- 不删除 `~/.claude`；它继续作为 Claude Code home 和 adapter surface。
- 不把 raw session archives 写进 repo-local ctx。
- 不把 `.agents` 下所有 memory 自动注入本地模型上下文；仍按 workflow/journal 按需加载。
- 不把 Claude Code 专用 subagent（依赖 Task tool + `tools:` 白名单）搬到 `~/.agents/agents/`。
- 不在 Phase 0.5（agent capability matrix）完成前触碰 `~/.claude/agents/` 任何文件。
- 不在本 change 内做自动化/非交互式迁移——执行环境锁定为 Captain 交互式 Claude CLI。

## RISKS

| Risk | Impact | Mitigation |
|------|--------|------------|
| Symlink 切换导致 Claude Code skill 解析异常 | Captain 日常 `/plan`、`/done` 退化 | 先做 inventory + dry-run，逐 skill 切换；保留 rollback manifest |
| 脚本里硬编码 `~/.claude` | Pi/bridge 写入旧路径或看不到新 archive | 先改脚本读取 `AGENTS_HOME`/`CLAUDE_HOME`，再移动真源 |
| 双向 symlink 或 wrapper 循环 | skill discovery 死循环或重复加载 | 只允许 `~/.claude -> ~/.agents` 单向 adapter；检测循环 |
| Claude-native metadata 与 bridge workflow metadata 重复 | 两份 YAML/frontmatter drift | `SKILL.md` frontmatter 为 metadata 真源；`workflow.yaml` 只保留 execution steps/TTL |
| 大量 skills 一次性迁移造成不可定位回归 | 难以 rollback | 分批，先只迁移 `plan/done/memory-gc`，每批有验收脚本 |
| 非 Claude runner workspace 仍指向 `~/.claude` | 新旧目录混用 | canonical migration 完成后再做 workspace default migration |
| Claude Code 与 Pi 对 agent 的语义不一致（前者含 `tools:` 白名单 + Task tool 派发，后者通过 `AgentPool` bounded prompt） | agent 定义无法直接跨 runner 复用 | 采用 dual-artifact：`~/.agents/agents/<role>/prompt.md` 为 canonical prompt，`~/.claude/agents/<role>.md` 为 Claude adapter（含 frontmatter 和 `tools:`） |
| bridge daemon 在迁移中读到半状态 | workflow 崩溃 / 产生脏 journal | Phase 1/2/2.5 开始前停止 bridge，完成并验证后再启动 |
| session-history JSONL 索引与新 archive root 不一致 | 搜索不到 `/done` 新归档 | Phase 2 强制执行 `session-history rebuild` 全量重建 `index.jsonl`，acceptance 验证命中率；`session-history` 源码已具备 `AGENTS_SESSIONS_DIR` + `CLAUDE_SESSIONS_DIR` 双根遍历能力（见 `~/.claude/bin/session-history` line 22-30），Phase 2 只需补 entries 的 root 标签字段 |
| `.migration-backup` 目录被 Claude Code skill loader 误扫 | 出现幽灵 skill 或加载报错 | backup 写到 `$AGENTS_HOME/adapters/claude/migration-backups/` 而非 `$CLAUDE_HOME/skills/` 下 |

## Decisions

| Decision | Default |
|----------|---------|
| Canonical skill root | `$AGENTS_HOME/skills`, default `~/.agents/skills` |
| Claude compatibility | `~/.claude/skills/<name>` is a symlink or thin wrapper to `$AGENTS_HOME/skills/<name>` |
| Preferred adapter | relative symlink when Claude Code handles it transparently |
| Wrapper use | only when Claude Code needs a different view or extra Claude-only instructions |
| Metadata truth source | `SKILL.md` frontmatter |
| Workflow execution truth source | `workflow.yaml` under canonical skill |
| Global rules truth source | `$AGENTS_HOME/AGENTS.md`; `CLAUDE.md` is adapter/generator output plus Claude-only notes |
| Session archive root | `$AGENTS_HOME/memory/sessions` |
| Project ctx truth source | `<repo>/.agents/ctx` |
| Agent role canonical prompt | `$AGENTS_HOME/agents/<role>/prompt.md`（纯文本，runner-neutral） |
| Claude agent adapter | `$CLAUDE_HOME/agents/<role>.md`（含 frontmatter + `tools:` 白名单，引用 canonical prompt） |
| Claude-only agents 处置 | `general-purpose`、`Explore`、`statusline-setup` 等 Claude 专用 agent 保留在 `$CLAUDE_HOME/agents/`，不迁移 |
| Migration backup location | `$AGENTS_HOME/adapters/claude/migration-backups/<timestamp>/<name>`（**不**放 `$CLAUDE_HOME/skills/` 下） |
| Execution environment | Captain interactive Claude CLI；bridge daemon 在 Phase 1/2/2.5 停用 |
| Migration utility location | `scripts/agents-skill-drift.py` in `feishu-bridge` repo（CI 与 `tests/unit/` 集成，与 `paths.py` 共版本）；可选软链 `$AGENTS_HOME/adapters/bridge/scripts/` 供 Pi 运行时发现 |
| Phase 编号真源 | `tasks.md`（Phase 3 = Bridge Integration Tests；Phase 4 = Fallback Skills；Phase 5 = Tool-Heavy Skills；Phase 6 = Workspace Default Migration）；design.md 按此编号对齐 |
| Session-history 索引形态 | JSONL（`$AGENTS_HOME/memory/sessions/index.jsonl`，atomic rename rebuild）；entries 带 `root: "agents"|"claude"` 标签区分双根 |

## Acceptance Criteria

- `~/.agents/skills/{plan,done,memory-gc}` contain canonical `SKILL.md`, `workflow.yaml`, scripts, schemas, prompts, and assets needed by bridge workflows.
- `~/.claude/skills/{plan,done,memory-gc}` are adapter symlinks or documented wrappers, not independent mutable copies.
- `session-done-apply.sh`, `session-history`, memory-gc scripts, and related helpers read `AGENTS_HOME` / `CLAUDE_HOME` instead of hardcoding `~/.claude`.
- A drift check reports no duplicate mutable copies for migrated skills.
- Existing Claude Code native usage of `/plan`, `/done`, `/memory-gc` continues to work.
- Bridge workflows for Pi/Codex continue to find canonical skills through `feishu_bridge.paths.resolve_skill_source()`.
- Rollback is documented and can restore previous `~/.claude/skills/*` directories from backup manifests.
- `capability-matrix.md` 完成并签字：列出每个 `~/.claude/agents/*.md` 的归属（Claude-only / bridge AgentPool-reusable / 待定）。
- `~/.agents/agents/{plan-reviewer,code-reviewer,security-reviewer}/prompt.md` 存在，且 `~/.claude/agents/<role>.md` 为对应 adapter。
- `AgentPool` 从 canonical prompt 路径读取并运行（unit test 覆盖）。
- `session-history rebuild` 执行后搜索旧关键词与新归档关键词均命中。
- `.migration-backup` 目录位于 `$AGENTS_HOME/adapters/claude/migration-backups/`，`$CLAUDE_HOME/skills/` 下无 backup 残留。
- 每个 Phase 末尾 tasks.md 有 `- result: PASS|WARN|BLOCK` 字段以供 spec-archive 识别。
- bridge daemon 在迁移后成功启动，运行 `/plan --dry-run`、`/done --dry-run`、`/memory-gc --dry-run` 通过。

### Evidence Matrix

每条 acceptance criterion 对应一个证据文件，写入 `.specs/changes/agents-canonical-migration/evidence/`：

| Criterion | Phase | 命令 / 检查 | 预期产出 | 证据文件 |
|-----------|-------|-------------|----------|----------|
| canonical skills 就绪 | 1 | `ls $AGENTS_HOME/skills/{plan,done,memory-gc}` | 文件清单 + SKILL.md 存在 | `evidence/phase1-inventory.md` |
| Claude adapter 不是独立 mutable | 1 | drift script | `status=ok` for 3 skills | `evidence/phase1-drift.md` |
| bridge daemon smoke 通过 | 1/2/2.5 | 飞书 bot 跑 `/plan --dry-run` 等 | 回包正常 + journal 干净 | `evidence/phase<N>-smoke.md` |
| session-history 命中率不降 | 2 | 5 个基线关键词 `session-history search` | 迁移前后命中数对比表 | `evidence/session-history-verify.md` |
| capability-matrix 签字 | 0.5 | 矩阵文档 review | 所有 agent 分类确定 + 签字 | `evidence/capability-matrix.md` |
| agent prompt hash 对齐 | 2.5 | sha256 pre/post 对比 | hash 相等或 signed delta | `evidence/agent-prompt-baseline.json` |
| backup 位置合规 | 1/2.5 | `find $CLAUDE_HOME/skills -name "*.migration-backup*"` | 无输出 | `evidence/backup-location-check.md` |
| rollback 可执行 | 1/2.5/2.6 | manifest 驱动 dry-run restore | 可恢复文件清单 | `evidence/rollback-verify.md` |

