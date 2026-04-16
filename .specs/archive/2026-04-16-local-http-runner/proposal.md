---
branch: master
start-sha: 1708c7fb51d48fa62944db07375b1a2581bd5167
status: active
scope: SINGLE
---

# Proposal: local-http-runner

## WHY

Claude CLI 框架自身的 system prompt + tools schema 占 ~26K tokens，对本地小模型（gemma-4-26b @ omlx）造成致命 prefill 开销：每条简单消息固定 ~100s 延迟。直接 HTTP 调用同模型同问题仅 14 tokens / 0.8s，差距 1850x。

**实测对比（2026-04-16）**：

| 路径 | input tokens | 延迟 |
|------|-------------|------|
| Claude CLI → omlx | 25,900 | ~100s |
| 直接 HTTP → omlx | 14 | 0.8s |

`--setting-sources ""` 已屏蔽 user-scope（CLAUDE.md、rules、hooks），bridge 注入仅 28 tokens。剩余 26K 全是 Claude CLI 框架不可压缩的内置 prompt。

bridge 已支持 claude / codex 两种 agent type，但都假设有外部 CLI 进程。本地小模型场景需要绕过 CLI 框架，直接走 HTTP API。需求不限于 omlx——ollama、vllm、任何 OpenAI/Anthropic 兼容的本地端点都适用。

## WHAT

新增 `local` agent type — 通用 HTTP LLM runner，按 protocol 适配多种本地端点。

1. **新 Runner 类** — `LocalHTTPRunner`（继承 `BaseRunner`），实现纯 HTTP 调用
2. **协议适配层** — 支持 `anthropic`（/v1/messages）和 `openai`（/v1/chat/completions）两种协议
3. **Session in-memory + 跨重启自愈** — `session_id → messages list`；进程重启后 runner 通过 `has_session(sid)` 告知 worker，worker 自动降级为新会话并给用户友好提示
4. **Prompt 膨胀硬默认关闭** — `type=local` 时 `feishu_cli=False`、`cron_mgr=False`、`safety=minimal` 作为硬默认，不依赖用户显式配置
5. **AuthFile 钩子** — `wants_auth_file()` 钩子让 worker 在 local runner 不需要时跳过 `/tmp/feishu_auth_*.json` 创建
6. **配置注册** — `_RUNNER_CLASSES["local"] = LocalHTTPRunner`
7. **Command 校验放宽** — `local` agent type 在三个 PATH 校验点全部跳过 `shutil.which`

### 变更模块清单

| 模块 | 改动 | 量级 |
|------|------|------|
| `runtime_local.py` (新文件) | `LocalHTTPRunner` + 协议适配 + `_SessionStore` + `_HTTPCall` | **大** (~300 行) |
| `runtime.py` | `BaseRunner.wants_auth_file() = True` hook；`has_session()` hook | 小 (~20 行) |
| `main.py` | 注册 `local` runner；放宽 3 处 PATH 校验；local type 硬默认 prompt 配置；endpoint 字段 normalize | 中 (~40 行) |
| `worker.py` | `process_message` 检查 `runner.has_session(sid)`；`_context_health_alert` 按 `supports_compact()` 门控 `/compact` 提示；`_write_auth_file` 按 `wants_auth_file()` 门控 | 中 (~30 行) |
| `commands.py` | `/agent` 选项从 `_RUNNER_CLASSES` 派生；`/status` quota 节按 ClaudeRunner 门控；空 model_aliases 省略"可选"行 | 小 (~15 行) |
| `tests/unit/test_local_runner.py` (新文件) | Adapter + Session + Cancel + Mock HTTP | 中 (~200 行) |
| `tests/unit/test_bridge.py` | 5 个集成测试（load_config local / switch_agent / switch_provider / stale sid / cost store） | 小 (~100 行) |

## NOT

- **不支持** tools / function calling（小模型不需要，且增加协议复杂度）
- **不支持** skills / hooks / `/compact`（依赖 Claude CLI 框架）
- **不支持** session 跨进程持久化：Session 仅在 bridge 进程内保留；进程重启后 in-memory store 清空，worker 通过 `has_session()` 显式检测并提示用户 `⚠️ 会话已重建`，不会静默丢失上下文
- **不支持** 流式 tool_use 解析
- **不支持** budget tracking（本地推理无金钱成本）
- **不实现** ollama 原生 `/api/chat` 协议（ollama 已有 OpenAI 兼容端点）
- **不改** 现有 ClaudeRunner / CodexRunner 行为
- **不改** BaseRunner 的 subprocess 抽象结构（follow-up 可拆 RunnerProtocol + SubprocessRunner）

## RISKS

| 风险 | 影响 | 缓解 |
|------|------|------|
| ~~Session in-memory 进程重启丢失~~ | ~~用户感知静默断开~~ | **通过 `has_session(sid)` + worker 自愈 + `⚠️ 会话已重建` 提示显式处理**（见 Decision Log 2026-04-16 row 6） |
| HTTP 取消依赖 connection close | 服务端可能继续推理浪费本地 GPU | 本地单用户可控，acceptable；明确文档 |
| SSE 解析与 Anthropic / OpenAI 协议差异 | 流式内容拼接错误 | 独立 Adapter；单元测试覆盖两种协议 |
| 协议字段命名差异（input_tokens vs prompt_tokens） | usage 统计不准 | Adapter 统一映射 |
| OpenAI `stream_options.include_usage` 兼容性 | 旧 ollama/vllm 返回 400 或无 usage | 配置 toggle（默认 True）；usage 缺失报 0；400 时 fallback 重试无此字段 |
| Wall-clock timeout 缺失 | 持续输出的慢流超过 self.timeout | stream 循环检查 `time.monotonic() - t0 > timeout`，cancel 并返回 timeout error |
| 端点不可用（omlx 未启动） | 调用失败 | 返回友好错误，提示 endpoint URL |
| BaseRunner 接口与 HTTP 模型阻抗不匹配 | 强行复用会引入 wrapping 假对象 | 覆盖 `run()` 整个方法，stub 三个 abstract；模块级注释 + 不可达断言测试 |
| Bridge 注入 feishu-cli / cron-mgr prompt 抵消减负收益 | local runner 仍收到数千 token 污染 | type=local 硬默认 `feishu_cli=False, cron_mgr=False, safety=minimal` |
| `/compact` 建议出现在不支持的 runner 上 | UX 混乱 | `_context_health_alert` 按 `supports_compact()` 门控提示文案 |
| FEISHU_AUTH_FILE 文件泄漏 | local runner 无消费者仍写 /tmp 文件 | `wants_auth_file()` 钩子，local 默认 False |

## Decision Log

| Date | Decision | Reason |
|------|----------|--------|
| 2026-04-16 | agent type 命名 `local` | 用户决定；贴近"本地 LLM"使用场景 |
| 2026-04-16 | 不限定 omlx，支持任意 HTTP endpoint | 用户决定；ollama/vllm/本地 OpenAI 兼容服务通用 |
| 2026-04-16 | Session 仅 in-memory | 本地对话重启可接受；避免 SQLite 依赖 |
| 2026-04-16 | Protocol 字段在 provider config 显式声明（anthropic/openai） | 不靠 URL 启发式猜测 |
| 2026-04-16 | 不实现 tools / skills / hooks | 本次目标"轻量纯对话"，保留会重新引入 prompt 膨胀 |
| 2026-04-16 | 第一版同时实现 anthropic + openai 两协议 | 评审建议；逻辑相似，避免后续重新评审 |
| 2026-04-16 | Session 跨重启使用 `has_session()` 钩子 + worker 自愈（方案 a） | 评审 HIGH#1；最小改动、与 `session_not_found_signatures` 自愈模式一致 |
| 2026-04-16 | `type=local` 硬默认 `feishu_cli=False, cron_mgr=False, safety=minimal` | 评审 HIGH#2；零配置默认正确性优于文档约定 |
| 2026-04-16 | 硬默认通过 `_normalize_prompt_config(agent_type=...)` 内置实现，不再用 post-normalize `_apply_local_defaults` helper | 二轮评审 HIGH#1：post-normalize setdefault 是 no-op，因为 `fill_defaults=True` 已填满全部 key；必须在 normalize 阶段切换 base defaults |
| 2026-04-16 | Worker 集成点全部对应 module-level 函数（`process_message` / `_context_health_alert` / `_write_auth_file`），通过参数注入 `runner` / `session_map` | 二轮评审 HIGH#2：纠正 round-1 设计中误用 `self.*` 的伪代码 |
| 2026-04-16 | 新增 `wants_auth_file()` BaseRunner 钩子（默认 True） | 评审 HIGH#3；避免 local runner 引发 /tmp 文件泄漏 |
| 2026-04-16 | `local` bypass 应用于 `resolve_effective_agent_command` + `load_config` + `switch_provider` + `switch_agent` 四处 PATH 校验 | 评审 HIGH#4；真实代码 3+1 处全部覆盖 |
| 2026-04-16 | Wall-clock timeout 在 streaming 循环显式检查 `time.monotonic()` | 评审 MEDIUM#6；`urlopen(timeout=)` 只控 socket idle 不控总时长 |
| 2026-04-16 | `OpenAIAdapter.include_usage` 为 provider 配置 toggle（默认 True），400 时 fallback | 评审 MEDIUM#5；ollama/vllm 兼容性保护 |
| 2026-04-16 | 不拆 BaseRunner 为 RunnerProtocol + SubprocessRunner（本次） | 评审 MEDIUM#2；本次接受 stub 3 abstract + 文档注释，拆分留 follow-up |
