# Feishu Bridge

在飞书中远程操控 Claude Code CLI — 同时让 AI Agent 拥有飞书全平台操作能力。

Feishu Bridge 是一个双向桥接器：你通过飞书聊天远程使用 Claude Code / Codex 的完整能力（编码、调试、文件操作），而 AI Agent 则通过 80+ 飞书 API 命令反过来管理你的文档、表格、日历、任务等飞书资源。

## 它能做什么

**通过飞书操控 AI Agent**
```
"帮我 review 一下这个 PR"                 → Claude Code 读代码、跑测试、给反馈
"/status"                                → 查看 context 用量、token 费用、配额
"/model sonnet"                          → 切换到 Sonnet 模型
```

**让 AI Agent 管理飞书资源**
```
"把上周会议纪要里的待办项创建为飞书任务"     → 读取文档 + 提取待办 + 批量创建
"对比这两个表格的数据差异，结果写入新文档"   → 读取表格 + 分析 + 创建文档
"查一下明天下午有没有空，帮我约个会"        → 查询日历空闲 + 创建事件 + 添加参会人
```

消息中的飞书链接会被自动识别并抓取内容作为上下文，直接发链接即可开始工作。

## 核心特性

### 实时流式输出
CardKit v2 流式渲染 — 打字指示器 + 100ms 级增量更新，体验接近原生 Claude Code 终端。自动适配飞书 Markdown 渲染限制。

### 会话与上下文
跨消息维持完整上下文，bridge 重启自动恢复会话。每个聊天独立任务队列（单聊串行、多聊天 4 workers 并行），`/btw` 可在不中断当前任务的情况下侧问。

### 80+ CLI 命令
覆盖 13 个飞书服务：文档、表格、多维表格、Wiki、日历、任务、评论、邮件、云盘、搜索、消息等。Agent 自动按需调用，也可作为独立 CLI 工具使用。

### 多 Agent 支持

| 特性 | Claude Code | Codex |
|------|-------------|-------|
| 流式输出 | 增量实时更新 | 流式输出 |
| `/compact` `/status` `/btw` | 支持 | 不支持 |
| 会话持久化 | session_id | thread_id |
| 默认模型 | `claude-opus-4-6` | `gpt-5.2-codex` |

### 安全
- **Token 加密** — AES-256-GCM，密钥绑定 app_id + user_id + machine_id
- **沙盒限制** — 内置权限规则拦截高危命令（`systemctl *feishu-bridge*`、`shutdown`、`curl` 等）
- **删除二次确认** — 所有删除命令需 `--confirm <prefix>` 校验

## Bridge 命令

在飞书聊天中直接使用：

| 命令 | 说明 |
|------|------|
| `/new` `/reset` `/clear` | 重置会话（清除上下文） |
| `/stop` `/cancel` | 取消当前任务 |
| `/stop all` | 取消当前任务并清空待处理队列 |
| `/btw <问题>` | 侧问（fork 会话，不中断当前任务） |
| `/compact [指令]` | 压缩上下文（仅 Claude） |
| `/model [模型名]` | 查看或切换模型 |
| `/status` | 查看会话状态（context / 费用 / 配额） |
| `/update` | 检查并拉取最新版本（不重启） |
| `/feishu-tasks [命令]` | 飞书任务管理（list/get/subtasks/add-subtask） |
| `/feishu-doc` | 云文档读写（Markdown） |
| `/feishu-sheet` | 电子表格读写 |
| `/feishu-bitable` | 多维表格操作 |
| `/restart` | 重启 Bridge 进程 |
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

### 升级

Bridge 会在后台自动检测新版本并拉取更新（每 6 小时），更新就绪后在回复卡片底部显示提醒。发送 `/restart` 即可部署。也可手动操作：

```bash
# PyPI 安装
pipx upgrade feishu-bridge

# 源码安装（纯代码变更 pull 即生效，依赖变更需重装）
cd feishu-bridge && git pull
pip install -e '.[dev]'           # 仅 pyproject.toml 依赖变更时需要
```

使用 `/update` 可手动触发版本检查。

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

`feishu-cli` 提供 80+ 子命令，覆盖 13 个飞书服务，可独立使用或作为 Agent 工具自动调用：

```bash
feishu-cli search-docs --query "季度报告"
feishu-cli read-doc --token doxcnXXX
feishu-cli list-tasks --completed false
feishu-cli create-event --calendar-id xxx --summary "周会" \
  --start-time 2026-03-24T10:00:00+08:00 --end-time 2026-03-24T11:00:00+08:00
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

## 开发

```bash
pip install -e '.[dev]'
pytest tests/unit/ -v
```

## License

MIT
