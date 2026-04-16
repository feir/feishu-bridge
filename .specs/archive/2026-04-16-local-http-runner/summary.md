# Summary: local-http-runner

## 概要

新增 `local` agent type，通过纯 HTTP 绕过 Claude CLI 框架，直连本地 LLM 端点（omlx/ollama/vllm 等）。解决 Claude CLI 固定 ~26K token system prompt 开销导致的 ~100s 延迟问题（实测直连 14 tokens / 0.8s，改善 1850x）。

## 变更内容

| 文件 | 变更 | 量级 |
|------|------|------|
| `feishu_bridge/runtime_local.py` (新) | `LocalHTTPRunner` + `_SessionStore` + `_HTTPCall` + `_sse_iter` + `AnthropicAdapter`/`OpenAIAdapter` | 644 行 |
| `feishu_bridge/main.py` | `_normalize_prompt_config(agent_type=)` + local bypass + endpoint normalize + create_runner local branch + switch re-normalize | +188 行 |
| `feishu_bridge/runtime.py` | `has_session()` + `wants_auth_file()` hooks on BaseRunner | +18 行 |
| `feishu_bridge/worker.py` | stale-sid auto-heal + auth-file gate + compact hint swap | +27 行 |
| `feishu_bridge/commands.py` | /agent 动态选项 + /status quota 门控 + /model 空别名省略 | +29 行 |
| `tests/unit/test_local_runner.py` (新) | Adapter + SSE + Session + Cancel + Mock HTTP 共 24 tests | 新建 |
| `tests/unit/test_bridge.py` | 9 个集成测试 + 2 个缺失测试补齐 | +366 行 |

## 关键决策

- `_normalize_prompt_config(agent_type=)` 内置 local 默认值（feishu_cli=False / cron_mgr=False / safety=minimal），取代 post-normalize no-op helper
- `_prompt_raw` 备份原始 prompt 供 switch_agent/switch_provider re-normalize（修复 C1 审查 BUG）
- SSE parser 独立 `_sse_iter` generator，处理 CRLF/heartbeat/multi-line/[DONE]/partial frame
- `_active[tag]` 在 urlopen 前注册，socket_timeout ≤ 2s，cancel 延迟上限确定
- 经双模型评审（Claude + Codex GPT-5.4）两轮，修复 C1 CRITICAL + H1-H4 HIGH

## 影响范围

- ClaudeRunner / CodexRunner 行为零变化（默认 hook 返回 True）
- 345 tests passed，3 个 pre-existing failure（interactive_cards / update_doc CLI 环境问题）与本次无关
- Phase 7 手动验证待执行（T7.1-T7.9）
