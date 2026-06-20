# Summary: pi-bridge-service-integration

## 概要
打通 Pi → Bridge Control API 调用路径，让 Pi 复用 bridge 的 FeishuAuth 会话内授权卡片能力。新增 Calendar、Mail 两个 wrapper，并在全部 6 个域实现全量读写 dispatch。

## 变更内容

### Bridge 基础设施
- Control API 新增 `call_service` RPC 方法（`_build_dispatcher` 闭包），含 `session_map` 活跃会话授权校验
- Worker `env_extra` 注入 4 个新变量：`FEISHU_USER_OPEN_ID`、`FEISHU_BOT_NAME`、`FEISHU_CONTROL_SOCKET`、`FEISHU_CONTROL_TOKEN`
- 所有 wrapper 实现 `dispatch(action, chat_id, sender_id, **kwargs)` 统一入口，归一化返回 `{ok, data/error}`

### 6 域 Wrapper（全量读写）
| 域 | 文件 | 操作数 | 新增 |
|----|------|--------|------|
| Tasks | `api/tasks.py` | 14 | dispatch() 路由（写操作 methods 已存在） |
| Sheets | `api/sheets.py` | 6 | dispatch() 路由 + Drive scope |
| Docs | `api/docs.py` | 4 | dispatch() 路由 + Drive scope + doc_id 别名 |
| Bitable | `api/bitable.py` | 20 | dispatch() 路由 + patch_table |
| Calendar | `api/calendar.py` | 9 | **全新 wrapper**（6 read + 3 write） |
| Mail | `api/mail.py` | 11 | **全新 wrapper**（6 read + 5 write + EML 构建） |

### Pi 侧
- `~/.pi/agent/skills/feishu-bridge/SKILL.md` — 6 域全量操作表
- `~/.pi/agent/skills/feishu-bridge/scripts/call.py` — Unix socket 通信脚本
- `~/.pi/agent/skills/feishu-bridge/workflow.yaml`

## 关键决策
- Mail wrapper API 格式通过 lark CLI dry-run 全量验证，EML 用 Python stdlib email 模块构建
- Mail 读/写 scope 分离：读操作仅请求 `_READ_SCOPES`，写操作走 `_get_write_token`
- Calendar 时间戳在 dispatch 层自动包装（string → `{timestamp: string}`）
- Sheets/Docs dispatch 增加 error dict 检测（MCP 返回的错误不会被误包为 `ok:true`）

## 影响范围
```
feishu_bridge/api/calendar.py   | 446 +++++  (新文件)
feishu_bridge/api/mail.py       | 416 +++++  (新文件)
feishu_bridge/api/bitable.py    | 133 +     (dispatch 扩展)
feishu_bridge/api/tasks.py      |  99 +     (dispatch 扩展)
feishu_bridge/api/sheets.py     |  65 +     (dispatch 扩展)
feishu_bridge/api/docs.py       |  61 +     (dispatch 扩展)
feishu_bridge/control_api.py    |  44 +-    (call_service)
feishu_bridge/worker.py         |   7 +-    (env vars)
feishu_bridge/main.py           |   6 +     (wrapper 注册)
```
