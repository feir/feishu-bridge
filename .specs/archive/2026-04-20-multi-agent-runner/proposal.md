---
branch: master
start-sha: 260230fb4ec518cfacffc6c7939bb4f10cc7461b
status: active
scope: HOLD
---

# Proposal: multi-agent-runner

## WHY

Bridge 自称"飞书 ↔ AI Agent 桥接器"，但代码深度耦合 Claude Code：命令构建（`claude -p --dangerously-skip-permissions --settings --output-format stream-json`）、流式协议（`stream_event/content_block_delta/result`）、模型列表（`claude-opus-4-6` 等）、settings 注入全部 hardcoded 在 `ClaudeRunner` 中。

要兑现"通用桥接器"的定位，需要抽象出 Agent 适配层。Codex（OpenAI）是第一个需要支持的非 Claude Agent，也是验证抽象是否正确的试金石。

**Phase 6 延伸（Path Z 严格版）**：完成 Phase 1-5 后，bridge 仍持有一批本应归 provider 自管的配置——每个 Runner 的 `DEFAULT_MODEL` 常量、内嵌 alias 字典（opus/sonnet/haiku/codex/pi/qwen/...）、`DEFAULT_CONTEXT_WINDOW` 常量、`infer_context_window()` 家族推断函数。这些值下游 CLI 发版都要跟着改，且 Pi（`~/.pi/models.json` 带每模型 `contextWindow`）、Codex（`~/.codex/config.toml` 带 `model` + profiles）已在 CLI 侧维护了完整的模型配置。让 bridge 继续持有相当于"bridge 替 provider 做它自己职责内的事"。Phase 6 把这些全部移出 bridge，Runner 接口同步收窄（移除 `get_model_aliases` / `get_default_context_window` 抽象方法，aliases 改由 Bot 层持有），让 bridge 退回到"只做 bridge 应做的事"。

## WHAT

1. **抽象 Runner 层** — `BaseRunner` 定义标准接口，`ClaudeRunner` / `CodexRunner` 各自实现差异点
2. **配置结构泛化** — `"claude": {...}` → `"agent": {"type": "claude|codex", ...}`，保持向后兼容
3. **配置向导适配** — 首次运行增加 Agent 类型选择
4. **命令层适配** — `/model` 按 Runner 类型提供模型列表，`/cost` `/context` 泛化
5. **文档更新** — README 和飞书文档同步

### 变更模块清单

| 模块 | 改动 | 量级 |
|------|------|------|
| `runtime.py` | 抽象 `BaseRunner` + 保留 `ClaudeRunner` + 新增 `CodexRunner` | **大** |
| `config.py` | 向导增加 Agent 选择；配置结构从 `claude` 泛化为 `agent` | 中 |
| `main.py` | Runner 工厂（按 type 自动选择）；配置验证泛化 | 中 |
| `commands.py` | `/model` 动态化，按 Runner 类型提供模型列表 | 小 |
| `worker.py` | 类型注解 `ClaudeRunner` → `BaseRunner`；context alert 泛化 | 小 |
| `data/bridge-settings.json` | 仅 Claude Code 使用，Codex 需忽略 | 小 |
| `README.md` + 飞书文档 | 更新配置示例和说明 | 小 |

### Phase 6 额外变更（Path Z 严格版）

| 模块 | 改动 | 量级 |
|------|------|------|
| `runtime.py` / `runtime_pi.py` / `runtime_local.py` | 删除 `DEFAULT_MODEL`、内嵌 alias 字典、`DEFAULT_CONTEXT_WINDOW`、`infer_context_window` / `resolve_context_window` | **中** |
| `runtime.py` | BaseRunner 抽象方法移除 `get_model_aliases` / `get_default_context_window`；删除 `_merge_model_aliases` helper；Runner 构造器删除 `model_aliases` 参数 | 中 |
| `runtime.py` | `RunResult.default_context_window` 默认 `200_000` → `0`（`0` 作为"未知"哨兵值） | 小 |
| `main.py` | Bot 实例新增 `model_aliases` 字段（从 provider profile 读取）；不再向 Runner 传 aliases；LocalHTTPRunner 启动时 model 为 None 则 raise `ValueError` | 中 |
| `commands.py` | `/model` 从 `self.bot.model_aliases` 解析后透传；`/context` 在 `max_ctx==0` 时显示"未知" | 小 |
| `worker.py` | context alert 在 `max_ctx==0` 时 early return | 小 |
| 单元测试 | 凡 assert `runner.get_model_aliases()` 的迁移到 Bot/commands；新增 empty-config 往返测试 | 中 |

## NOT

- **不做** OpenCode 支持（本次只做抽象 + Codex，OpenCode 留给后续验证）
- **不做** Codex 的 sandbox 策略自动映射（使用 `--dangerously-bypass-approvals-and-sandbox`）
- **不做** Codex 的预算追踪（Codex 无 `total_cost_usd`）
- **不做** 多 Agent 同 bot 混用（一个 bot 实例只绑定一个 Agent 类型）
- **不做** Codex 增量流式输出（Codex `--json` 仅输出完整事件，无 delta）
- **不做** Codex `/compact` 支持（Codex 无等价的上下文压缩命令；用户使用 `/compact` 时返回"此 Agent 不支持"）

## RISKS

| 风险 | 影响 | 缓解 |
|------|------|------|
| 抽象泄漏 — BaseRunner 接口被 Claude 假设污染 | 后续 Agent 适配困难 | Codex 作为第二实现验证接口设计 |
| Codex 流式体验差 | 用户等待时间长，无实时反馈 | typing indicator 持续到完成；文档说明差异 |
| 配置向后兼容 | 现有用户升级后配置失效 | 检测 `"claude"` key 自动迁移为 `"agent"` |
| Codex session 恢复协议不同 | Session 丢失或恢复失败 | `codex exec resume <sid>` 子命令适配 |
| ~~Codex 无 system prompt 机制~~ | ~~安全提示注入不可靠~~ | ~~prompt 前置拼接~~ → **已验证 `-c model_instructions_file` 可用** |
| Codex 模型名假设错误 | 模型别名失效 | 已验证实际模型：`gpt-5.2-codex`, `gpt-5.3-codex`, `gpt-5.4` 等 |
| Codex 安全层弱于 Claude | prompt-only 安全控制，无 settings 沙箱 | 使用 `--dangerously-bypass-approvals-and-sandbox`；仅 `model_instructions_file` 注入安全提示。README 明确说明 Codex 模式推荐可信用户环境 |

## Decision Log

| Date | Decision | Reason |
|------|----------|--------|
| 2026-03-20 | 配置 key 从 `claude` 改为 `agent`，保留旧格式自动迁移 | 语义准确 + 向后兼容 |
| 2026-03-20 | Codex 流式降级为完整事件输出，不模拟增量 | Codex `--json` 协议限制，模拟增量不可靠 |
| 2026-03-20 | ~~System prompt 通过 prompt 前置注入~~ → 改用 `-c model_instructions_file="<path>"` | 实测验证：Codex 支持通过配置文件注入指令，效果等价 `--append-system-prompt` |
| 2026-03-20 | 一个 bot 只绑定一个 Agent 类型 | 避免 session 管理、模型列表、命令路由的复杂度 |
| 2026-03-20 | Codex invalid session 静默创建新 session | 实测验证：resume 不存在的 thread_id 不报错，直接创建新 session（无需 `session_not_found_signatures`） |
| 2026-03-20 | Codex 默认模型改为 `gpt-5.2-codex` | 实测验证：实际模型体系为 `gpt-5.x-codex` 系列，非 `o4-mini` |
| 2026-03-20 | 统一返回结构使用 `RunResult` dataclass | 评审建议：避免裸 dict 导致字段遗漏，提升类型安全 |
| 2026-03-20 | Agent 类型通过 `agent.type` 配置字段显式指定 | 评审建议：不用 `shutil.which` 启发式猜测，避免误判 |
| 2026-04-19 | Path Z 严格版：删除所有 `DEFAULT_MODEL` / `DEFAULT_CONTEXT_WINDOW` / 内嵌 aliases；收窄 BaseRunner 接口移除 `get_model_aliases` 和 `get_default_context_window`；aliases 改由 Bot 层持有，context_window 未知时 `/context` 和 worker alert 静默降级 | Captain 指令：bridge 只做 bridge 该做的事，模型相关配置/能力参数（含 context length）属于 provider 自管范畴，`~/.codex/config.toml` 和 `~/.pi/models.json` 已在 CLI 侧维护完整信息 |
| 2026-04-19 | LocalHTTPRunner model 必填，其他三个 Runner `model=None` 时不传 `--model` 让 CLI 用自身默认 | LocalHTTPRunner 无 backing CLI 来决定默认，必须启动时明确失败；其他 Runner 的 CLI 侧（Claude 支持原生短名，Codex 用 config.toml，Pi 用 settings.json）已具备默认能力 |
| 2026-04-19 | LocalHTTPRunner removed — H1 + F1 resolved via file deletion | Path Z 严格版下 bridge 不再直连 HTTP 端点；本地 OpenAI-compatible 访问改用 `agent.type=codex` + `--oss --local-provider ollama\|lmstudio` 由 Codex CLI 转发。`agent.type='local'` 在 `load_config` 与 `switch_agent` 双入口 fail-fast，附带迁移提示指向 README §从 type=local 迁移。详见 `.specs/changes/remove-local-http-runner/` |
