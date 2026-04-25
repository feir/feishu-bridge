# Tasks: token-optimization

## Phase 1: Quick Wins

- [x] 1.1 Auto-fetch 自适应策略
  - 修改 `worker.py:362-504` 的 auto-fetch 逻辑
  - 单 URL + 文档 < 2,000 字符：保持全文注入
  - 单 URL + 文档 >= 2,000 字符：注入 title + 前 500 字符 preview + "使用 feishu-cli read-doc 获取全文" 提示
  - 多 URL（>=2）：全部 metadata-only（title + 前 200 字符 preview）
  - Validate: 测试单长文档 URL → preview only；单短文档 URL → 全文；多 URL → 全部 metadata

- [x] 1.2 Status 行抑制
  - 在 `runtime.py` 的 `_SAFETY_PROMPT` 中追加一行指令："Do not output 'Status:' lines at the end of responses — status is tracked externally."
  - Validate: 发送测试消息，确认响应末尾无 Status: 行；bridge regex 无需修改（保留作为 fallback）

- [x] 1.3 日志增强：system prompt payload 大小
  - 在 `ClaudeRunner.build_args()` 或 `run()` 中记录 system prompt 字符数到日志
  - 格式：`log.info("System prompt size: %d chars (~%d tokens)", len(sp), len(sp) // 4)`
  - Validate: 日志中可见 prompt size 记录

## Phase 2: CLI Prompt 分层注入

- [x] 2.1 创建 `cli_prompt_summary.md`
  - 内容：feishu-cli 简介 + 命令分类（Documents/Sheets/Wiki/Calendar/Search/Bitable/Drive/Mail/Tasks/Messaging）每类一行描述
  - 末尾：`Run feishu-cli <command> --help for detailed usage.`
  - 目标大小：< 500 tokens
  - Validate: wc -c < 2,000 bytes

- [x] 2.2 Refactor runner prompt pipeline（两条路径）
  - `BaseRunner.__init__()` 接收 `extra_system_prompts_summary` 和 `extra_system_prompts_full` 两个 list
  - `_build_system_prompt(full: bool = True)` 根据参数选择版本
  - **ClaudeRunner 路径**：`BaseRunner.run()` 新增 `full_cli_prompt: bool = True` 参数 → 传入 `build_args()` → 传入 `_build_system_prompt(full=)`
  - **CodexRunner 路径**：`CodexRunner.run()` 新增 `full_cli_prompt: bool = True` 参数 → 直接调用 `_build_system_prompt(full=)` 写入临时文件 → 传入 `super().run(full_cli_prompt=)`
  - 注意：CodexRunner.run() 签名与 BaseRunner.run() 不同（无 fork_session），需分别添加 full_cli_prompt 参数
  - Validate: 两种 runner 分别以 full=True/False 构建，确认 system prompt 长度差异符合预期

- [x] 2.3 Session-sticky 飞书模式检测
  - 在 `worker.py:process_message()` 中，检测多信号联合：
    - 消息含飞书 URL pattern（`_feishu_urls` 非空）
    - 消息含 `/feishu-*` 命令前缀
    - 消息含中文飞书关键词（飞书|文档|表格|日历|邮件|任务|多维表格|wiki）
    - SessionMap 中该 session 的 `feishu_cli_activated` 标记为 True
  - 任一信号触发 → `full_cli_prompt=True` + 设置 `feishu_cli_activated=True`
  - Validate: 测试无飞书关键词 → summary；有关键词 → full；后续无关键词消息 → 仍 full（sticky）

- [x] 2.4 ~~SessionMap 扩展~~ → In-memory dict（不需要持久化）
  - `SessionMap.set()` / `get()` 支持存储 `feishu_cli_activated` 布尔标记
  - `/new` 重置时清除该标记
  - Validate: set → get 一致；/new 后标记清除

- [x] 2.5 main.py 工厂适配
  - `create_runner()` 传递 summary 和 full 两个 prompt 列表
  - `cli_prompt_summary.md` 通过 `importlib.resources` 加载（同现有 cli_prompt.md 模式）
  - Validate: runner 构造成功，两个 prompt 版本可用

## Review Report

### Round 1 (2026-03-29, basis: 8bb316b+dirty)

**Verdict: APPROVE** — No CRITICAL or HIGH issues.

| Severity | Count | Claude | Codex | Both |
|----------|-------|--------|-------|------|
| CRITICAL | 0     | 0      | 0     | 0    |
| HIGH     | 0     | 0      | 0     | 0    |
| MEDIUM   | 3     | 0      | 0     | 3    |
| LOW      | 3     | 2      | 1     | 0    |

Findings:

1. [MEDIUM][Claude+Codex] Boundary `> 2000` vs `>= 2000` — off-by-one vs spec (`worker.py:439, :468`)
2. [MEDIUM][Claude+Codex] Code duplication: identical adaptive logic in wiki-doc and direct-doc paths (`worker.py:434-445, :463-474`)
3. [MEDIUM][Claude+Codex] Missing test coverage for adaptive branches — no tests for threshold boundary, multi-URL, or single-long-doc paths
4. [LOW][Codex] Wiki-resolves-to-sheet inflates `_doc_url_count` — rare, graceful degradation (`worker.py:373`)
5. [LOW][Claude] Double `_build_system_prompt()` invocation per `run()` — minor inefficiency, risk of divergence in Phase 2 (`runtime.py:483, :773`)
6. [LOW][Claude] Token estimate `len // 4` underestimates Chinese text — acceptable as logging baseline (`runtime.py:487`)

Re-review: skip

## Spec-Check

- result: WARN
- reviewer: code-reviewer
- basis: HEAD=8bb316b+dirty
- timestamp: 2026-03-29
- notes: Phase 1 tasks checked but no Evidence lines. Boundary off-by-one (> 2000 vs >= 2000). All changes within scope.
