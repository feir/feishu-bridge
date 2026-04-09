## Architecture

- `feishu_bridge/main.py`
  - 进程入口、配置加载、飞书 WebSocket 接入、bot 生命周期管理。
- `feishu_bridge/runtime.py`
  - runner 抽象、session 映射、任务队列、CLI 调用运行时、公用限制常量。
- `feishu_bridge/worker.py`
  - 单条消息处理管线、媒体下载、quota/context 告警、idle compact 调度。
- `feishu_bridge/ui.py`
  - 飞书 CardKit 消息渲染、Markdown 适配、交互按钮与 URL 按钮。
- `feishu_bridge/commands.py`
  - bridge 内建命令和 `/feishu-*` 服务命令分发。
- `feishu_bridge/api/*.py`
  - 飞书各服务封装：docs、sheets、bitable、wiki、calendar、tasks、search 等。

## Data Flow

1. 飞书事件进入 `main.py`
2. 解析消息、引用内容、附件和链接
3. 投递到 `worker.py` 的处理管线
4. 通过 `runtime.py` runner 调用 Claude Code / Codex
5. `ui.py` 将流式输出和状态渲染成飞书卡片
6. 必要时由 `commands.py` 或 `api/*` 反向操作飞书资源

## Design Constraints

- 回调层和 worker 层职责分离：回调层不做网络 I/O。
- 会话状态依赖 `SessionMap`，改后端或切 session 时要考虑旧 session 清理。
- `/compact`、quota 告警、流式 usage 统计是联动行为，修改其一要检查其余两处。
