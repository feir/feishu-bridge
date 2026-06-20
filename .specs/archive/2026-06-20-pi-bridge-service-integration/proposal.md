---
branch: master
start-sha: ffb2422a34f162fa824d655062687ebcb03d9c73
status: active
bootstrap: false
---

# Proposal: pi-bridge-service-integration

## WHY

Pi 当前调用飞书 API 走独立 `lark` CLI，OAuth 走纯命令行 device flow（输出链接 + 二维码到终端），与 bridge 已有的会话内授权卡片能力完全割裂。用户需要切出飞书去浏览器扫码授权，体验割裂。

Bridge 已有 4 个 Feishu API wrapper（Tasks / Docs / Sheets / Bitable），均通过 `FeishuAuth` 实现会话内卡片授权，但 Pi 无法调用——Control API 只有运维方法，没有 service 调用通路。

打通 Pi → Bridge Control API → Wrapper 这条路径后，Pi 发起的飞书 API 调用可获得**统一的会话内卡片授权体验**。

## WHAT

### Phase 1（本 change）：只读操作 MVP

**Bridge 侧**
1. Worker 注入完整上下文环境变量：`FEISHU_USER_OPEN_ID`、`FEISHU_BOT_NAME`、`FEISHU_CONTROL_SOCKET`、`FEISHU_CONTROL_TOKEN`
2. 各 wrapper 新增 `dispatch(action, chat_id, sender_id, **kwargs)` — 统一归一化返回 `{ok, data/error}`
3. Control API 新增 `call_service` — 在 `_build_dispatcher()` 中加闭包，路由到 wrapper dispatch
4. `call_service` 加权限校验：`chat_id` 必须在 `bot.session_map` 活跃会话中

**Pi 侧**
5. 新建 `feishu-bridge` skill：`SKILL.md` + `scripts/call.py`

**只暴露读操作**（MVP 范围）

| 域 | 暴露的 action |
|----|-------------|
| Tasks | `list_tasks`, `get_task`, `list_subtasks`, `list_tasklists`, `summary` |
| Sheets | `info`, `read` |
| Docs | `fetch` |
| Bitable | `list_records`, `get_record`, `list_fields`, `list_views`, `list_tables`, `get_view` |

### Phase 2（后续 change）：写操作

write / update / create / delete / append / complete 等变更类操作，在 Phase 1 验证通过后另开 change。

## NOT

- **不做** Mail / Markdown / Calendar 等新域 wrapper（后续按需）
- **Phase 1 不做**写/删/改操作（留 Phase 2）
- **不动** Pi 现有消息发送路径（`feishu-cli` 已够用）
- **不改** bridge 的 slash 命令系统

## RISKS

| 风险 | 缓解 |
|------|------|
| `dispatch()` 返回格式不统一 | 明确归一化规则（见 design.md），每个 wrapper 的 dispatch 统一包装 |
| 并发调用同一用户的 token refresh 竞争 | wrapper 已有 per-user threading.Lock |
| Pi skill 调用 Control API 失败（socket 不可达） | `call.py` 内置超时 + 重试；skill 文档注明 fallback 到 `lark` CLI |
| `call_service` 被非本会话进程滥用 | 校验 `chat_id` 在 `session_map` 活跃会话中；仅本地 socket 可达 |
| env var 路径依赖 `FEISHU_BRIDGE_BG_HOME` 覆盖 | worker 直接注入绝对路径到 env，不依赖 Pi 推断 |
| wrapper dispatch 执行中抛异常导致 RPC 错误 | dispatch 内部 catch 所有异常，归一化为 `{ok:false, error:...}` |

## Decision Log

| Date | Decision | Reason |
|------|----------|--------|
| 2026-06-20 | Pi 通过 Unix socket 直接调 Control API，不走 HTTP | 零新依赖，复用现有 socket，安全性更高 |
| 2026-06-20 | 各 wrapper 加 `dispatch()` 统一入口 | 避免 Control API 了解 wrapper 内部方法签名 |
| 2026-06-20 | sender_id 通过 env var 注入而非读 auth file | auth file 可能不存在（依赖 feishu_docs 配置），env var 始终可用 |
| 2026-06-20 | Control API 实现用闭包而非类方法 | 对齐 `_build_dispatcher()` 现有架构 |
| 2026-06-20 | `call_service` 校验 chat_id 在活跃会话中 | 防止其他进程借用 control token 跨会话调用，实现简单（~10 行） |
| 2026-06-20 | Phase 1 只暴露只读操作 | 降低首次交付风险，验证 dispatch 归一化 + auth 卡片流程后再开写 |
| 2026-06-20 | Worker 直接注入 socket/token 绝对路径到 env | 避免 Pi 推断 `bg_home()` 路径（可能被 `FEISHU_BRIDGE_BG_HOME` 覆盖） |
| 2026-06-20 | Mail wrapper 一次性做读+写（11 action），跳 Phase 2| Captain 决策：重复反馈循环成本高，且 API 格式已通过 lark CLI dry-run 全量验证 |
