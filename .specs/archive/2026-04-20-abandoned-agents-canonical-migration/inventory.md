# Inventory — agents-canonical-migration

**Date captured**: 2026-04-20
**Purpose**: Truth source for Phase 2.1/2.2 migration script lists, 0.5 capability matrix, 0.6 symlink spike, and full migration scope.
**Status column**: updated at each Phase end（`P0`/`P1`/`P2` 等表明已由该 Phase 处理）。

---

## 0. Execution Environment（Phase 0.7）

### Bridge daemon
- LaunchAgent plist: `~/Library/LaunchAgents/com.feishu-bridge-staging.plist`（当前仅 staging，无 production）
- Stop: `launchctl unload ~/Library/LaunchAgents/com.feishu-bridge-staging.plist`
- Start: `launchctl load ~/Library/LaunchAgents/com.feishu-bridge-staging.plist`
- Verify quiet: `pgrep -fl feishu_bridge`（当前 2026-04-20 已无进程运行）
- **迁移期间 drain 协议**：Phase 1.0 开始 unload，Phase 2 Spec-Check PASS 后再 load

### Claude CLI
- Binary: `claude`（via Homebrew）
- Settings: `~/.claude/settings.json` + `~/.claude/settings.local.json`
- **本次迁移直接在 Claude CLI 交互模式执行**（不走 bridge），`~/.claude/` 敏感文件编辑走 `~/.claude/bin/file-edit` 或 Python 内联脚本

### Pi runtime
- Binary: `pi-runner`（部署位置待 Phase 2.5 阶段补充；AgentPool 加载路径经由 `$AGENTS_HOME` 环境变量）
- 依赖：canonical 布局必须建在 `~/.agents/`，`~/.claude/` 无访问

### Codex
- Binary: `codex`（acpx 包装）
- Session/auth: `~/.codex/`
- 依赖：`/plan`、`/done`、`/memory-gc` canonical prompts 通过 acpx 注入

---

## 1. Skills

### 1.1 Canonical home（`~/.agents/skills/`）— 迁移目标

| Skill | SKILL.md | workflow.yaml | scripts/ | prompts/ | schemas/ | Status |
|---|---|---|---|---|---|---|
| `plan` | 976B | 547B | 2 files | 1 file | 1 file | canonical 已完整（P0 freeze 快照） |
| `done` | 985B | 296B | — | — | — | 骨架，scripts 待 Phase 2 迁移 |
| `memory-gc` | 878B | 327B | — | — | — | 骨架，scripts 待 Phase 2 迁移 |

Canonical `plan` 完整清单：
- `scripts/spec-resolve.py`（3752B）
- `scripts/spec-write.py`（7657B）
- `prompts/draft.md`（2756B）
- `schemas/plan-draft.schema.json`（1477B）
- `SKILL.md`（976B）+ `workflow.yaml`（547B）

### 1.2 Claude current state（`~/.claude/skills/`）— 迁移范围

**通用 skills（迁 canonical + 保留 Claude adapter）**：

| Skill | 规模 | Phase | Status |
|---|---|---|---|
| `plan` | SKILL.md only | — | 已由 canonical 替代（symlink 待 Phase 1.1） |
| `done` | SKILL.md + 9 scripts | P2 | scripts 列表见 §1.3 |
| `memory-gc` | SKILL.md + 4 scripts | P2 | scripts 列表见 §1.3 |
| `acpx` | SKILL.md only | P1 | 通用调用指南 |
| `config-drift` | SKILL.md only | P1 | 通用维护工具 |
| `idea` | SKILL.md only | P1 | 通用 |
| `investigate` | SKILL.md only | P1 | 通用 |
| `research` | SKILL.md only | P1 | 通用 |
| `retro` | SKILL.md only | P1 | 通用 |
| `security-review` | SKILL.md only | P1 | 通用（与 `/security-review` slash 命名冲突待 Phase 1 评估） |
| `tdd-workflow` | SKILL.md only | P1 | 通用 |
| `wiki` | SKILL.md only | P1 | 通用 |

**Fallback skills（Phase 4）**：

| Skill | 规模 | Phase | Status |
|---|---|---|---|
| `save` | SKILL.md + 9 scripts | P4 | web 内容抓取 fallback |

**Tool-heavy skills（Phase 5）**：

| Skill | 规模 | Phase | Status |
|---|---|---|---|
| `app-research` | SKILL.md only | P5 | 双平台 app 调研 |
| `paddleocr-doc-parsing` | SKILL.md + 9 scripts | P5 | PaddleOCR 依赖重 |
| `paddleocr-text-recognition` | SKILL.md + 5 scripts | P5 | 同上 |
| `things` | SKILL.md + 1 script | P5 | macOS Things 3 专属，Pi 无此工具 |

### 1.3 Script 枚举（Phase 2.1/2.2 真源）

#### `done/scripts/`（9 个可执行文件，不含 .DS_Store）

| Script | Size | Exec | Notes |
|---|---:|:---:|---|
| `memory-anchor-sync.sh` | 8128B | x | |
| `memory-gc-check.sh` | 1899B | - | **非可执行**，被 session-done-apply.sh source |
| `session-done-apply.sh` | 11219B | x | |
| `session-done-commit.sh` | 7336B | x | |
| `session-done-format.py` | 2177B | x | |
| `spec-archive-validate.py` | 15546B | x | |
| `spec-archive.sh` | 3695B | x | |
| `spec-check-write.py` | 4065B | x | |
| `stale-ctx-check.sh` | 4799B | x | |

迁移注意：`.DS_Store` 不纳入；`__pycache__/` 不纳入（每次 import 自动再生）。

#### `memory-gc/scripts/`（4 个可执行文件）

| Script | Size | Exec | Notes |
|---|---:|:---:|---|
| `memory-gc-archive.sh` | 2866B | x | |
| `memory-gc-maintain.sh` | 3038B | x | |
| `memory-gc-route.sh` | 5549B | x | |
| `memory-gc-stats.sh` | 2735B | x | |

---

## 2. Agents（`~/.claude/agents/`）— 7 个

designer.md 已于 2026-04-20 移除，**不纳入迁移**。

| Agent | Size | Default class | Notes |
|---|---:|:---:|---|
| `build-error-resolver` | 2660B | C | 构建错误修复；Pi 可能用 |
| `code-reviewer` | 23829B | B | `ALLOWED_REVIEWER_ROLES` 覆盖，dual-artifact |
| `database-reviewer` | 7940B | C | SQL/SQLite 专家；Pi 可能用 |
| `e2e-runner` | 3947B | C | Vercel Agent Browser / Playwright；Claude 独有 tool |
| `loop-operator` | 950B | A | Claude-only，frontmatter 含 model=sonnet |
| `plan-reviewer` | 14758B | B | `ALLOWED_REVIEWER_ROLES` 覆盖，dual-artifact |
| `security-reviewer` | 4508B | B | `ALLOWED_REVIEWER_ROLES` 覆盖，dual-artifact |

Plus Claude CLI built-in agents（不迁移）：`general-purpose`、`Explore`、`statusline-setup`。

详见 `capability-matrix.md`（Phase 0.5 产出）。

---

## 3. Rules

### `~/.claude/rules/`（12 条，不含 .DS_Store / lessons.md.lock）

| File | Size | Scope | Phase |
|---|---:|---|---|
| `ctx-timeline.md` | 1999B | global | P2.6 |
| `lessons.md` | 32529B | global knowledge base | P2.6 |
| `llm-client.md` | 1666B | global | P2.6 |
| `security.md` | 2321B | global | P2.6 |
| `sensitive-file-edit.md` | 1147B | global | P2.6 |
| `session-history.md` | 1170B | global | P2.6 |
| `skill-quality.md` | 1565B | global | P2.6 |
| `skill-self-patch.md` | 1542B | global | P2.6 |
| `talk-normal.md` | 4005B | global | P2.6 |
| `token-budget.md` | 2376B | global | P2.6 |
| `web-extract.md` | 2669B | global | P2.6 |

### `~/.agents/rules/`
空目录。Phase 2.6 迁移后全部规则在此，`~/.claude/rules/` 仅保留 Claude 独有项（目前预期全迁）。

---

## 4. Output Styles

`~/.claude/output-styles/` **不存在**。无迁移项。

---

## 5. Scripts in `~/.claude/bin/`

| Binary | Size | Phase | Notes |
|---|---:|---|---|
| `check-codex-quota` | 4223B | P2 | codex 专用，考虑移到 canonical |
| `file-edit` | 2433B | P2 | 编辑 `~/.claude/` 敏感文件的绕路；保留或迁移？Phase 0.4 决策 |
| `session-history` | 17192B | P2.3 | 已有双根能力（line 21-30）；P2 改写路径 + entries 加 root 字段 |
| `skill-eval-builder` | 32146B | P2 | skill eval 工具；canonical 化 |
| `skill-eval-runner` | 95868B | P2 | 同上 |
| `skill-validate` | 13484B | P2 | skill 约束校验；canonical 化 |
| `tv` | 7606B | — | Terminal viewer 工具；用途待确认 |

---

## 6. Memory / Sessions

- `~/.claude/projects/-Users-feir--claude/memory/` — 当前主要 memory 路径（MEMORY.md + project files）
- `~/.agents/memory/sessions/` — canonical session 归档路径（1 entry，Phase 2.3 完善）
- `~/.agents/memory/codex-review-6.3-prompt.md` / `codex-review-6.3-round2-prompt.md` — 现存 codex review 产物，Phase 2.3 评估是否保留

详细路径映射见 `design.md` Phase 2.3 / 2.4。

---

## 7. Adapters（`~/.agents/adapters/`）

| Sub | Content | Phase |
|---|---|---|
| `bridge/` | `command-registry.yaml`（736B） | 已存在，Phase 3 验证 |
| `claude/` | `migration-backups/20260420-bootstrap-freeze/*` | P0 产生；P2.6 会持续追加 |
| `pi/` | 空 | Phase 2.5 填充 |

---

## Update log

- 2026-04-20 P0: 初版建立。
