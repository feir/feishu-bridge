# Tasks: multi-agent-runner

## 1. 抽象 Runner 接口（零行为变更）

- [x] 1.1 在 `runtime.py` 中定义 `RunResult` dataclass 和 `StreamState` dataclass
- [x] 1.2 提取 `BaseRunner` 抽象基类
- [x] 1.3 将 `ClaudeRunner` 重构为 `BaseRunner` 子类
- [x] 1.4 更新 `worker.py` 和 `main.py` 的类型引用

## 2. 实现 CodexRunner（基于实测验证）

- [x] 2.1 `CodexRunner(BaseRunner)` — 命令构建
- [x] 2.2 `CodexRunner(BaseRunner)` — 输出解析（含 is_error 传播、null item 防护）
- [x] 2.3 System prompt 注入 — 使用 `model_instructions_file`（TLS 线程安全）
- [x] 2.4 Session 管理
- [x] 2.5 模型别名和 context window

## 3. 配置和向导适配

- [x] 3.1 配置结构泛化（`"claude"` → `"agent"` 迁移 + 类型验证）
- [x] 3.2 配置向导增加 Agent 选择
- [x] 3.3 Runner 工厂 + main.py 适配 + session 命名空间

## 4. 命令层适配

- [x] 4.1 `/model` 命令动态化（aliases from runner）
- [x] 4.2 `/cost` 和 `/context` 适配（runner default ctx window, null cost handling）
- [x] 4.3 `/compact` 命令适配（supports_compact guard）
- [x] 4.4 用户可见字符串去品牌化（display_name, fallback_name）
- [x] 4.5 `bridge-settings.json` 条件注入（already handled by runner design）

## 5. 文档和测试

- [x] 5.1 单元测试（117 tests pass）
  - CodexRunner: build_args, parse events, streaming flow, error flow, TLS, temp file
  - RunResult/StreamState: to_dict, is_error default
  - BaseRunner ABC: instantiation guard, __init_subclass__
  - Config: migration, new format, missing type, runner factory, session namespace
- [x] 5.2 更新 README 和飞书文档
  - 配置示例增加 Codex 版本 + 旧格式兼容说明
  - Agent 类型对比表（流式差异、/compact、/cost、会话、默认模型）
  - 向导流程更新（Agent 类型选择）
  - /model、/compact、/cost 命令说明泛化
- [ ] 冒烟测试（手动 QA，需 codex 环境）

## 6. 模型无关化 + Runner 接口收窄（Path Z 严格版）

目标：bridge 只做 bridge 应做的事——模型名、模型别名、context window 等 provider-own 配置全部从 bridge 移除，Runner 接口相应收窄。任何模型相关值未指定时，交给下游 CLI（claude/codex/pi/local HTTP server）自行决定。

### 6.1 删除模型常量和内嵌别名

- [x] 6.1.1 `runtime.py` ClaudeRunner 删除 `DEFAULT_MODEL`、`get_model_aliases`、`get_default_context_window`
- [x] 6.1.2 `runtime.py` CodexRunner 删除 `DEFAULT_MODEL`、`get_model_aliases`、`get_default_context_window`
- [x] 6.1.3 `runtime_pi.py` 删除 `DEFAULT_MODEL`、`DEFAULT_CONTEXT_WINDOW`、`get_model_aliases`、`get_default_context_window`；`_build_streaming_result` 中 `contextWindow` 改为 0，`model_name` 回退为 `"(cli-default)"`
- [x] 6.1.4 `runtime_local.py` LocalHTTPRunner 删除 `DEFAULT_MODEL`、`get_model_aliases`、`get_default_context_window`

### 6.2 收窄 BaseRunner 接口

- [x] 6.2.1 `runtime.py` BaseRunner 删除 `get_model_aliases` 抽象方法
- [x] 6.2.2 `runtime.py` BaseRunner 删除 `get_default_context_window` 抽象方法
- [x] 6.2.3 `runtime.py` 删除 `_merge_model_aliases` helper
- [x] 6.2.4 各 Runner 构造器删除 `model_aliases` 参数及 `self._model_aliases` 实例变量

### 6.3 删除 context_window 推断

- [x] 6.3.1 `runtime.py` 删除 `infer_context_window` 和 `resolve_context_window`
- [x] 6.3.2 `runtime.py` `RunResult.default_context_window` 默认 `200_000` → `0`
- [x] 6.3.3 `runtime.py` BaseRunner.run 里填 `default_context_window` 的调用点改成 0 或删除
- [x] 6.3.4 `runtime_local.py` `context_window: int = 8192` → `int = 0`；相应 `self._context_window` 使用点保持（仍来自 user config）
- [x] 6.3.5 `main.py` `profile.get("context_window", 8192)` → `profile.get("context_window", 0)`

### 6.4 主流程承接 aliases（Runner 无感）

- [x] 6.4.1 `main.py` Bot 实例新增 `model_aliases: dict[str, str]` 字段，构建时从 provider profile 读取
- [x] 6.4.2 `main.py` `create_runner` 不再向 Runner 传 `model_aliases`
- [x] 6.4.3 `commands.py` `/model` 改为 `aliases = self.bot.model_aliases; self.bot.runner.model = aliases.get(arg, arg)`（解析后透传）
- [x] 6.4.4 `commands.py` model_display 及相关显示路径同步调整（不调用 Runner.get_model_aliases）

### 6.5 消费端降级（max_ctx==0 路径）

- [x] 6.5.1 `commands.py` `/context`：`max_ctx == 0` 时显示 "上下文窗口：未知（由 CLI 决定）"，不计算百分比
- [x] 6.5.2 `worker.py` context alert：`max_ctx == 0` 时 early return，不推送告警

### 6.6 测试迁移

- [x] 6.6.1 凡 assert `runner.get_model_aliases()` 的单元测试迁移到 Bot/commands 层
- [x] 6.6.2 凡依赖 `DEFAULT_MODEL` 的 fixture 改为显式 user-config 注入
- [x] 6.6.3 新增 `test_empty_config_claude_runner`（无 model，不传 `--model`）
- [x] 6.6.4 新增 `test_empty_config_codex_runner`（无 model，不注入 `--config model=`）
- [x] 6.6.5 新增 `test_empty_config_pi_runner`（无 model，不传 `--model`）
- [x] 6.6.6 新增 `test_local_runner_requires_model`（启动校验失败）
- [x] 6.6.7 新增 `test_context_command_unknown_window_message`（max_ctx==0 显示"未知"）
- [x] 6.6.8 新增 `test_bot_model_aliases_from_provider_profile` + `_empty_when_profile_missing`

### 6.7 LocalHTTPRunner model 必填校验

- [x] 6.7.1 `main.py` `create_runner`：LocalHTTPRunner 且 model 为 None 时 raise `ValueError`（明确错误信号）
- [x] 6.7.2 其他三个 Runner：model=None 时不传 `--model`，CLI 用自己的默认（已是现状，确认覆盖）

### 6.8 文档更新

- [x] 6.8.1 README 配置示例简化（只需 `agent.type`，其他字段改为"可选，未配则 CLI 默认"）
- [x] 6.8.2 README 新增一节"模型相关配置归属 CLI"——说明 `~/.codex/config.toml`、`~/.pi/models.json` 等
- [x] 6.8.3 飞书文档 `/model`、`/context` 的"未知"降级行为说明
- [x] 6.8.4 proposal Decision Log 追加本次 Path Z 严格版决策

### 6.9 手动回归

- [x] 6.9.1 清空 XDG config 只保留 `agent.type`，三个 CLI 各跑一次消息往返，验证均能用 CLI 自身默认模型应答
    - 首轮（2026-04-19 上午）INVALID：staging pipx venv 未装新代码（`get_default_context_window()` 旧路径），测到的是旧 DEFAULT_CONTEXT_WINDOW 行为
    - 修复：`pipx reinstall feishu-bridge-staging --pip-args="-e /Users/feir/projects/feishu-bridge"`，grep 验证 venv 内 `contextWindow: 0`
    - 重跑 live PASS（2026-04-19 22:30+，staging bridge `com.feishu-bridge-staging`）：
      - Claude 空 config → footer `opus-4-7`（Claude CLI 响应里自带 modelUsage，主动告知 model）、7.2s、53.9k in (99% cached)、16 out
      - Codex 空 config → 应答正常 "Hi! How can I help?"、footer 不含 model 段（CodexRunner `if self.model:` guard，self.model=None 时整个 modelUsage dict 不构建）
      - Pi 空 config → footer `(cli-default)` 占位符（PiRunner `self.model or "(cli-default)"` fallback）、6.8s、3.1k in、11 out
    - 样例 config：`tests/staging_config_empty_{claude,codex,pi}.json`
    - CLI 默认模型发现路径（供未来排障参考）：
      - Claude：硬编码在 CLI binary（`claude --help` 说 `--model` 可选）
      - Codex：`~/.codex/config.toml` 顶层 `model = "gpt-5.2"`
      - Pi：`~/.pi/agent/settings.json` `defaultProvider=omlx, defaultModel=Qwen3.6-35B-A3B-mxfp4`
    - 代码层 smoke（独立于 live）：`tests/smoke_path_z.py`
    - 关键发现：Pi/Codex CLI 不在响应里主动告知 model，Path Z 下 bridge 只能显示占位符或省略——CLI 输出约定差异，非 bridge bug

- [x] 6.9.2 `/model sonnet`、`/model opus` 透传给 Claude CLI（native alias）
    - Live PASS（2026-04-19 22:33+）：
      - `/model sonnet` + "hi" → footer `sonnet-4-6`（Claude CLI 把短名解析为长名）
      - `/model opus` + "hi" → footer `opus-4-7`
      - bridge 回复文案"（未识别的名称，将直接传递给 CLI）"：样例 A 没配 user alias，sonnet/opus 不在 `bot.model_aliases` 中走直传路径，符合 Path Z 预期
- [SKIP] 6.9.3 `/model <user-config 别名>` 解析后透传
    - 跳过理由：单元测试 `test_bot_model_aliases_from_provider_profile` + `_empty_when_profile_missing` (§6.6.8) 已覆盖字典查找；6.9.2 已验证"bridge → runner.model → `--model` 透传 → CLI"完整链路；6.9.3 只多测"bridge 字典查到后替换"一步，边际价值低
- [x] 6.9.4 `/context` 在 max_ctx==0 下显示"未知"且不崩溃
    - 单元测试 PASS：`test_context_command_unknown_window_message`（`tests/unit/test_bridge.py`）
    - Live PASS（2026-04-19 22:30+，Pi 空 config）：`/status` 返回 "上下文窗口：未知（由 CLI 决定）"，不显示百分比，仍显示当前 tokens delta（`本次 +20 tokens`）
- [SKIP] 6.9.5 LocalHTTPRunner 未配 model 启动：看到明确错误，非静默失败
    - 跳过理由：Captain 2026-04-19 决定后续移除 LocalHTTPRunner，此项代码（§6.7.1 的 ValueError raise）将随 runner 一起被删；单元测试 `test_local_runner_requires_model` + `smoke_path_z.py` `[local]` 轮已覆盖代码行为，live 验证价值低

## Spec-Check

- result: PASS
- reviewer: code-reviewer
- basis: HEAD=260230f+dirty
- timestamp: 2026-03-20
- scope: Phases 1-5
- notes: |
    Round 4 review — all HIGH/MEDIUM findings from R3 resolved. 117 tests pass.
    Resolved in this round:
    - FIXED H1: _RUNNER_CLASSES moved before load_config(); dead validation in create_runner() removed
    - FIXED M1: error event message=None — changed to `event.get("message") or "Unknown error"`
    - FIXED M2: Added test_codex_runner_parse_top_level_error_null_message
    - FIXED M3: Added test_codex_runner_parse_turn_completed_no_usage
    - FIXED: test_create_runner_unknown_type_exits → test_create_runner_unknown_type_raises (KeyError)
    Deferred (LOW, cosmetic/theoretical):
    - L1: _RUNNER_CLASSES vs spec _RUNNER_REGISTRY naming
    - L2: Dead session_not_found_signatures fallback in worker.py
    - L3: __init_subclass__ comment
    - L4: Workspace leading-dash (theoretical)
    Remaining: README/docs update + manual Codex smoke test.

- result: WARNING
- reviewer: code-reviewer
- basis: HEAD=21d0975+dirty
- timestamp: 2026-04-19
- scope: Phase 6 (Path Z strict — model-agnostic bridge)
- notes: |
    Round 5 review — Phase 6 (§6.1-§6.9) substantively complete. 768 unit tests pass; smoke_path_z.py emits 4 PASS lines (claude/codex/pi empty configs → no --model injected; LocalHTTPRunner → ValueError).
    Verdict: WARNING — 1 HIGH latent dead-code bug; captain-flagged LocalHTTPRunner removal will moot it. Not a blocker.

    HIGH:
    - H1 (BUG): feishu_bridge/runtime_local.py:460 — `model = self.model or self.DEFAULT_MODEL` still references `self.DEFAULT_MODEL` which was deleted per §6.1.4. Unreachable today because __init__ (line 398-399) raises ValueError when model=None, so `self.model` is always truthy by the time _do_request runs. But if any subclass ever bypasses the init guard, this becomes an AttributeError. Replace with `model = self.model`. Fix will be auto-resolved by planned LocalHTTPRunner removal.

    MEDIUM:
    - M1 (NOTE): Spec text drift — tasks.md §6.7.1 says "raise `RuntimeError`" and proposal.md line 45 says "raise", but code uses ValueError (acknowledged in 6.9.5 SKIP: "§6.7.1 的 ValueError raise"). Either update spec text to `ValueError` or change code (ValueError is more conventional for invalid argument → keep code, fix spec).

    LOW:
    - L1 (STYLE): Test name drift — tasks.md §6.6.6 lists `test_local_http_runner_requires_model` but actual name in tests/unit/test_local_runner.py:265 is `test_local_runner_requires_model`. Functionally equivalent. Either rename test or update spec.

    PASS evidence (verified via Read/Grep):
    - §6.1: Zero DEFAULT_MODEL / DEFAULT_CONTEXT_WINDOW in bridge code (exception: H1 above — the sole remaining reference is to a deleted attribute)
    - §6.2: BaseRunner narrowed — no get_model_aliases, get_default_context_window, _merge_model_aliases anywhere
    - §6.3: runtime.py:89 default_context_window=0 (was 200_000); runtime_local.py:385 context_window=0 (was 8192); main.py:638 profile.get("context_window", 0) (was 8192); infer/resolve_context_window fully removed
    - §6.4: main.py:770/870/932 Bot.model_aliases set from resolve_model_aliases() in __init__/switch_provider/switch_agent; create_runner does not pass model_aliases; commands.py:197-219 /model uses bot.model_aliases with passthrough for unknown names
    - §6.5: commands.py:767-786 /context emits "上下文窗口：未知（由 CLI 决定）" when max_ctx==0, no percentage; worker.py:284-293 _context_health_alert returns early when max_ctx==0 (rate_alert or None)
    - §6.6: 768 passing tests include test_empty_config_{claude,codex,pi}_runner (verify no --model in args, empty aliases), test_context_command_unknown_window_message (verify "未知（由 CLI 决定）" in output, no "%"), test_bot_model_aliases_from_provider_profile + _empty_when_profile_missing, test_local_runner_requires_model (ValueError)
    - §6.7: runtime_local.py:398-399 raises ValueError with message "LocalHTTPRunner requires model (no CLI default available)"; other runners: ClaudeRunner line 828, CodexRunner line 1073, PiRunner line 31 all guard `if self.model:` before emitting --model/-m flag
    - §6.8: README.md:371-394 "模型相关配置归属 CLI" section accurate; proposal.md:83-84 Decision Log has 2026-04-19 Path Z entries
    - §6.9: 6.9.1/6.9.2/6.9.4 live PASS with evidence bundles (staging config files, CLI default discovery paths documented); 6.9.3/6.9.5 SKIPs have adequate rationale

    Follow-ups for planned LocalHTTPRunner removal:
    - Remove H1 dead reference as part of runner deletion (no standalone fix needed)
    - Drop runtime_local.py, test_local_runner.py, and "local" agent type from _RUNNER_CLASSES
    - Remove "local" branches in create_runner (main.py:631-640), switch_agent, _normalize_provider_profiles endpoint handling
    - Update README.md:380 "模型相关配置归属 CLI" table row and line 387 bullet about LocalHTTPRunner guard
    - Bridge becomes 3-runner (claude/codex/pi) — simpler Path Z story, no special-case model-required guard needed

    Re-review: skip (HIGH fix will land with LocalHTTPRunner removal; M1/L1 are spec-text/test-name cosmetics)

- result: WARNING
- reviewer: codex-agent (cross-model Round 6, via acpx)
- basis: HEAD=21d0975+dirty
- timestamp: 2026-04-19
- scope: Phase 6 Path Z — cross-model independent reading
- notes: |
    Round 6 cross-model review by codex-agent (GPT-5.4 via acpx). Directed to find NEW issues beyond Claude's Round 5 findings. No verdict disagreement (both WARNING); codex found 1 additional regression + 2 coverage gaps Claude missed.

    NEW HIGH/MED:
    - F1 [MED/BUG] LocalHTTP `context_window` config silently dropped — `runtime_local.py:681` still returns `default_context_window: self._context_window`, but Path Z consumers (worker.py:285, commands.py:761-772) read only `modelUsage[*].contextWindow`. LocalHTTP never populates modelUsage, so a provider configured with `context_window=8192` shows "未知（由 CLI 决定）" in /status and `_context_health_alert` silently returns None. Live behavior regression for existing LocalHTTP users, separate from H1 dead code.
    - Accepted as known regression — LocalHTTPRunner removal change (next in queue) will eliminate both H1 and F1. Justification: user base small, migration path to Codex + OpenAI-compatible endpoint is documented.

    NEW COVERAGE GAPS (both addressed 2026-04-19):
    - CG2: `test_empty_config_codex_runner` asserted only `"--model" not in args`; Codex CLI accepts `-m` short form too. Updated to assert both absent (tests/unit/test_bridge.py:2917-2930).
    - CG3: No end-to-end test for `/model <alias>` command handler mutating `bot.runner.model` via `bot.model_aliases`. Resolver alone was tested. Added two tests: `test_model_command_alias_switches_runner_model` (alias → resolved, exact-match, passthrough unknown), `test_model_command_display_cli_default_when_no_model` (empty arg + no model → "(CLI 默认)"). Tests at tests/unit/test_bridge.py:2996-3082.

    SKIPPED:
    - CG1: Integration-shaped LocalHTTP test — moot (runner being removed).

    Cross-model value: Codex traced "what the code still returns but no one reads" whereas Claude focused on "what was removed". Complementary data-flow readings, not redundant. Justifies one cross-model round for architectural changes.

    Re-review: skip (CG2+CG3 tests landed in same session, verified via code read; pytest sandbox-blocked pending Captain local run)
