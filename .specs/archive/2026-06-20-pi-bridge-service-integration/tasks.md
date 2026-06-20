# Tasks: pi-bridge-service-integration

## 1. Bridge: Worker 环境变量注入

- [x] 1.1 `worker.py` `env_extra` 新增 `FEISHU_USER_OPEN_ID`、`FEISHU_BOT_NAME`、`FEISHU_CONTROL_SOCKET`、`FEISHU_CONTROL_TOKEN`
  - 其中 `FEISHU_BOT_NAME` 从 auth-file 分支提升为无条件注入
  - `FEISHU_CONTROL_SOCKET`/`FEISHU_CONTROL_TOKEN` 使用 `bg_root` 计算绝对路径
  - Evidence: python3 py_compile worker.py → PASS

## 2. Bridge: Wrapper dispatch()（仅暴露只读 action）

- [x] 2.1 `api/tasks.py` 新增 `dispatch(action, chat_id, sender_id, **kwargs)`
  - 只读 action: `list_tasks`, `get_task`, `list_subtasks`, `list_tasklists`, `summary`
  - 归一化返回 `{ok, data/error}`，内部 catch 所有异常
  - Evidence: dispatch unsupported_action guard → {ok:false, error:unsupported_action} ✓

- [x] 2.2 `api/sheets.py` 新增 `dispatch(action, chat_id, sender_id, **kwargs)`
  - 只读 action: `info`, `read`
  - Evidence: dispatch unsupported_action guard ✓

- [x] 2.3 `api/docs.py` 新增 `dispatch(action, chat_id, sender_id, **kwargs)`
  - 只读 action: `fetch`
  - Evidence: dispatch unsupported_action guard ✓

- [x] 2.4 `api/bitable.py` 新增 `dispatch(action, chat_id, sender_id, **kwargs)`
  - 只读 action: `list_records`, `get_record`, `list_fields`, `list_views`, `list_tables`, `get_view`
  - Evidence: dispatch unsupported_action guard ✓

## 3. Bridge: Control API call_service

- [x] 3.1 `control_api.py` 的 `_build_dispatcher()` 内新增 `_call_service(params)` 闭包
  - 权限校验 + service 路由 + dispatch 调用
  - Evidence: python3 py_compile control_api.py → PASS

- [x] 3.2 `control_api.py` 在 `_CAPABILITIES` 中注册 `"call_service"`，dispatch dict 中注册 `"call_service": _call_service`
  - Evidence: `"call_service"` 已在 _CAPABILITIES 和 dispatch dict 中

## 4. Pi: feishu-bridge skill

- [x] 4.1 新建 `~/.pi/agent/skills/feishu-bridge/scripts/call.py`
  - env 读取 + socket 通信 + 超时 + JSON 输出
  - Evidence: python3 py_compile call.py → PASS

- [x] 4.2 新建 `~/.pi/agent/skills/feishu-bridge/SKILL.md`
  - Frontmatter + 只读 action 列表 + fallback 说明
  - Evidence: 文件已写入，frontmatter 含 `runners.pi: bridge_workflow`

- [x] 4.3 新建 `~/.pi/agent/skills/feishu-bridge/workflow.yaml`
  - `ttl: "30m"`、`version: 1`
  - Evidence: 文件已写入

## 5. 端到端验证

- [x] 5.1 首次调用：tasks list_tasks 返回真实数据
  - Evidence: `call.py --service tasks --action list_tasks` → ok:true, 50 tasks returned

- [x] 5.2 二次调用（有缓存 token）：无需重新授权
  - Evidence: `call.py --service tasks --action summary` → ok:true, 4 tasklists + 10 pending tasks

- [x] 5.3 权限拒绝：非活跃 chat_id 被拦截
  - Evidence: fake chat_id → ok:false, error:unauthorized, message:会话未激活或已过期

- [x] 5.4 异常路径：各 guard 行为正确
  - Evidence: unsupported_action → proper rejection for all 4 services; unknown_service → proper error; socket_unreachable → ok:false

### Round 1 (2026-06-20, basis: ffb2422)

Codex code-review findings:

**[CRITICAL]** `call_service` trusts caller-supplied `sender_id`
- File: control_api.py:262
- Issue: RPC validates only chat_id active, then passes caller-controlled sender_id to wrapper auth
- Captain decision: DISMISSED — local Unix socket (0600) + token file (0600) + active-session check is sufficient access control for this MVP; any process that can read the token already has equivalent access

**[HIGH]** Pi-side skill files appear missing
- Verdict: FALSE POSITIVE — skill files exist at `~/.pi/agent/skills/feishu-bridge/` (outside git repo, not in project tree scanned by Codex)

**[HIGH]** New RPC and dispatch paths have no tests
- Verdict: ACCEPTED for Phase 1 — pytest-based unit tests deferred to bridge restart validation cycle; dispatch guard logic verified via direct Python import tests

No SCOPE drift detected.

## Spec-Check

- result: PASS
- reviewer: code-review
- basis: HEAD=020957e
- timestamp: 2026-06-20
- notes: All tasks 1.1-5.4 complete with evidence. Bridge auto-updated to v2026.06.20. E2E verified: tasks list_tasks/summary both return real data via call_service, unauthorized guard rejects fake chat_id, all 4 wrappers reject unsupported actions, unknown service returns proper error. Scope clean per git diff. Codex CRITICAL dismissed by captain.
