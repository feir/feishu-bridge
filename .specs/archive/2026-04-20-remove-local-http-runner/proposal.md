---
branch: master
start-sha: 21d0975+dirty
status: active
scope: remove-only
---

# Proposal: remove-local-http-runner

## WHY

`LocalHTTPRunner`（2026-04-16 引入）与 Path Z 严格版（`multi-agent-runner` change, 2026-04-19）架构不契合：

1. **H1（死代码）**：`runtime_local.py:460` `model = self.model or self.DEFAULT_MODEL` 引用已在 §6.1.4 删除的 `DEFAULT_MODEL` 属性。当前 `__init__` 守卫 `model=None → ValueError` 挡住执行路径，是潜伏 bug。
2. **F1（行为回归）**：`runtime_local.py:681` 返回 `default_context_window: self._context_window`，但 Path Z 的 `/status`（`commands.py:761-772`）和健康告警（`worker.py:285`）只读 `modelUsage[*].contextWindow`。LocalHTTP 永远不填 `modelUsage`，配 `context_window=8192` 的用户看不到百分比，也不触发健康告警。
3. **架构冗余**：Codex 原生支持 OpenAI-compatible endpoint（`~/.codex/config.toml` 的 provider profile），LocalHTTP 覆盖的场景（连 Ollama / 本地 vLLM / LM Studio）完全可用 Codex + 自定义 endpoint 替代，且后者有 CLI 侧完整的模型/context 管理。
4. **维护成本**：保留 LocalHTTPRunner 需要为 Path Z 回头补 `modelUsage` 构建逻辑、重写 §6.1.4 遗留的死引用、专门的 model-required ValueError 守卫——所有这些在 runner 被移除后都自动消失。

## WHAT

一次性删除 LocalHTTPRunner 及其配套，bridge 收敛为 3-runner 架构（claude / codex / pi）。

### 变更模块清单

| 模块 | 改动 | 量级 |
|------|------|------|
| `feishu_bridge/runtime_local.py` | **删除整文件** | ~700 行 |
| `tests/unit/test_local_runner.py` | **删除整文件** | ~400 行 |
| `feishu_bridge/runtime.py` | `_RUNNER_CLASSES` 移除 `"local"` 条目；移除 `LocalHTTPRunner` 的 import | 小 |
| `feishu_bridge/main.py` | 清理 13 处：import / `_RUNNER_CLASSES` / `_LOCAL_PROMPT_DEFAULTS` / prompt defaults 选择 / `_normalize_prompt_config` docstring / command 解析 / default_cmd sentinel / `if != "local"` guard（3 处）/ `_prompt_raw` comment / create_runner 分支 / switch_agent 迁移分支 / load_config 迁移错误 / `_normalize_endpoint_config` 整函数删除 / `_normalize_provider_profiles` endpoint + local-extras 块删除 | 中 |
| `feishu_bridge/worker.py` | 删除 `_runner_type()` 中 `"local" in name` 分支 | 小 |
| `feishu_bridge/runtime.py` | 2 处 docstring 去掉 "LocalHTTPRunner overrides..." 提及 | 小 |
| `feishu_bridge/session_journal.py` | docstring 枚举去掉 `"local"` | 小 |
| `feishu_bridge/workflows/registry.py` | docstring 枚举去掉 `"local"` | 小 |
| `feishu_bridge/commands.py` | 无改动（`_RUNNER_CLASSES` 读取自动跟随 registry 收窄） | — |
| `tests/smoke_path_z.py` | `[local]` 轮次从"断言 create_runner ValueError"**转换**为"断言 load_config 迁移错误"（保留回归覆盖，从 4 runner 降为 3 runner + migration assertion） | 小 |
| `tests/unit/test_bridge.py` | 删除 8 个 LocalHTTP 专属测试 + 1 个 `LocalHTTPRunner` import；2 个 `switch_agent("local")` 测试改断言"未知 Agent 类型"/迁移错误 | 中 |
| `tests/unit/test_session_journal.py` | L238 parametrize 移除 `"local"` | 小 |
| `tests/unit/test_workflow_registry.py` | L146 `test_resolve_unsupported_for_local_runner` 改断言另一未知 runner → UNSUPPORTED（或删除） | 小 |
| `tests/unit/test_workspace_policy.py` | L71 `test_codex_and_local_share_non_claude_default` 缩窄为 codex only | 小 |
| `README.md` | L177 agent-type 注释去 `local`；L206 `/agent` 命令句去 `local`；L316 commands dict 示例去 `"local": "local"`；§"模型相关配置归属 CLI" 表格删除 LocalHTTP 行；L387 LocalHTTPRunner 要点删除；新增迁移指引（`"type": "local"` → `"type": "codex"` + `--oss --local-provider ollama`） | 中 |
| `.specs/changes/multi-agent-runner/proposal.md` Decision Log | 追加 `2026-04-19 LocalHTTPRunner removal` 条目，引用本 change | 小 |

### 用户迁移路径

旧配置：
```json
{
  "agent": {
    "type": "local",
    "base_url": "http://localhost:11434",
    "model": "qwen3.6:27b"
  }
}
```

新配置（Codex OSS 本地 provider；默认连 Ollama `http://localhost:11434`）：
```json
{
  "agent": {
    "type": "codex",
    "args_by_type": {"codex": ["--oss", "--local-provider", "ollama"]},
    "provider": "ollama",
    "providers": {"ollama": {"model": "qwen3.6:27b"}}
  }
}
```

**配置机制**：
- `--oss` 等价于 `-c model_provider=oss`，Codex CLI 内置 provider profile，无需 `OPENAI_BASE_URL` 环境变量
- `--local-provider ollama` 选择 Ollama（备选 `lmstudio`），默认连标准本地端口
- 非默认端口覆盖：在 `~/.codex/config.toml` 添加 `[model_providers.oss] base_url = "http://host:port"`，或 `args_by_type.codex` 追加 `-c model_providers.oss.base_url="http://host:port"`

加载层识别 `"type": "local"` 后 `log.error + sys.exit(1)` 给出迁移提示：
> agent.type='local' 已于 2026-04-19 移除（LocalHTTPRunner 被删除）。请参考 README §本地模型接入迁移至 `"type": "codex"` + `--oss --local-provider ollama` 配置。

## NOT IN SCOPE

- 不对 `BaseRunner` 接口做任何改动
- 不修改 Claude / Codex / Pi 任何一个 runner 的行为
- 不引入 back-compat shim（Captain 明确指示直接删）
- 不提供自动配置迁移脚本（用户需手动改 config，加载层会明确报错）

## Decision Log

- **2026-04-19**: Captain 确认移除 LocalHTTPRunner。触发条件：Round 6 codex-agent 交叉评审发现 F1 行为回归 + Round 5 Claude 评审发现 H1 死代码。一次移除解决两处问题，无 back-compat 成本。
