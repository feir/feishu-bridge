# Tasks: local-http-runner

> Decisions inherited from `proposal.md` Decision Log — no open items.

## Phase 1 — Core Implementation

- [x] T1.1 创建 `feishu_bridge/runtime_local.py`
  - `_SessionStore` (in-memory LRU, `exists()` hook)
  - `_HTTPCall` (cancel + socket/wall-clock timeout)
  - `ProtocolAdapter` ABC + `AnthropicAdapter` + `OpenAIAdapter`
  - `LocalHTTPRunner(BaseRunner)` 完整 `run()` override
  - 模块级注释说明 stub 的 3 个 abstract 方法
  - `has_session()` / `wants_auth_file()` / `supports_compact()=False` overrides
- [x] T1.2 SSE 解析器（标准库 urllib，无新依赖）
- [x] T1.3 Cancel + wall-clock timeout 流式循环检查
- [x] T1.4 OpenAIAdapter `include_usage` toggle + 400 fallback 重试
- [x] T1.5 错误处理：endpoint 不可达、HTTP 4xx/5xx、超时、JSON 解析失败 → 结构化 error result

## Phase 2 — BaseRunner Contract Extension

> **Execution order**: Phase 2 MUST land before Phase 4 (worker calls these hooks).
> Phase 1 and Phase 2 can run in parallel; Phase 4 depends on both.

- [x] T2.1 `runtime.py` BaseRunner 新增 `has_session(sid) -> bool` (默认 True)
- [x] T2.2 `runtime.py` BaseRunner 新增 `wants_auth_file() -> bool` (默认 True)
- [x] T2.3 ClaudeRunner / CodexRunner 保持默认实现（无行为变化）

## Phase 3 — Main / Config Integration

- [x] T3.1 `main.py` 注册 `_RUNNER_CLASSES["local"] = LocalHTTPRunner`
- [x] T3.2 `resolve_effective_agent_command` 加 `local` bypass（单一 source of truth）
- [x] T3.3 `load_config` + `switch_provider` + `switch_agent` 调用点全部使用 `resolve_effective_agent_command`（必要时重构绕开重复的 `shutil.which`）
- [x] T3.4 `_normalize_prompt_config` 新增 `agent_type` 参数；`agent_type=="local"` 时 base defaults 切到 `(feishu_cli=False, cron_mgr=False, safety=minimal)`；所有调用点传入 `agent_type`（load_config / resolve_prompt_config / _normalize_provider_profiles / switch_provider / switch_agent re-normalize）
- [x] T3.5 `_normalize_endpoint_config` — endpoint 字段 normalize（base_url / protocol / api_key）；协议非法 raise ConfigError
- [x] T3.6 `_normalize_provider_profiles` 吸收 `max_tokens / context_window / openai_include_usage / model_aliases`
- [x] T3.7 `create_runner` — type=local 分支传 endpoint kwargs 到 LocalHTTPRunner

## Phase 4 — Worker Integration

- [x] T4.1 `worker.py:process_message` 调用 `runner.has_session(sid)`，返回 False 时 resume 降级 + 插入 `⚠️ 会话已重建` 提示
- [x] T4.2 `worker.py:_write_auth_file` 调用前检查 `runner.wants_auth_file()`
- [x] T4.3 `worker.py:_context_health_alert` 按 `runner.supports_compact()` 门控 `/compact` 提示文案
- [x] T4.4 验证现有 `_write_auth_file` 是否有清理机制，若无则按需补（local 默认跳过，所以此项仅影响 claude/codex）

## Phase 5 — Commands UI

- [x] T5.1 `/agent` help 文案从 `_RUNNER_CLASSES.keys()` 派生
- [x] T5.2 `/status` quota 节 `isinstance(self.runner, ClaudeRunner)` 门控
- [x] T5.3 `/model` 当 `get_model_aliases()` 空时省略"可选"行

## Phase 6 — Tests

- [x] T6.1 `tests/unit/test_local_runner.py` — Adapter 协议（4 cases）
- [x] T6.2 同上 — Session store（5 cases，含 `exists()`）
- [x] T6.3 同上 — Cancel / socket timeout / wall-clock timeout（3 cases）
- [x] T6.4 同上 — Mock HTTP e2e（4 cases，含 OpenAI 400 fallback）
- [x] T6.5 同上 — BaseRunner stub 不可达断言（1 case）
- [x] T6.6 `tests/unit/test_bridge.py` — 9 个集成测试（见 design.md §测试策略 Integration）
  - `test_load_config_local_type`
  - `test_switch_agent_to_local`
  - `test_switch_provider_to_local_endpoint`
  - `test_process_message_stale_local_sid`
  - `test_cost_store_no_ops_for_local`
  - `test_local_no_auth_file` — `wants_auth_file()` 为 False 时 `_write_auth_file` 不被调用，无 /tmp 文件残留
  - `test_local_compact_hint_omitted` — `_context_health_alert(runner=LocalHTTPRunner())` 返回文案不含 `/compact`
  - `test_local_build_extra_prompts_empty` — type=local 走 `build_extra_prompts` 返回不含 feishu-cli / cron-mgr 段
  - `test_switch_to_local_resets_prompts` — 从 claude 切到 local 后 `next_cfg["prompt"]` 已重 normalize 为 local 默认
- [x] T6.7 跑全套 `pytest tests/unit/` 验证无回归

## Phase 7 — Manual Verification

- [ ] T7.1 用户添加 `omlx-local` provider 到 `~/.config/feishu-bridge/config.json`
- [ ] T7.2 重启 bridge → `/agent local` + `/provider omlx-local`
- [ ] T7.3 "hi" → 延迟 ≤ 2s，omlx 日志 `input_tokens ≤ 100`
- [ ] T7.4 多条消息测 session 累积上下文
- [ ] T7.5 重启 bridge → 同 chat 再发消息 → 验证 `⚠️ 会话已重建` 提示出现
- [ ] T7.6 飞书取消按钮测长消息 → HTTP 连接断开 + 返回"已取消"
- [ ] T7.7 `kill omlx` → 发消息 → 收到友好 endpoint 错误
- [ ] T7.8 `/agent claude` 切回验证 ClaudeRunner 无回归
- [ ] T7.9 验证 /tmp 无 stale `feishu_auth_*.json` 堆积（仅 claude/codex 下创建）

## Spec-Check

- result: PASS
- basis: HEAD=1708c7fb51d48fa62944db07375b1a2581bd5167+dirty
