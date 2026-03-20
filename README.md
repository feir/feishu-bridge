# Feishu Bridge

飞书 ↔ AI Agent 桥接器。通过飞书机器人与 AI Agent（默认 Claude Code，也支持 Codex、OpenCode 等）对话，拥有完整的工具访问能力。在各平台飞书客户端上实时远程会话，通过自然语言对话或斜杠命令，让 AI 直接操作飞书中的文档、表格、日历、任务等资源。


## 使用示例

**简单示例**：
- "我飞书任务里的待办都有哪些" → 查询任务列表
- "帮我在知识库里新建一篇文档" → 创建文档
- "查一下上周的日历安排" → 查询日历事件
- "把这个文件传到飞书云盘 https://example.com/report.pdf" → 下载并上传

**复杂组合示例**：
- "查看下昨天我们讨论的 bridge 增强计划的执行进度情况" → 搜索文档 + 读取内容 + 查询任务，综合分析后汇报
- "把上周会议纪要里的待办项创建为飞书任务" → 读取文档 + 提取待办 + 批量创建任务
- "对比这两个表格的数据差异，结果写入新文档" → 读取两个表格 + 分析 + 创建文档写入结果
- "创建一个任务清单叫 Q2 目标，然后把这几个任务加进去" → 创建清单 + 批量添加任务到清单

## 功能特性

### 核心架构

- **WebSocket 实时通信** — 长连接接收消息，无需部署 webhook 服务器
- **流式输出** — 实时打字指示器 + 渐进式消息更新（CardKit v2）
- **会话持久化** — 跨消息维持对话上下文，重启后自动恢复
- **按会话任务队列** — 单聊内串行处理，多聊天间并行（4 workers）

### 飞书 API 集成

- **40+ CLI 子命令** — 文档、表格、多维表格、Wiki、日历、任务、评论、云盘、搜索
- **交互式命令** — `/feishu-tasks`、`/feishu-doc`、`/feishu-sheet`、`/feishu-bitable` 直接在聊天中操作飞书数据
- **OAuth 设备流** — 用户级 API 授权，AES-256-GCM 加密存储 token，自动续期
- **URL 自动识别** — 消息中的飞书文档/Wiki/表格链接自动抓取内容作为上下文

### 消息处理

- **多类型支持** — 纯文本、图片、富文本（post）、任务分享（todo）、卡片消息、合并转发
- **飞书任务自动展开** — 收到任务分享时自动拉取任务详情（`todo_auto_drive` 配置），自动判断任务状态进行推进执行。

### 多 Bot 支持

- 配置文件中定义多个 bot 实例，独立 workspace 和会话

## Bridge 命令

在飞书聊天中直接使用：

| 命令 | 说明 |
|------|------|
| `/new` `/reset` `/clear` | 重置会话（清除上下文） |
| `/stop` `/cancel` | 取消当前任务 |
| `/stop all` | 取消当前任务并清空待处理队列 |
| `/compact [指令]` | 压缩上下文 |
| `/model` | 查看当前模型 |
| `/model opus\|sonnet\|haiku` | 切换 Claude 模型 |
| `/cost` | 查看 token 用量和费用 |
| `/context` | 查看上下文使用率（含可视化进度条） |
| `/restart` | 重启 Bridge 进程（launchd/systemd 自动拉起） |
| `/restart-all` | 重启所有 bot 实例 |
| `/help` | 显示帮助 |

## 快速开始

### 1. 创建飞书机器人

访问 [OpenClaw 多智能体创建页](https://open.feishu.cn/page/openclaw?form=multiAgent)，按引导操作即可自动创建 bot 并完成权限配置，直接获取 App ID 和 App Secret。

### 2. 安装

```bash
# 推荐使用 pipx（隔离环境）
pipx install feishu-bridge

# 或从源码安装
git clone https://github.com/feir/feishu-bridge.git
cd feishu-bridge
pip install -e '.[dev]'
```

### 3. 配置并运行

首次运行时，如果没有配置文件，会自动启动交互式向导：

```bash
$ feishu-bridge --bot my-bot

✦ Feishu Bridge 首次配置向导

  请先在飞书开放平台创建机器人：
  https://open.feishu.cn/page/openclaw?form=multiAgent

  App ID: cli_xxxx
  App Secret: xxxx
  工作目录 [~/.local/share/feishu-bridge/workspaces/my-bot]:

  凭证已写入 ~/.config/feishu-bridge/.env
  配置已写入 ~/.config/feishu-bridge/config.json
```

向导会自动生成配置文件和凭证文件，然后直接启动。

也可以手动创建 `~/.config/feishu-bridge/config.json`：

```json
{
  "bots": [
    {
      "name": "my-bot",
      "app_id": "${FEISHU_APP_ID}",
      "app_secret": "${FEISHU_APP_SECRET}",
      "workspace": "/path/to/workspace",
      "allowed_users": ["*"]
    }
  ],
  "claude": {
    "command": "claude",
    "timeout_seconds": 300
  }
}
```

- `${VAR}` 语法会在加载时替换为环境变量（凭证存放在 `~/.config/feishu-bridge/.env`）
- `allowed_users`：允许使用的用户 ID 列表，`["*"]` 表示所有人
- `allowed_chats`（可选）：允许的群聊 ID 列表
- `model`（可选）：默认 Claude 模型，默认 `claude-opus-4-6`
- `group_policy`（可选）：群聊响应策略，详见下方说明

### 群聊响应策略

通过 `group_policy` 配置 bot 在群聊中的响应行为。不配置时为兼容模式（响应所有消息）。

```json
{
  "name": "my-bot",
  "allowed_users": "*",
  "group_policy": {
    "default_mode": "mention-all",
    "owner": "ou_xxx",
    "groups": {
      "oc_group_id_1": { "mode": "auto-reply" },
      "oc_group_id_2": { "mode": "disabled" }
    }
  }
}
```

#### 四种模式

| 模式 | 行为 | 适用场景 |
|------|------|----------|
| `owner-only` | 仅 owner @bot 时响应 | 个人专属 bot，拉入群仅供自己使用 |
| `mention-all` | 任何人 @bot 时响应 | 团队共享 bot |
| `auto-reply` | 响应群内所有消息（无需 @） | 小群或专用工作群 |
| `disabled` | 不响应群消息 | 配合 per-group 覆盖实现"仅指定群" |

#### 配置字段

| 字段 | 必填 | 说明 |
|------|------|------|
| `default_mode` | 是 | 默认模式，所有群生效 |
| `owner` | `owner-only` 时必填 | owner 的 `open_id`（如 `ou_xxx`） |
| `groups` | 否 | 按 `chat_id` 覆盖特定群的模式 |

#### 配置示例

**个人 bot（默认）**：所有群仅 owner @bot 响应
```json
"group_policy": {
  "default_mode": "owner-only",
  "owner": "ou_your_open_id"
}
```

**团队共享 bot**：所有人 @bot 可触发
```json
"allowed_users": "*",
"group_policy": {
  "default_mode": "mention-all",
  "owner": "ou_admin_open_id"
}
```

**仅指定群响应**：默认不响应，特定群开放
```json
"group_policy": {
  "default_mode": "disabled",
  "owner": "ou_admin_open_id",
  "groups": {
    "oc_allowed_group": { "mode": "mention-all" }
  }
}
```

#### 注意事项

- **私聊不受影响** — `group_policy` 仅控制群聊行为，私聊始终正常响应
- **`allowed_users` 前置过滤** — `mention-all` 和 `auto-reply` 模式需要 `allowed_users: "*"`，否则非白名单用户的消息在到达门控前就被拒绝
- **DM 副作用** — 将 `allowed_users` 设为 `"*"` 时，私聊也会对所有飞书用户开放
- **破坏性命令保护** — 群内 `/restart`、`/restart-all`、`/stop` 仅 owner 可执行
- **bridge 命令豁免** — `/help`、`/model` 等非破坏性命令不受门控限制
- **回滚方式** — 删除 `group_policy` 配置块并重启即可恢复原始行为

### CLI 工具

`feishu-cli` 提供对飞书 API 的直接访问（也作为 Claude 的工具注入）：

```bash
feishu-cli search-docs --query "季度报告"
feishu-cli read-doc --token doxcnXXX
feishu-cli list-tasks --completed false
feishu-cli upload-file --file ./report.pdf
```

## 部署

### systemd（Linux）

```bash
cp contrib/feishu-bridge@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now feishu-bridge@my-bot
```

### launchd（macOS）

使用 `contrib/feishu-bridge-launcher.sh` 配合 plist 配置。

进程异常退出后 launchd/systemd 会自动重启，editable install 模式下代码修改即时生效。

## 配置文件查找顺序

1. `--config <path>` 命令行参数
2. `$FEISHU_BRIDGE_CONFIG` 环境变量
3. `~/.config/feishu-bridge/config.json`

## 安全

- **Token 加密存储** — AES-256-GCM，密钥绑定 app_id + user_id + machine_id
- **沙盒限制** — Bridge 自带权限规则拦截高危命令（如 `systemctl *feishu-bridge*`、`shutdown`、`curl`、`kill` 等），同时遵守用户本地的 Claude Code 权限配置
- **删除操作安全确认** — 所有 `feishu-cli` 删除命令需 `--confirm <prefix>` 二次确认

## 开发

```bash
pip install -e '.[dev]'
pytest tests/unit/ -v
```

## TODO

- [x] 群聊响应策略（owner-only / mention-all / auto-reply / disabled + per-group 覆盖）
- [ ] 独立 DM 策略（解耦 `allowed_users` 对私聊和群聊的双重控制）

## License

MIT
