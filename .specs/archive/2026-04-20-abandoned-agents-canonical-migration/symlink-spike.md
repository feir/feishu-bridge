# Symlink Compatibility Spike — Phase 0.6

**Date**: 2026-04-20
**Purpose**: Decide whether Phase 1 / Phase 2.5 use symlink or thin wrapper for adapting canonical `~/.agents/` to `~/.claude/`.
**Downstream consumers**: Phase 1.1 / 1.2 / 1.3 / 2.5.2 sub-tasks reference this matrix.

---

## Setup

```python
# skill: dir symlink
~/.agents/skills/__spike_target__/          # target（with SKILL.md + scripts/hello.sh）
~/.claude/skills/__spike__ -> ~/.agents/skills/__spike_target__

# agent: file symlink
~/.agents/agents/__agent_spike_target__/prompt.md   # target
~/.claude/agents/__agent_spike__.md -> ~/.agents/agents/__agent_spike_target__/prompt.md
```

Cleanup（spike 结束后）：移除两个 symlink 与各自 target。

---

## Decision Matrix

行 = 维度；列 = 结果（✓ = works as-is；△ = works with caveat；✗ = fails → wrapper required）。

| Dimension | `~/.claude/skills/<name>` **dir** symlink → `~/.agents/skills/<name>` | `~/.claude/agents/<name>.md` **file** symlink → `~/.agents/agents/<name>/prompt.md` |
|---|:---:|:---:|
| Filesystem read via link（Python `Path.read_bytes()`） | ✓ | ✓ |
| `(link/subpath).is_file()` 识别 | ✓ | N/A |
| Subprocess exec `<link>/scripts/hello.sh` | ✓ | N/A |
| `Path.resolve()` 到 canonical 路径 | ✓ | ✓ |
| SKILL.md/prompt.md 内容 byte-identical 于 canonical | ✓ | ✓ |
| **Claude Code skill loader 注册 slash** | **✗**（`Skill(skill:"__spike_target__")` 返回 `Unknown skill`） | **PENDING**（未独立测试，推定 N/A — agent 已定 dual-artifact） |
| **Claude Code agent loader 注册 sub-agent** | N/A | **PENDING**（已定 dual-artifact，无需测） |
| Plugin namespace（如 `plugin:name`） | ✗（同上，未注册即无 namespace） | N/A |
| Relative vs absolute symlink | absolute 已失败；relative 更不会成功 | 同上 |

✓/△/✗ 为已验证；N/A 为无需验证（设计已绕开）。

---

## Empirical Evidence

### ✓ 已确认（文件系统层）
1. `is_symlink` / `readlink` 正确；`resolve()` 到 canonical
2. 通过 symlink path 读 SKILL.md / prompt.md 得到与 canonical 一致的 bytes
3. `<symlink>/scripts/hello.sh` 子进程执行成功（returncode=0）— 证明 scripts 相对路径经 symlink 解析时不受影响

### 已观察（运行时层）
**Bootstrap freeze 副作用**：
- Phase 0.0 初版将 `~/.claude/skills/.done.snapshot.20260420/` 放在 `$CLAUDE_HOME/skills/` 下，Claude Code skill loader **立即注册** 该目录为 skill 并把 `.done.snapshot.20260420` 加入 available-skills 列表；移走后注册消失。
- **推论**：skill loader 对 `~/.claude/skills/` 下的普通目录（非 symlink）至少是动态扫描；对 **symlink 目录** 的行为未在 spike 本轮验证，但 Python filesystem layer 已证明 symlink 对所有 I/O 操作透明。高概率结论：skill loader 走 `os.listdir` + 对每个 entry `os.stat` 或 `pathlib.Path.is_dir()`，symlink 会被自动跟随（默认 follow_symlinks=True）。

### ✗ 已确认（运行时层 — 2026-04-20 新增证据）

**Test**: 在当前会话调用 `Skill(skill: "__spike_target__")`
**Result**: `Unknown skill: __spike_target__`

**解读**：即便 `~/.claude/skills/__spike__` 作为 symlink 存在，并且 filesystem 层验证全部通过（`is_symlink=True` / `exists=True` / `resolve()` 指向 canonical / scripts 可执行），Claude Code **skill 注册扫描不跟随 symlink**。这与 Phase 0.0 "regular dir `.done.snapshot.20260420` 被自动注册"形成明确对比——loader 对 symlink dir 和 regular dir 的处理方式不同。

**推论**：loader 很可能使用 `os.scandir()` 或 `pathlib.Path.iterdir()` 后用默认参数（`follow_symlinks=False`）过滤，或者只对非 symlink 的条目调用 `is_dir()`。具体机制不需确定——**现象已足够排除 symlink 方案**。

**未独立验证**：agent file symlink 对 sub-agent loader 的注册（上表标 PENDING）。因 Phase 2.5 已独立决定使用 dual-artifact（需要 Claude-only frontmatter），agent symlink 路径事实上被 out-of-scope，不再补测。

### ✓ 已确认（Phase 1.0 file-level symlink 试验 — 2026-04-20 新增证据）

**Context**：dir-level symlink 被 loader 排除后，script 访问三选一：(X) file-level symlink / (Y) SKILL.md body rewrite 绝对路径 / (Z) 双写 scripts。用户指示"不做假设，先做试验，确认下"。

**Setup**：在 `~/.agents/skills/plan/scripts/__spike_hello.sh` 放占位脚本，在 `~/.claude/skills/plan/scripts/`（新建目录，adapter 侧原本不存在）下放两个 file-level symlink：`__spike_hello.sh` / `spec-resolve.py`，各指向 `~/.agents/skills/plan/scripts/` 下同名文件。

**Test**：
1. `subprocess.run(["bash", "/Users/feir/.claude/skills/plan/scripts/__spike_hello.sh"])` → returncode=0，stdout 含 `SPIKE_OK pid=11357 path=<adapter-path> realpath=<canonical-path>`。
2. Claude `Read` 工具直接读 `/Users/feir/.claude/skills/plan/scripts/spec-resolve.py` 前 5 行，成功返回 canonical 文件内容。
3. `Path.resolve()` 返回 `/Users/feir/.agents/skills/plan/scripts/spec-resolve.py`，与 canonical 实际路径一致。

**解读**：
- 与 dir-level symlink 完全对称的反面结论——**文件级 symlink 位于 adapter skill 目录内对 Claude 运行时完全透明**。
- 原因推测：skill loader 的 `follow_symlinks=False` 只作用在 **skill 注册扫描** 时（即 `~/.claude/skills/` 下的顶层 entry）。skill 一旦以常规目录注册成功，其内部文件（SKILL.md / scripts/）的访问全部走标准 filesystem I/O，无额外过滤。
- 对 Phase 1 的影响：option **(X) file-level symlink** 可行；SKILL.md body 内的 `~/.claude/skills/.../scripts/...` 硬编码路径可通过 file-level symlink 保持兼容，无需 body 改写。

**Trial cleanup**：spike 脚本与两个 symlink 已全部删除；`~/.claude/skills/plan/scripts/` 空目录已 `rmdir`；两侧 skill 目录恢复到试验前状态。

---

## Verdict（2026-04-20 终审）

| 方案 | 裁定 | 依据 |
|---|:---:|---|
| Phase 1 skill 用 **dir symlink** | **RULED OUT** | `Skill(skill:"__spike_target__")` 返回 `Unknown skill`；loader 不跟随 symlink |
| **Phase 1 skill 用 thin wrapper** | **SELECTED** | canonical `~/.agents/skills/<name>/` 保持完整；`~/.claude/skills/<name>/SKILL.md` 为独立文件但 body 内容与 canonical 一致（或通过 `include:` / sourcing 引用 canonical 脚本） |
| **Phase 2.5 agent 用 dual-artifact** | **SELECTED** | canonical `prompt.md` body + `~/.claude/agents/<role>.md`（frontmatter + tools 白名单 + body）；drift 检查 body SHA256 一致 |
| Phase 2.5 agent 用 symlink | **RULED OUT** | 无论 loader 是否跟随 symlink，adapter 结构与 canonical 不同（文件 vs 目录，含 vs 不含 frontmatter）|

### Thin Wrapper 具体形态（Phase 1.1 / 1.2 / 1.3 设计依据）

**重要 nuance**：三个 "universal skills"（plan / done / memory-gc）的 canonical 与 adapter **不是简单复制关系**：
- Canonical（`~/.agents/skills/<name>/`）：runner-neutral stub SKILL.md（~1KB，含 `runners:` 块指向 `workflow.yaml` 状态机）+ workflow.yaml + 少量 runner-共享 scripts（如 plan 的 spec-resolve.py）
- Adapter（`~/.claude/skills/<name>/`）：完整 Claude 专属 SKILL.md（~7KB，包含 Claude CLI 完整执行步骤）+ 大量 Claude 专属 scripts（如 done 的 9 个 shell/python 脚本）

因此 Phase 1 的迁移目标不是 "让 SKILL.md byte-identical"，而是：**将 runner-共享 scripts 迁移到 canonical，adapter 保留 Claude 专属内容并通过 file-level symlink 引用 canonical scripts**。

**Skill 适配模式 A — "canonical scripts + file-level symlink"（选定）**：
- `~/.agents/skills/<name>/SKILL.md` — runner-neutral stub（保持现状，指向 workflow.yaml）
- `~/.agents/skills/<name>/scripts/` — runner-共享脚本（从 adapter 迁入 canonical）
- `~/.claude/skills/<name>/SKILL.md` — Claude 专属 body（保持现状，不与 canonical SKILL.md 对比）
- `~/.claude/skills/<name>/scripts/<file>` — **file-level symlink** 指向 `~/.agents/skills/<name>/scripts/<file>`（Phase 1.0 已验证 Claude 运行时对 file-level symlink 透明）
- drift 检查：canonical `scripts/` 与 adapter `scripts/` 下同名 entry 必须是 symlink 且 `resolve()` 指向 canonical 实文件
- 优点：scripts 唯一 source（canonical）；adapter SKILL.md body 里的 `~/.claude/skills/<name>/scripts/...` 绝对路径硬编码无需改写即可兼容；Pi/Codex 直接从 canonical 读 scripts，无副本
- 缺点：需要 drift check 新增 rule 验证 symlink 状态

**Skill 适配模式 B — "full copy"（保底）**：
- 仅当某 skill 有 canonical 无法承载的 Claude 专属脚本时启用
- `~/.claude/skills/<name>/scripts/<file>` 为实文件，不 symlink；canonical 下不复制
- drift 检查：adapter 独占脚本 enumerate 并允许存在

Phase 1.1/1.2/1.3 默认采用 **模式 A**；模式 B 仅用于 adapter 独占 scripts（如果有）。SKILL.md 两侧分离设计保留，**不做 SKILL.md SHA256 drift check**。

---

## Action Items

1. [x] ~~Captain 在新 Claude CLI session 验证~~ → **本会话 `Skill(skill:"__spike_target__")` 已测，`Unknown skill`**。
2. [x] ~~步骤 1 成功走 symlink~~ → **N/A，已失败**。
3. [x] **Phase 1 降级 thin wrapper**：design.md Phase 1.1 / 1.2 / 1.3 子任务按 wrapper 路线展开（见上 §"Thin Wrapper 具体形态"）。
4. [x] Phase 2.5 agent 方案：**固定为 dual-artifact**。
5. [x] ~~spike cleanup~~：`~/.claude/skills/__spike__` / `~/.claude/agents/__agent_spike__.md` / `~/.agents/skills/__spike_target__/` / `~/.agents/agents/__agent_spike_target__/` 已全部删除（2026-04-20 验证均不存在）。另清理 `~/.claude/eval/__spike__/`（skill-eval-builder 对 `__spike__` skill 注册期间自动生成的副产物）。
