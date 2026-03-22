# Feishu Bridge

飞书 ↔ AI Agent 桥接器 — 通过飞书机器人与 Claude Code / Codex 等 AI Agent 对话，拥有完整工具访问能力，自然语言操作文档、表格、日历、任务等飞书资源。

## 使用示例

```
"我飞书任务里的待办都有哪些"           → 查询任务列表
"帮我在知识库里新建一篇文档"           → 创建文档
"把上周会议纪要里的待办项创建为飞书任务" → 读取文档 + 提取待办 + 批量创建
"对比这两个表格的数据差异，结果写入新文档" → 读取表格 + 分析 + 创建文档
```

## 功能特性

- **WebSocket 实时通信** — 长连接接收，无需 webhook 服务器
- **CardKit v2 流式输出** — 打字指示器 + 渐进式消息更新
- **会话持久化** — 跨消息维持上下文，重启自动恢复
- **按会话任务队列** — 单聊串行，多聊天并行（4 workers）
- **40+ CLI 子命令** — 文档、表格、多维表格、Wiki、日历、任务、评论、云盘、搜索、邮件
- **OAuth 设备流** — 用户级授权，AES-256-GCM 加密存储 token，自动续期
- **URL 自动识别** — 飞书文档/Wiki/表格链接自动抓取内容作为上下文
- **多消息类型** — 文本、图片、富文本、任务分享、卡片、合并转发
- **多 Bot 实例** — 独立 workspace 和会话，支持不同 Agent 类型

## Bridge 命令

在飞书聊天中直接使用：

| 命令 | 说明 |
|------|------|
| `/new` `/reset` `/clear` | 重置会话（清除上下文） |
| `/stop` `/cancel` | 取消当前任务 |
| `/stop all` | 取消当前任务并清空待处理队列 |
| `/compact [指令]` | 压缩上下文（仅 Claude） |
| `/model [模型名]` | 查看或切换模型（别名因 Agent 类型而异） |
| `/status` | 查看会话状态（context / 费用 / 配额） |
| `/feishu-tasks [命令]` | 飞书任务管理（list/get/subtasks/add-subtask） |
| `/feishu-doc` | 云文档读写（Markdown） |
| `/feishu-sheet` | 电子表格读写 |
| `/feishu-bitable` | 多维表格操作 |
| `/restart` | 重启 Bridge 进程（launchd/systemd 自动拉起） |
| `/restart-all` | 重启所有 bot 实例 |
| `/help` | 显示帮助 |

## 快速开始

### 1. 创建飞书机器人

访问 [OpenClaw 多智能体创建页](https://open.feishu.cn/page/openclaw?form=multiAgent)，按引导创建 bot 并获取 App ID / App Secret。

### 2. 安装

```bash
pipx install feishu-bridge        # 推荐（隔离环境）

# 或从源码
git clone https://github.com/feir/feishu-bridge.git
cd feishu-bridge && pip install -e '.[dev]'
```

### 3. 配置并运行

首次运行自动启动配置向导：

```bash
$ feishu-bridge --bot my-bot
# 交互式输入 App ID、Secret、Agent 类型、工作目录
# → 凭证写入 ~/.config/feishu-bridge/.env
# → 配置写入 ~/.config/feishu-bridge/config.json
```

手动配置 `~/.config/feishu-bridge/config.json`：

```jsonc
{
  "bots": [{
    "name": "my-bot",
    "app_id": "${FEISHU_APP_ID}",       // 从 .env 加载
    "app_secret": "${FEISHU_APP_SECRET}",
    "workspace": "/path/to/workspace",
    "allowed_users": ["*"],             // ["*"] = 所有人
    "allowed_chats": ["oc_xxx"],        // 可选：限定群聊
    "model": "claude-opus-4-6",         // 可选：覆盖默认模型
    "group_policy": { ... }             // 可选：群聊响应策略
  }],
  "agent": {
    "type": "claude",                   // claude | codex
    "command": "claude",
    "timeout_seconds": 300
  }
}
```

配置查找顺序：`--config <path>` → `$FEISHU_BRIDGE_CONFIG` → `~/.config/feishu-bridge/config.json`

#### Agent 类型对比

| 特性 | Claude | Codex |
|------|--------|-------|
| 流式输出 | 增量实时更新 | 完成后一次性显示 |
| `/compact` `/status` | 支持 | 不支持 |
| 会话持久化 | session_id | thread_id |
| 默认模型 | `claude-opus-4-6` | `gpt-5.2-codex` |

### 群聊响应策略

通过 bot 的 `group_policy` 字段配置群聊响应行为，不配置时响应所有消息（兼容模式）。私聊不受影响。

| 模式 | 行为 | 适用场景 |
|------|------|----------|
| `owner-only` | 仅 owner @bot 时响应 | 个人专属 bot |
| `mention-all` | 任何人 @bot 时响应 | 团队共享 bot |
| `auto-reply` | 响应所有消息（无需 @） | 专用工作群 |
| `disabled` | 不响应 | 配合 `groups` 做白名单 |

```jsonc
"group_policy": {
  "default_mode": "mention-all",        // 全局默认
  "owner": "ou_xxx",                    // owner-only 时必填
  "groups": {                           // 可选：按群覆盖
    "oc_group_1": { "mode": "auto-reply" },
    "oc_group_2": { "mode": "disabled" }
  }
}
```

> **注意**：`mention-all`/`auto-reply` 需配合 `allowed_users: "*"`，否则非白名单用户被前置过滤。设为 `"*"` 时私聊也对所有人开放。群内破坏性命令（`/restart`、`/stop`）仅 owner 可执行。

### CLI 工具

`feishu-cli` 提供 40+ 子命令，直接访问飞书 API（同时作为 Agent 工具注入）：

```bash
feishu-cli search-docs --query "季度报告"
feishu-cli read-doc --token doxcnXXX
feishu-cli list-tasks --completed false
feishu-cli upload-file --file ./report.pdf
```

## 部署

```bash
# systemd（Linux）
cp contrib/feishu-bridge@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now feishu-bridge@my-bot

# launchd（macOS）
bash contrib/feishu-bridge-launcher.sh
```

异常退出后自动重启，editable install 下代码修改即时生效。

## 安全

- **Token 加密** — AES-256-GCM，密钥绑定 app_id + user_id + machine_id
- **沙盒限制** — 内置权限规则拦截高危命令（`systemctl *feishu-bridge*`、`shutdown`、`curl` 等）
- **删除二次确认** — `feishu-cli` 所有删除命令需 `--confirm <prefix>`

## 开发

```bash
pip install -e '.[dev]'
pytest tests/unit/ -v
```

## License

MIT
