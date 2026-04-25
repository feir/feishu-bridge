# Canonical Metadata Rule（Phase 0.4 Decision）

**Status**: CONFIRMED — matches existing canonical implementation at `~/.agents/skills/{plan,done}/`.
**Consumers**: Phase 1 / Phase 2 migrators、`agents-skill-drift.py`、Phase 2.5 `AgentPool` loader。

---

## Skill: SKILL.md frontmatter vs workflow.yaml

| Artefact | Authoritative for | Reader | Forbidden to contain |
|---|---|---|---|
| `SKILL.md` frontmatter（YAML） | **metadata**：`name`、`description`、`triggers`、`allowed-tools`、`argument-hint`、`model`、`effort`、`capabilities`、`runners`、`user-invocable` | Claude Code skill loader、Pi/Codex capability resolver、adapter 生成器 | 状态机、TTL、脚本路径、schema 引用 |
| `workflow.yaml` | **execution contract**：`version`、`ttl`、`states`、`steps`、`schemas`（列表）、`scripts`（列表） | bridge `PiRunner` / `CodexRunner`（当前 Phase 6 尚 stub） | name 以外的 metadata（description/triggers 等不重复） |
| `SKILL.md` body（`---` 之后） | 人类可读指令，LLM 在 Skill 运行时按此指令执行 | 运行 skill 的 LLM（Claude / Pi / Codex） | 机器可读的配置（会被 frontmatter / workflow.yaml 取代） |

### Rationale
- **唯一真源**（single source of truth）：同一字段只能由一处声明。`name` 字段例外（两处均需，作为 join key，迁移脚本校验一致）。
- **Claude Skill runtime 只读 frontmatter + body**，不读 workflow.yaml（workflow 由 bridge 解析）。
- **bridge/Pi/Codex 两文件都读**：frontmatter 决定能力与路由，workflow.yaml 决定状态机执行。

### 冲突解决规则
若同一字段在两文件都出现且值不同 → `agents-skill-drift.py` 报 ERROR，迁移中止。

---

## Agent: canonical prompt + Claude adapter（dual-artifact）

| Artefact | Authoritative for | Reader |
|---|---|---|
| `~/.agents/agents/<role>/prompt.md` | **prompt body only**（无 frontmatter） | `AgentPool.load_reviewer_prompt()`、Pi/Codex 运行时 |
| `~/.claude/agents/<role>.md` | **frontmatter + prompt body**；frontmatter 含 `name`、`description`、`tools:`、`model`、`color` 等 Claude Code 独有字段 | Claude Code sub-agent loader |

### Composition rule
- Claude adapter body 段（`---\n...\n---\n` 之后）**必须与 canonical `prompt.md` byte-identical**（`agents-skill-drift.py::check_agent_dual_artifact` 校验 SHA256 一致）
- 修改 prompt 内容时：
  1. 改 canonical `prompt.md`
  2. 重新生成 Claude adapter body（保留原 frontmatter，替换 body）
  3. 运行 drift 检查
- Claude-only 字段（`tools:` 白名单、`model`）**只在 adapter 的 frontmatter**，不进 canonical prompt

### 3-class routing（详见 `capability-matrix.md`）
- **A 类**（Claude-only）：只有 `~/.claude/agents/<role>.md`；canonical 不建 `prompt.md`
- **B 类**（AgentPool-reusable）：dual-artifact（canonical + adapter）
- **C 类**（待定）：默认保留 Claude-only，按需晋升为 dual-artifact

---

## Rule: `~/.agents/rules/` 与 `~/.claude/rules/`

| Scenario | Canonical | Adapter |
|---|---|---|
| 全局通用规则（Phase 2.6 后目标状态） | `~/.agents/rules/<name>.md`（真源） | `~/.claude/rules/<name>.md` 为 symlink 或 identical copy |
| Claude-only 规则（例如 `sensitive-file-edit.md`） | 不建 canonical | 只存 `~/.claude/rules/` |
| Pi/Codex-only 规则 | 只存 canonical；不进 `~/.claude/rules/` | — |

Drift 规则：若两侧同名文件 SHA256 不一致 → `agents-skill-drift.py::check_rules_adapter` 报 ERROR。

---

## Bin scripts（`~/.claude/bin/` → canonical?）

Phase 2 决定（见 inventory.md §5 + design.md Phase 2）：
- `session-history` → 迁移到 `~/.agents/bin/`（`PATH` 优先 `~/.agents/bin/`，回退 `~/.claude/bin/`）
- `skill-validate` / `skill-eval-{builder,runner}` → 迁移到 `~/.agents/bin/`
- `file-edit` → 保留 `~/.claude/bin/`（Claude 敏感文件专用）
- `check-codex-quota` → 迁移到 `~/.agents/bin/`（codex 通用）
- `tv` → 用途待确认（不在本次 scope）

详见 Phase 2 tasks.md。

---

## 验证（Phase 0 Spec-Check 入口）

```sh
python3 /Users/feir/projects/feishu-bridge/scripts/agents-skill-drift.py --check duplicate_skills --check hardcoded_claude_paths --check executable_bits
```

Phase 0.4 签字条件：上述三项 check 返回 OK 或 INFO（WARN for duplicate_skills 是 mid-migration 预期）。
