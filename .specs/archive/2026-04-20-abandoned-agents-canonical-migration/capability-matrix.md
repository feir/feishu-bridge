# Agent Dispatch Capability Matrix — Phase 0.5

**Date**: 2026-04-20
**Source of truth for**: Phase 2.5 dual-artifact migration scope、`agents-skill-drift.py::check_agent_dual_artifact`
**Prerequisite**: `inventory.md` §2（7 agents enumerated）

---

## Classification Decision

| # | Agent | Class | Model | Size | `ALLOWED_REVIEWER_ROLES`? | Migrates at | Notes |
|---|---|:---:|---|---:|:---:|:---:|---|
| 1 | `plan-reviewer` | **B** | opus | 14056B | ✓ | P2.5 | dual-model Claude+Codex via acpx |
| 2 | `code-reviewer` | **B** | opus | 20443B | ✓ | P2.5 | test adequacy checks |
| 3 | `security-reviewer` | **B** | sonnet | 4466B | ✓ | P2.5 | OWASP checks |
| 4 | `loop-operator` | **A** | sonnet | 950B | ✕ | — | Claude-only autonomous loop orchestration |
| 5 | `build-error-resolver` | **C** | sonnet | 2654B | ✕ | deferred | 按需晋升 B |
| 6 | `database-reviewer` | **C** | sonnet | 7876B | ✕ | deferred | 按需晋升 B |
| 7 | `e2e-runner` | **C** | sonnet | 3933B | ✕ | deferred | Vercel Agent Browser / Playwright，依赖浏览器工具 |

Plus Claude CLI built-ins（不纳入 `~/.claude/agents/` 扫描）：
- `general-purpose` — Claude 内置
- `Explore` — Claude 内置
- `statusline-setup` — Claude 内置

---

## Class Definitions

### A — Claude-only
**保留在** `~/.claude/agents/<role>.md`；**不建 canonical**。
**特征**：
- 不在 `ALLOWED_REVIEWER_ROLES`（bridge AgentPool 不复用）
- 依赖 Claude CLI 独有行为（内置 slash 交互、自动 loop 控制等）
- Pi/Codex 无对等能力

### B — AgentPool-reusable（dual-artifact）
**Canonical**: `~/.agents/agents/<role>/prompt.md`（body only）
**Adapter**: `~/.claude/agents/<role>.md`（frontmatter + tools: + body）
**规则**：adapter body 段 SHA256 必须与 canonical prompt.md 一致（`agents-skill-drift.py` 校验）。
**用法**：
- bridge `AgentPool.load_reviewer_prompt()` 从 canonical 读 prompt，追加任务 prompt 后丢给 Pi/Codex
- Claude CLI 通过 adapter 调用 sub-agent

### C — Deferred
**保留** Claude-only 形态；Phase 2.5 **不迁移**。
**晋升条件**：
- bridge 自动化 pipeline 需要从 Pi 调用
- 或该 agent 成为 `ALLOWED_REVIEWER_ROLES` 一员

---

## Per-agent Rationale

### A 类

**`loop-operator`**（950B，model=sonnet，color=orange）
- 职责：autonomous agent loop 监控 + 干预
- 不在 AgentPool；依赖 Claude CLI 的 loop 交互语义
- Pi/Codex 无 autonomous-loop 对等概念 → 保留 Claude-only

### B 类（必迁）

**`plan-reviewer`**（14056B，opus，`ALLOWED_REVIEWER_ROLES`）
- 职责：plan/design dual-model review（Claude + Codex via acpx）
- AgentPool 已复用（见 `feishu_bridge/workflows/agent_pool.py`）
- Pi/Codex 执行时需访问 canonical prompt 合成 review 指令
- Phase 2.5 生成 `~/.agents/agents/plan-reviewer/prompt.md`

**`code-reviewer`**（20443B，opus，`ALLOWED_REVIEWER_ROLES`）
- 职责：代码评审 + 测试充分性
- AgentPool 复用 / bridge 自动触发 spec-check code-review

**`security-reviewer`**（4466B，sonnet，`ALLOWED_REVIEWER_ROLES`）
- 职责：OWASP Top 10 + 秘密泄露检测
- AgentPool 复用

### C 类（deferred）

**`build-error-resolver`**（2654B，sonnet）
- 职责：构建/类型错误最小 diff 修复
- bridge 当前无自动调用；手动触发场景多
- 评估：若 bridge 引入 "auto-fix build failures"，晋升 B

**`database-reviewer`**（7876B，sonnet）
- 职责：SQL / SQLite schema、并发、完整性专家
- bridge 当前无自动调用

**`e2e-runner`**（3933B，sonnet）
- 职责：E2E 测试生成与运行（Vercel Agent Browser / Playwright）
- 依赖浏览器工具链；Pi/Codex 侧无等价环境
- 评估：若未来 Pi 提供 Playwright 访问，才考虑晋升

---

## Tools Whitelist 兼容性

| Agent | Claude `tools:` | Pi/Codex equivalent | Risk |
|---|---|---|---|
| `plan-reviewer` | Read/Grep/Glob/Bash/TodoWrite | Pi 无 TodoWrite；prompt body 需避免 TodoWrite 依赖，或 bridge 提供 stub | **WATCH** Phase 2.5 |
| `code-reviewer` | Read/Grep/Glob/Bash/Edit/TodoWrite | 同上 | **WATCH** Phase 2.5 |
| `security-reviewer` | Read/Write/Edit/Bash/Grep/Glob | 全部可用 | ✓ |
| `loop-operator` | Read/Grep/Glob/Bash/Edit | — (Claude-only) | N/A |
| `build-error-resolver` | Read/Write/Edit/Bash/Grep/Glob | 全部可用 | ✓（若未来迁） |
| `database-reviewer` | Read/Write/Edit/Bash/Grep/Glob | 全部可用 | ✓（若未来迁） |
| `e2e-runner` | Read/Write/Edit/Bash/Grep/Glob | Browser tools 缺失 | **BLOCK** 晋升（需 Pi 侧提供） |

Phase 2.5 动作项：检查 `plan-reviewer` / `code-reviewer` 的 body 中是否硬编码 `TodoWrite` 调用；若是，**在 Phase 2.5.2 body 抽取时剥离或条件化**（Claude 专属逻辑留给 adapter frontmatter 处理 → 但 body 是共享的，需改写为 runner-agnostic 指令）。

---

## Sign-off

Phase 0.5 签字条件：
- [x] 全部 7 个 agent 分类完成
- [x] 分类依据（`ALLOWED_REVIEWER_ROLES` + tools + 职责）可核对
- [x] B 类 3 项已确认进入 Phase 2.5 scope
- [x] C 类 3 项已明示 deferred 标准
- [x] Tools 兼容性风险项标出（`plan-reviewer` / `code-reviewer` 的 TodoWrite 依赖）

**签字**：Captain 2026-04-20 明确确认 A=1 / B=3 / C=3 分类，接受 `plan-reviewer` / `code-reviewer` body 的 `TodoWrite` 依赖需在 Phase 2.5.2 改写为 runner-agnostic 的 rewrite 代价。Phase 2.5 可启动。
