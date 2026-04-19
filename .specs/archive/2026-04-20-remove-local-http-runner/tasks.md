# Tasks: remove-local-http-runner

## Phase 1 — 侦察

- [x] 1.1 `grep -r "LocalHTTPRunner\|runtime_local\|\"local\"" feishu_bridge/ tests/ README.md` 得到完整引用清单；确认与 design.md §变更模块清单一致
- [x] 1.2 读当前 `runtime_local.py` 确认其只实现 BaseRunner，没有被任何其他 runner 继承或 helper 依赖
- [x] 1.3 读当前 `tests/smoke_path_z.py`，找到 LocalHTTP 轮次的确切结构

## Phase 2 — 删除

### 2A. 文件级删除
- [x] 2.1 删除 `feishu_bridge/runtime_local.py`（整文件）
- [x] 2.2 删除 `tests/unit/test_local_runner.py`（整文件）

### 2B. main.py 代码清理（13 处，按行号序）
- [x] 2.3 `main.py:59` 移除 import `from feishu_bridge.runtime_local import LocalHTTPRunner`
- [x] 2.4 `main.py:138-142` `_RUNNER_CLASSES` 移除 `"local": LocalHTTPRunner` 条目
- [x] 2.5 `main.py:146` 删除 `_LOCAL_PROMPT_DEFAULTS` 常量
- [x] 2.6 `main.py:156-161` 简化 prompt defaults 为 `base = _GLOBAL_PROMPT_DEFAULTS`；同时更新 `_normalize_prompt_config` docstring 去掉 "For agent_type=='local' the base defaults flip to safety=minimal..." 提及（L1）
- [x] 2.7 `main.py:176-190` **删除整个 `_normalize_endpoint_config()` 函数**（H1 补充：LocalHTTP-only helper）
- [x] 2.8 `main.py:265-273` `_normalize_provider_profiles` 删除 endpoint 块（`endpoint = _normalize_endpoint_config(...)` + `if endpoint: profile["endpoint"] = endpoint`）；preserved keys loop 收窄为 `("model_aliases", "workspace")`（H1 补充）
- [x] 2.9 `main.py:315-320` 删除 command 解析里 `if agent_type == "local":` 块
- [x] 2.10 `main.py:471-478` **在 `_RUNNER_CLASSES` 校验之前**新增 `if agent_type == "local":` 迁移分支，走 `log.error + sys.exit(1)`，文案按 design.md §2 位置 A 模板（H2 — 不用 raise）
- [x] 2.11 `main.py:483-484` 删除 `elif agent_type == "local": default_cmd = "local"  # sentinel` 分支
- [x] 2.12 `main.py:495-497` 更新 `_prompt_raw` 注释（去掉 "claude→local would keep feishu_cli=True" 举例；改为中性描述，L1）
- [x] 2.13 `main.py:522, 844, 904` `if agent_type != "local" and not resolved_cmd:` → `if not resolved_cmd:`（三处）
- [x] 2.14 `main.py:631` 删除 `if agent_type == "local":` create_runner 分支
- [x] 2.15 `main.py:879-884` **在 switch_agent `_RUNNER_CLASSES` 校验之前**新增 `if target_type == "local":` 迁移分支，返回 `(False, 迁移文案, None)`，文案按 design.md §2 位置 B 模板（M4）

### 2C. 其他 feishu_bridge/ 模块
- [x] 2.16 `feishu_bridge/worker.py:48-49` 删除 `if "local" in name: return "local"` 分支
- [x] 2.17 `feishu_bridge/runtime.py:424, 433` docstring 更新（去掉 "LocalHTTPRunner overrides..." 提及）
- [x] 2.18 `feishu_bridge/session_journal.py:17` docstring 去掉 `"local"`
- [x] 2.19 `feishu_bridge/workflows/registry.py:105` docstring 去掉 `'local'`

### 2D. tests/ 清理（M1 + M2 + M5 枚举）
- [x] 2.20 `tests/smoke_path_z.py:20-35` **转换**（非删除）：`[local]` 轮从断言 `create_runner ValueError` 改为断言 `load_config` 捕获 `SystemExit(1)` + log 文案含 "已于 2026-04-19 移除" 和 "codex"（M5）
- [x] 2.21 `tests/unit/test_bridge.py:3664` 删除 `from feishu_bridge.runtime_local import LocalHTTPRunner` import
- [x] 2.22 `tests/unit/test_bridge.py` 删除 6 个 LocalHTTP 专属测试（M1 枚举）：
    - `test_load_config_local_type` (L3667)
    - `test_load_config_local_prompt_defaults_are_minimal` (L3687)
    - `test_create_runner_local_builds_http_runner` (L3705)
    - `test_local_runner_wants_auth_file_is_false` (L3730)
    - `test_local_build_extra_prompts_empty` (L3746)
    - `test_context_health_alert_local_runner_omits_compact_hint` (L3758)
- [x] 2.23 `tests/unit/test_bridge.py` 两个 switch_agent 测试改造（M1 枚举）：
    - `test_switch_agent_claude_to_local_applies_local_defaults` (L3781) → 改断言 switch_agent("local") 返回 `(False, 迁移文案, None)`
    - `test_switch_agent_to_local` (L3833) → 同上（或删除其中一个避免重复）
- [x] 2.24 `tests/unit/test_session_journal.py:238` parametrize `["claude", "pi", "codex", "local"]` 移除 `"local"`（M2）
- [x] 2.25 `tests/unit/test_workflow_registry.py:146` `test_resolve_unsupported_for_local_runner` 改断言其他未知 runner（如 `"mystery"`）→ UNSUPPORTED（M2）
- [x] 2.26 `tests/unit/test_workspace_policy.py:71` `test_codex_and_local_share_non_claude_default` 缩窄为 `test_codex_shares_non_claude_default`，删除 `paths.default_runner_workspace("local")` 断言行；`test_unknown_runner_type_uses_non_claude_default` (L78) 已经覆盖 "unknown" case（M2）

## Phase 3 — 测试

- [x] 3.1 `tests/unit/test_bridge.py` 新增 `test_migration_error_on_local_agent_type_load_config`：写入 `{"agent": {"type": "local"}}` 临时 config，调 `load_config(path, bot_id)`，用 pytest `SystemExit` + capsys/caplog 捕获退出码 1 + log 文案含 "已于 2026-04-19 移除" 和 "codex"
- [x] 3.2 `tests/unit/test_bridge.py` 新增 `test_migration_error_on_local_agent_type_switch_agent`：构造最小 bot，调 `bot.switch_agent("local")`，断言返回 `(False, msg, None)` 且 `msg` 含相同迁移关键字
- [x] 3.3 运行全量单元测试（`pytest tests/unit/`）确认 Claude / Codex / Pi runner 测试全部 PASS，2 个新 migration 测试 PASS
- [x] 3.4 运行 `tests/smoke_path_z.py` 确认 3 runner round + 1 migration round 全 PASS

## Phase 4 — 文档

- [x] 4.1 `README.md:177` agent-type 注释 `claude | codex | local | pi` → `claude | codex | pi`
- [x] 4.2 `README.md:206` `/agent claude / /agent codex / /agent local / /agent pi` → `/agent claude / /agent codex / /agent pi`
- [x] 4.3 `README.md:316` commands dict 示例 `"local": "local",` 行删除
- [x] 4.4 `README.md` §"模型相关配置归属 CLI" 表格：删除 LocalHTTPRunner 行
- [x] 4.5 `README.md:387` 关于 LocalHTTPRunner model-required 的要点删除
- [x] 4.6 `README.md` 新增"迁移指引"段（或合并到配置示例段）：展示 `"type": "local"` → `"type": "codex"` + `--oss --local-provider ollama` 配置映射（Ollama 默认端口场景 + config.toml 覆盖自定义端口场景）
- [x] 4.7 `.specs/changes/multi-agent-runner/proposal.md` Decision Log：追加 `2026-04-19 LocalHTTPRunner removed — H1 + F1 resolved via file deletion` 条目

## Phase 5 — 验证

- [x] 5.1 `grep -r "LocalHTTPRunner\|runtime_local" feishu_bridge/ tests/` 返回 0 行
- [x] 5.2 `grep -r '"local"' feishu_bridge/` 剩余引用全部在 migration 文案字符串或命令比较（如 `target_type == "local"`）上，无执行路径构造 LocalHTTPRunner
- [x] 5.3 manual smoke：启动 staging bridge 用 `tests/staging_config_empty_codex.json`（或等效）发一条消息，确认 Codex 路径仍正常
- [x] 5.4 尝试加载含 `"type": "local"` 的 config，确认进程退出码 1 + stderr 出现单行中文迁移提示（**不是** Python traceback）
- [x] 5.5 运行中 `/agent local` 热切换尝试，确认 bot 回复迁移文案而非 "未知 Agent 类型"

## Phase 6 — Review

- [x] 6.1 code-reviewer subagent：spec-check scope = Phase 1-5，检查是否有残留引用、迁移错误信息是否清晰、Claude/Codex/Pi 行为是否意外受损
- [ ] 6.2 （可选）codex-agent cross-review：重点看是否有"删文件太多导致其他地方 break"的遗漏
- [x] 6.3 Round N Spec-Check block 追加到本 tasks.md 尾部，result 字段填 PASS / WARNING / BLOCK

## Spec-Check

- result: BLOCK
- reviewer: plan-reviewer (Claude) + codex-agent (cross-model via acpx)
- basis: HEAD=21d0975+dirty
- timestamp: 2026-04-19
- scope: Round 1 dual-model plan review — proposal.md + design.md + tasks.md
- notes: |
    Direction: PASS（两个模型都同意删除优于补 modelUsage）。Engineering: BLOCK — 3 HIGH design-level gaps.
    Divergence: None. Both models independently BLOCK with overlapping evidence. Claude added M4（运行时 /agent UX）；Codex added L1（docstring sweep）+ 外部 SKILL.md 元数据。

    HIGH:
    - H1 [Claude+Codex] main.py 变更清单不全：`_normalize_endpoint_config()` (main.py:176-190) 是 LocalHTTP-only helper；`_normalize_provider_profiles` 的 endpoint + local extras 块 (main.py:267-273) 保留的 `max_tokens` / `context_window` / `openai_include_usage` 三个 key 都是 LocalHTTP-only runner kwargs。design.md §3 表格未列入，实施后变成死代码/死配置。Fix：design 显式追加（删 `_normalize_endpoint_config`、删 main.py:265-273、preserved keys 收窄到 `workspace` + `model_aliases`）。
    - H2 [Claude+Codex] 迁移 ValueError 会让 main() 吐未处理栈：design §2 在 `load_config` 里 `raise ValueError(...)`，但 main.py:1904 `load_config(config_path, args.bot)` 无 try/except；周围校验风格是 `log.error + sys.exit(1)` (main.py:471-478)。用户升级后看到 Python traceback，而非友好迁移提示。Fix：改用 `log.error(msg); sys.exit(1)` 与邻居一致；或在 main.py:1904 调用点加 try/except ValueError → log.error + exit(1)。
    - H3 [Claude+Codex] Codex 迁移示例可能用错 knob：proposal.md:53-64 `args_by_type.codex=["--oss","--local-provider","ollama"]` + `env_by_type.codex.OPENAI_BASE_URL`。Codex CLI `--help` 有 `--oss` / `--local-provider` 但没有 OPENAI_BASE_URL；Codex 用 `~/.codex/config.toml` 的 `model_provider` / provider profiles；binary strings 含 `CODEX_OSS_BASE_URL` / `CODEX_OSS_PORT`。用户复制示例大概率 endpoint 不生效。Fix：实施前跑一次 `codex --oss --local-provider ollama` 对 local Ollama；示例改为 Codex config.toml profile 形式或 CODEX_OSS_BASE_URL；Captain 的"validate once"步骤必须前移到合并前。

    MED:
    - M1 [Claude+Codex] test_bridge.py 清理欠规格：tasks.md 2.17 只说 "grep 'local' 清理残留"，但 tests/unit/test_bridge.py:3664-3878 约 215 行专门 LocalHTTP 测试。Fix：枚举确切测试名 — `test_load_config_local_type`、`test_load_config_local_prompt_defaults_are_minimal`、`test_create_runner_local_builds_http_runner`、`test_local_runner_wants_auth_file_is_false`、`test_local_build_extra_prompts_empty`、`test_context_health_alert_local_runner_omits_compact_hint`、`test_switch_agent_claude_to_local_applies_local_defaults`、`test_switch_agent_to_local`、加 L3664 `LocalHTTPRunner` import。两个 switch_agent("local") 测试：删除或改断言"未知 Agent 类型"路径。
    - M2 [Claude+Codex] 遗漏 3 个测试文件：tests/unit/test_session_journal.py:238（parametrize 含 "local"）、tests/unit/test_workflow_registry.py:146（test_resolve_unsupported_for_local_runner）、tests/unit/test_workspace_policy.py:71（test_codex_and_local_share_non_claude_default）。Fix：Phase 2 新增子任务（移除 parametrize "local"、workflow_registry 测试改断言其他未知 runner→UNSUPPORTED、workspace 测试缩窄为 codex only）。
    - M3 [Claude+Codex] README 清理欠规格：tasks.md 4.1-4.3 只列表格行+line 387+迁移段，漏了 README.md:177 agent-type 注释、README.md:206 /agent 命令句、README.md:316 commands dict 示例含 `"local": "local"`。Fix：Phase 4 显式追加这三处（`--local-provider` 保留，是 Codex CLI flag）。
    - M4 [Claude] /agent local 运行时 UX 缺口：design §2 只在 config 加载层拦截迁移。用户 hot-switch 用 `/agent local` 会命中 main.py:881-883 的通用 `未知 Agent 类型` 错误，零迁移提示。Fix：switch_agent 在 _RUNNER_CLASSES 校验前加 "local" 分支返回相同迁移文案；或在"未知"错误里加 "local" 提示。
    - M5 [Claude+Codex] smoke_path_z.py 应转换非删除：L27-35 当前断言 `create_runner(type='local')` raise ValueError（Path Z strict model-required 守卫）；tasks.md 2.16 要求整轮删除，但这正是 H2 新迁移错误的天然回归测试。Fix：转换为 `load_config` 层断言（匹配 design §2 位置），保留廉价回归覆盖。

    LOW:
    - L1 [Codex] Prompt defaults 清理需同步 docstring/注释：移除 `_LOCAL_PROMPT_DEFAULTS` 也要清理 `_normalize_prompt_config` docstring (main.py:156-161 "For `agent_type=='local'` the base defaults flip to safety=minimal...") 和 `_prompt_raw` 注释 (main.py:495-497 "claude→local would keep feishu_cli=True")。Plan 只列常量/分支，没列 docstring/comment。Fix：新增 docstring/comment 更新子任务。
    - L2 [Codex] Spec-Check placeholder 格式不够具体：tasks.md:57 占位只列字段名，没给 YAML 排序。multi-agent-runner 既有块用有序 bullets（result / reviewer / basis / timestamp / scope / notes）。Fix：placeholder 换成匹配既有块的模板。

    外部 consumer 风险 (Codex finding, Claude verified)：
    - ~/.agents/skills/{plan,done,memory-gc}/SKILL.md 含 `local: unsupported` frontmatter 条目（路径：SKILL.md:28/33/31）。这些是纯元数据，bridge runtime 不读。决定：(a) 保留作惰性元数据 — 无害，表达意图，零代码动作；(b) 扫描移除。推荐 (a) 并在 design.md 风险段加注说明 drift。

    Verdict: BLOCK — H1/H2/H3 是会导致实施问题的设计层缺口（漏代码、traceback UX、错误迁移文档）；M1-M5 是欠规格导致执行不完整。
    Post-review: fix-then-proceed — 修 H1/H2/H3 + 在 design/tasks 枚举 M1-M5 后，实施可以直接走，无需再轮 review；Phase 6 code-reviewer spec-check 将在实施阶段验证。

- result: RESOLVED (Round 1 fix applied)
- reviewer: Claude (main session applying plan-reviewer feedback)
- basis: HEAD=21d0975+dirty (post-fix dirty state in .specs/changes/remove-local-http-runner/)
- timestamp: 2026-04-19
- scope: Round 1 findings 落地位置记录（implementation pending）
- notes: |
    每条 finding 的处理位置和状态（便于实施/后续 code-reviewer 对照）：

    HIGH:
    - H1 [RESOLVED in spec] proposal.md §变更模块清单 main.py 行量级改为 "清理 13 处"；design.md §3 表格新增 main.py:176-190（删 `_normalize_endpoint_config`）和 main.py:265-273（删 endpoint 块 + preserved keys 收窄到 `model_aliases, workspace`）；tasks.md Phase 2B 新增 2.7/2.8 子任务枚举。
    - H2 [RESOLVED in spec] design.md §2 从 `raise ValueError` 改为 `log.error + sys.exit(1)`，与 main.py:471-478 + 518-520 邻居风格一致；tasks.md 2.10 明确要求 `log.error + sys.exit(1)`（不用 raise）；测试策略更新为 `SystemExit(1)` + caplog 断言。
    - H3 [RESOLVED in spec] proposal.md 迁移示例删除 `OPENAI_BASE_URL` 环境变量（Codex CLI 不支持）；新增"配置机制"说明（`--oss` = `-c model_provider=oss` + `--local-provider ollama`）；自定义端口走 `~/.codex/config.toml` 或 `-c model_providers.oss.base_url=...`。Pre-merge 验证步骤取消（Captain 本地已移除 Ollama，仅保留 omlx；迁移示例面向通用用户，无需 Captain 本地验证）。

    MED:
    - M1 [RESOLVED in spec] tasks.md Phase 2D 枚举 test_bridge.py 确切测试名（2.21 import + 2.22 6 个测试 + 2.23 两个 switch_agent 测试改造）。
    - M2 [RESOLVED in spec] tasks.md Phase 2D 新增 2.24/2.25/2.26 三个子任务（session_journal / workflow_registry / workspace_policy）。
    - M3 [RESOLVED in spec] tasks.md Phase 4 改为 7 个子任务 (4.1-4.7) 枚举 README.md:177/206/316/表格/387/迁移段/Decision Log；design.md §5 补充保留不动的 `--local-provider ollama` / `.local/bin` / `pi-local` 区分说明。
    - M4 [RESOLVED in spec] design.md §2 新增"位置 B — switch_agent"段；tasks.md 2.15 新增 switch_agent 迁移分支子任务；Phase 5 5.5 新增运行时 `/agent local` 验证；Phase 3 3.2 新增 `test_migration_error_on_local_agent_type_switch_agent`。
    - M5 [RESOLVED in spec] design.md §4 改为"转换非删除"设计；tasks.md 2.20 明确转换内容（断言 SystemExit + log 文案）；Phase 3 3.4 改为 "3 runner round + 1 migration round"。

    LOW:
    - L1 [RESOLVED in spec] tasks.md 2.6 补充 docstring 更新；2.12 新增 `_prompt_raw` 注释更新（main.py:495-497）。
    - L2 [N/A — ONGOING] Spec-Check 格式已通过 Round 1 实际块（L90-117）落盘时遵循 multi-agent-runner 模板，placeholder 已被真实内容替换。

    外部 consumer 风险 [RESOLVED in design]: design.md §风险与 Mitigation 新增表格行，明确"保留作惰性元数据"的决定和理由（bridge runtime 不读 workflow metadata 的 runner 枚举）。

    实施就绪：所有 spec 修订已落盘，无需再轮 plan-review。直接进入 Phase 2 实施；Phase 6 code-reviewer spec-check 作为实施质量守卫。

- result: PASS-WITH-EDITS
- reviewer: code-reviewer (Claude subagent)
- basis: HEAD=21d0975+dirty
- timestamp: 2026-04-20
- scope: Phase 6 code-reviewer spec-check — Phases 1-5 verification against repo state
- notes: |
    Verdict: PASS-WITH-EDITS — one minor Phase 2.19 miss (docstring not updated).

    Verified CLEAN:
    - Residual references: `feishu_bridge/` has only 2 LocalHTTPRunner mentions (main.py:438 load_config migration error, main.py:834 switch_agent migration error) — both are spec-mandated migration strings. `tests/` has 2 residual "local" mentions (test_bridge.py:3669 migration test config, L3689 migration test call) + 1 documentation comment (test_bridge.py:3661 section header, smoke_path_z.py:33 comment). All expected.
    - File deletions: `feishu_bridge/runtime_local.py` and `tests/unit/test_local_runner.py` both removed (tasks 2.1, 2.2). _RUNNER_CLASSES collapsed to {claude, codex, pi} at main.py:137-141 (task 2.4). _normalize_endpoint_config and _LOCAL_PROMPT_DEFAULTS gone from live code (tasks 2.5, 2.7). Preserved keys at main.py:240 correctly narrowed to ("model_aliases", "workspace") (task 2.8).
    - Migration UX:
      * load_config (main.py:436-442): `log.error + sys.exit(1)` with single-line Chinese message containing "已于 2026-04-19 移除" + "codex" + "README §从 type=local 迁移" — matches design §2 Position A.
      * switch_agent (main.py:831-837): returns (False, msg, None) with equivalent message — matches design §2 Position B.
      * Both messages reference anchor "§从 type=local 迁移" consistent with README:256 `### 从 \`type=local\` 迁移（2026-04-19 起）`.
    - worker.py:35-48 _derive_runner_type: "local" branch removed (task 2.16).
    - Test integrity: `uv run pytest tests/unit -q` → 735 passed, 30s. `uv run python tests/smoke_path_z.py` prints 3 runner rounds + `[local] migration rejected at load_config (expected)` matching design §4 transformation.
    - README.md:256-264: new "从 `type=local` 迁移" section exists with mapping table (旧 local → codex + --oss --local-provider ollama). No "local": "local" in commands dict. No LocalHTTPRunner row. Anchor matches main.py error strings.
    - multi-agent-runner Decision Log (proposal.md:85): `2026-04-19 LocalHTTPRunner removed — H1 + F1 resolved via file deletion` entry present (task 4.7).
    - Side-channel bleed: commands.py, runtime_pi.py, test_pi_runner.py, test_quota.py, uv.lock contain zero LocalHTTP leftovers — confirmed out-of-scope Pi-runner work.

    Residual finding (minor):
    - LOW [SCOPE] **Task 2.19 incomplete**: `feishu_bridge/workflows/registry.py:19` docstring schema still lists `local: ...` alongside `claude / pi / codex`. Spec task 2.19 explicitly says "docstring 去掉 'local'". This is a pure docstring/comment miss (no runtime impact — registry reads frontmatter dynamically, the docstring just documents schema shape). Fix: delete line 19 entirely (1-char edit). Classified PASS-WITH-EDITS rather than BLOCK because (a) no behavioral or test impact, (b) session_journal.py:17 and runtime.py docstrings were correctly updated (tasks 2.17, 2.18), (c) registry_test behavior and runtime unaffected.

    Scope alignment: All implementation changes are inside proposal.md WHAT scope; nothing from NOT IN SCOPE leaked (BaseRunner untouched, no back-compat shim, no auto-migration script, Claude/Codex/Pi runners unchanged).

Re-review: skip

- result: RESOLVED
- reviewer: Claude (main session applying reviewer finding)
- basis: HEAD=21d0975+dirty
- timestamp: 2026-04-20
- scope: Address PASS-WITH-EDITS residual (task 2.19 docstring)
- notes: |
    Deleted `local: ...` line from `feishu_bridge/workflows/registry.py:19` frontmatter schema docstring. Pure documentation edit; no runtime/test impact (registry reads frontmatter dynamically, tests verify runner enumeration via fixtures).

    Verification:
    - `grep -in '\blocal\b' feishu_bridge/workflows/registry.py` → 0 hits.
    - Full unit suite not re-run (docstring-only change, zero code semantics).

    All reviewer findings resolved; PASS-WITH-EDITS cleared to PASS. Change ready for /done archival.
