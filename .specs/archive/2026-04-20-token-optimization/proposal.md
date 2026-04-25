---
branch: master
start-sha: 8bb316b850e8a96ff1e20bebe5aa45da84e211b2
status: active
---

# Proposal: token-optimization

## WHY

Bridge 在每次 CLI 调用时通过 `--append-system-prompt` 注入 ~3,500 tokens 的 CLI prompt，无论消息是否涉及飞书操作。Auto-fetch 对消息中的飞书 URL 无条件拉取全文（截断到 8,000 字符 ≈ 2,500 tokens/URL）。Status 行每条响应产生 ~15 output tokens 后被 regex 丢弃。三项叠加在非飞书操作的日常对话中造成不必要的 context window 消耗，加速 auto-compact 触发。

## WHAT

### Phase 1: Quick Wins
1. **Auto-fetch 自适应策略**：单 URL + 短文档（< 2,000 字符）保持全文 fetch；长文档改为 title + 预览（500 字符）；多 URL 全部 metadata-only
2. **Status 行抑制**：在 safety prompt 中添加指令禁止输出 Status 行
3. **日志增强**：在 runner 日志中记录 system prompt payload 大小，为 Phase 2 提供基线数据

### Phase 2: CLI Prompt 分层注入
1. 拆分 `cli_prompt.md` 为 `cli_prompt_summary.md`（~400 tokens）+ 完整版
2. Runner 持有两个 system prompt 版本（summary / full）
3. Session-sticky 升级逻辑：默认 summary，多信号触发后 sticky full（信号：飞书 URL / `/feishu-*` 命令 / 中文飞书关键词 / session 内曾使用 feishu-cli 工具）
4. SessionMap 增加 `feishu_cli_activated` 标记

## NOT

- 不改变 `--append-system-prompt` 的基础机制
- 不改变 session 管理核心逻辑（session_map、resume/fork）
- 不改变 streaming / card UI 层
- 不改变合并转发消息展开逻辑
- 不改变 cron-mgr prompt 注入方式（独立优化）

## Acceptance Criteria

- [ ] 不含飞书关键词的消息，system prompt 中 CLI 部分 < 500 tokens（Phase 2）
- [ ] 含飞书关键词或 session 曾触发飞书操作的消息，system prompt 完整注入（Phase 2）
- [ ] 飞书 URL auto-fetch：单 URL 短文档保持全文；长文档/多 URL 仅注入 metadata + preview（Phase 1）
- [ ] Claude 仍可通过 feishu-cli read-doc 获取全文（能力未丧失）
- [ ] Status 行不再出现在 LLM 输出中（Phase 1）
- [ ] 现有功能不受影响：命令处理、session 管理、streaming、card UI 均正常

## Approaches Considered

### Approach A: Conditional Summary/Full + Auto-fetch Adaptive（selected, refined by Codex review）
- Summary: 按消息内容 + session 历史多信号检测，条件注入 summary 或 full CLI prompt；auto-fetch 自适应；status 行抑制
- Effort: M    Risk: Low
- Pros: 非飞书消息节省 ~3,100 input tokens/轮 + ~2,000 input tokens/URL；飞书消息零降级；session-sticky 消除关键词漏判
- Cons: 需要 refactor runner 的 prompt pipeline（_extra_system_prompts 从 startup-static 改为 per-message selectable）

### Approach B: Minimal — 仅 Auto-fetch + Status 行
- Summary: 只做 auto-fetch 自适应和 status 行抑制，不动 CLI prompt
- Effort: S    Risk: Very Low
- Pros: 改动最小，零回归风险
- Cons: 最大浪费点（CLI prompt 3,500 tokens/轮）未解决

### Approach C: --help Discovery — 彻底移除 system prompt 注入
- Summary: System prompt 仅含一行提示，Claude 通过 feishu-cli --help 自行发现命令
- Effort: S    Risk: Med
- Pros: 近零 system prompt 开销
- Cons: 每次新 session 首次飞书操作多一次 tool call（~2s + ~500 output tokens）

**Selected: A (two-phase delivery)** — Phase 1 先交付 easy wins（B 的内容 + 日志基线），Phase 2 再做 CLI prompt 分层。Codex review 确认方向正确，补充了 session-sticky 升级和自适应 auto-fetch 策略。

## RISKS

| 风险 | 缓解 |
|------|------|
| Session-sticky 标记在 session 恢复失败时丢失 | SessionMap 已有 session-not-found 自动恢复机制，新 session 默认 summary，无降级 |
| Auto-fetch 自适应判断文档长度需要额外 API 调用 | 利用已有的 fetch 响应：先获取 markdown，检查长度后决定截断策略 |
| Status 行抑制指令被 CLAUDE.md 覆盖 | `--append-system-prompt` 内容在 system prompt 最末，优先级高 |
| CLI prompt summary 不够 Claude 使用 feishu-cli | Summary 包含命令名 + 一句话描述 + `--help` 提示，足够触发工具调用 |

## Decision Log

| Date | Decision | Reason |
|------|----------|--------|
| 2026-03-29 | 采用 two-phase delivery | Codex review 建议先交付 easy wins，积累基线数据后再做 prompt 分层 |
| 2026-03-29 | 关键词检测改为 session-sticky 多信号联合 | Codex 指出纯关键词检测 false negative 风险（如"继续改刚才那个文档"） |
| 2026-03-29 | Auto-fetch 保留单 URL 短文档全文 fetch | Codex 指出 metadata-only 对"帮我看看这个文档"场景体验降级 |
