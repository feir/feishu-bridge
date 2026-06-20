# Design: pi-bridge-service-integration

## 技术方案

### 数据流

```
┌──────────────────────────────────────────────────────────────────┐
│ Feishu 会话                                                       │
│   用户: "帮我查任务"                                                │
│    ↓                                                              │
│ Bridge Worker                                                     │
│   ├─ 注入 env: FEISHU_CHAT_ID, FEISHU_USER_OPEN_ID, FEISHU_BOT_NAME │
│   │           FEISHU_CONTROL_SOCKET, FEISHU_CONTROL_TOKEN (绝对路径) │
│   └─ 启动 Pi 子进程                                                │
│        │                                                          │
│        ▼                                                          │
│   Pi (feishu-bridge skill)                                        │
│     │  python3 scripts/call.py --service tasks --action list_tasks│
│     │  ① 从 env 读 chat_id, sender_id, socket/token 路径          │
│     │  ② echo '{"method":"call_service",...}' | socat → socket    │
│     ▼                                                             │
│   Control API (_build_dispatcher 闭包 `_call_service`)            │
│     │  ① 权限校验：chat_id ∈ session_map 活跃会话                  │
│     │  ② 路由: params.service → self.bot.feishu_<service>         │
│     ▼                                                             │
│   Wrapper.dispatch(action, chat_id, sender_id, **kwargs)           │
│     │  ① get_token(chat_id, sender_id)                            │
│     │     └─ 无缓存 token → 发授权卡片到 chat_id                   │
│     │  ② 调用飞书 API                                              │
│     │  ③ 归一化返回 {ok, data/error}                               │
│     ▼                                                             │
│   返回 JSON → Control API → socket → Pi → 呈现给用户               │
└──────────────────────────────────────────────────────────────────┘
```

### Control API `call_service` 协议

请求：
```json
{
  "method": "call_service",
  "token": "<control-token>",
  "id": "req-1",
  "params": {
    "service": "tasks",
    "action": "list_tasks",
    "chat_id": "oc_xxx",
    "sender_id": "ou_xxx",
    "args": {}
  }
}
```

响应（成功）：
```json
{
  "result": {"ok": true, "data": {"items": [...], "count": 5}},
  "id": "req-1",
  "api_version": 1,
  "capabilities": ["...", "call_service"]
}
```

响应（wrapper 层错误）：
```json
{
  "result": {"ok": false, "error": "auth_failed", "message": "已发送授权卡片，请完成授权后重试"},
  "id": "req-1"
}
```

响应（权限拒绝）：
```json
{
  "result": {"ok": false, "error": "unauthorized", "message": "会话未激活或已过期"},
  "id": "req-1"
}
```

### 权限模型

`_call_service` 闭包内校验：

```python
def _call_service(params):
    chat_id = params.get("chat_id", "")
    # 校验 chat_id 在活跃会话中
    active_chats = set()
    with bot.session_map._lock:
        for key in bot.session_map._data:
            if key == bot.session_map._AGENT_TYPE_KEY:
                continue
            parts = key.split(":", 2)
            if len(parts) >= 2:
                active_chats.add(parts[1])
    if chat_id not in active_chats:
        return {"ok": False, "error": "unauthorized",
                "message": "会话未激活或已过期"}
    # ... 路由到 wrapper
```

仅允许对当前 bridge 正在服务的会话发起调用。本地 socket 文件权限 0600，token 600，已经限制了物理访问面。

### Wrapper `dispatch()` 签名 + 归一化规则

```python
def dispatch(self, action: str, chat_id: str, sender_id: str, **kwargs) -> dict:
    """统一服务入口。返回格式:
    - 成功: {"ok": True, "data": <原始返回值>}
    - auth 失败: {"ok": False, "error": "auth_failed"}
    - 参数错误: {"ok": False, "error": "invalid_args", "message": "..."}
    - 未支持 action: {"ok": False, "error": "unsupported_action", "message": "..."}
    - API/网络异常: {"ok": False, "error": "api_error", "message": "..."}
    
    内部 catch 所有异常，绝不抛到 Control API 层。
    """
```

归一化规则：
| 原始返回值 | 归一化 |
|-----------|--------|
| `dict` 直接返回 | `{"ok": True, "data": result}` |
| `None`（token 获取失败） | `{"ok": False, "error": "auth_failed"}` |
| `{"error": "auth_failed"}` | `{"ok": False, "error": "auth_failed"}` |
| `FeishuAPIError` 异常 | `{"ok": False, "error": "api_error", "message": str(e)}` |
| 未知 action | `{"ok": False, "error": "unsupported_action"}` |
| 其他异常 | `{"ok": False, "error": "internal_error", "message": str(e)}` |

### Worker env 注入

```python
env_extra = {
    "FEISHU_CHAT_ID": chat_id,
    "FEISHU_BOT_ID": bot_id,
    "FEISHU_THREAD_ID": thread_id or "",
    "FEISHU_USER_OPEN_ID": sender_id,           # 新增
    "FEISHU_BOT_NAME": bot_config.get("name", ""),  # 从 auth-file 分支提升为无条件
    "FEISHU_CONTROL_SOCKET": str(ctrl_socket_path), # 新增，绝对路径
    "FEISHU_CONTROL_TOKEN": str(ctrl_token_path),   # 新增，绝对路径
}
```

## 关键决策

### 为什么走 Unix socket 而不是 HTTP？

| 维度 | Unix socket | HTTP localhost |
|------|------------|----------------|
| 依赖 | 零（socat/Python stdlib） | 需要 HTTP server + 端口管理 |
| 安全性 | 文件权限 0600，天然隔离 | 需额外 bind 127.0.0.1 + token |
| 复用 | 现有 Control API 基础设施 | 需要新建 HTTP handler |
| 部署 | 已有 socket，无需改配置 | 需新增端口配置 |

### 为什么每个 wrapper 独立 `dispatch()` 而不是统一路由表？

1. 减少 Control API 的耦合——它不需要知道每个 wrapper 的方法签名
2. 每个 wrapper 的 dispatch 可以做自己的参数校验和错误格式化
3. 新增 wrapper 时只需加 dispatch + 注册到 Control API 路由表，改动范围明确

### 为什么 sender_id 走 env 不走 auth file？

auth file 只在 `feishu_docs and runner.wants_auth_file()` 条件下创建，不是始终存在。env var 不依赖任何外部条件，更可靠。

### 为什么 Control API 实现用闭包而不是类方法？

`control_api.py` 已有架构：`_build_dispatcher(bot, log_buffer)` 返回 `{"method_name": closure, ...}` 字典。新增 `call_service` 必须遵循此模式，在函数内定义 `_call_service(params)` 闭包。

### 为什么 Phase 1 只做只读？

1. 验证 dispatch 归一化 + auth 卡片流程的稳定性
2. 写操作涉及数据变更，需要更严谨的测试覆盖
3. 读操作已覆盖 90% 的 Pi 日常飞书查询需求

## 影响范围

### Bridge 侧文件

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `feishu_bridge/control_api.py` | 修改 | `_build_dispatcher()` 内新增 `_call_service` 闭包 + 权限校验；`_CAPABILITIES` 注册 |
| `feishu_bridge/worker.py` | 修改 | `env_extra` 新增 4 个字段 |
| `feishu_bridge/api/tasks.py` | 修改 | 新增 `dispatch()`（仅暴露只读 action） |
| `feishu_bridge/api/sheets.py` | 修改 | 新增 `dispatch()`（仅暴露 info/read） |
| `feishu_bridge/api/docs.py` | 修改 | 新增 `dispatch()`（仅暴露 fetch） |
| `feishu_bridge/api/bitable.py` | 修改 | 新增 `dispatch()`（仅暴露只读 action） |

### Pi 侧文件（新建）

| 文件 | 说明 |
|------|------|
| `~/.pi/agent/skills/feishu-bridge/SKILL.md` | Skill 定义，含 frontmatter 和操作说明 |
| `~/.pi/agent/skills/feishu-bridge/scripts/call.py` | Socket 通信脚本 |

## 回滚方案

1. 移除 Pi skill：`rm -rf ~/.pi/agent/skills/feishu-bridge/`
2. 移除 `call_service`：从 `_CAPABILITIES` 和 dispatch dict 中删除
3. 移除 wrapper `dispatch()`：删除 4 个 wrapper 中的 dispatch 方法（或保留为 inert——不加注册就不会被调用）
4. 移除 env 注入：回退 worker.py `env_extra` 的 4 个新增字段
5. 验证：`echo '{"method":"status","token":"..."}' | socat - UNIX-CONNECT:<sock>` 原有方法正常
