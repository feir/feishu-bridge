# Feishu Bridge

在飞书中获得完整的 Claude Code CLI 体验 — 不只是聊天，而是流式输出、任务追踪、多 Agent 调度、上下文管理的全功能呈现。

普通 AI Bot 只转发文本。Feishu Bridge 不同：它将 Claude Code CLI 的每一项能力——实时流式输出、todo 进度追踪、Agent 分发状态、context 用量监控、配额告警——**原生渲染到飞书消息卡片中**，同时利用飞书的交互按钮、内联链接、Markdown 排版增强体验。AI Agent 还能通过 80+ 飞书 API 命令反向管理你的文档、表格、日历、任务等资源。

## 信息卡片

每条回复都是一张富信息卡片，而非纯文本：

```
┌──────────────────────────────────────────────────┐
│ 已完成代码审查，发现 2 个问题并修复。              │
│                                                  │
│ 1. `worker.py:128` — 重复 join 表达式...          │
│ 2. `ui.py:727` — 冗余注释...                      │
│                                                  │
│ ◉ Code review (Explore)        ← Agent 进度       │
│ ~~☑ Search patterns~~                             │
│ ☑ Fix lint errors (3/3)        ← Todo 追踪        │
│ ◻ Run tests (0/2)                                 │
│                                                  │
│ [📎 Open PR]  [✅ Confirm]  [❌ Cancel]  ← 交互按钮│
│                                                  │
│ ────────────────────────────────────── footer ─── │
│ ✅ 3/5 tasks · opus-4-6 · 2m15s · 45k tokens · main │
│ 🟡 Context 72% — 可考虑 /compact 压缩上下文        │
│ ⬆ v2026.3.25.1 已就绪，/restart 部署               │
└──────────────────────────────────────────────────┘
```

**卡片内包含的实时信息：**

| 区域 | 内容 | 来源 |
|------|------|------|
| 主体 | 流式 Markdown 输出（100ms 级增量更新） | Claude Code stdout |
| Agent 进度 | 多 Agent 并行分发的启动/完成状态 | `stream-json` tool_use 事件 |
| Todo 追踪 | 任务列表实时进度（☑/◻） | TodoWrite 回调 |
| 交互按钮 | 确认/取消/选择，点击即响应 | Action markers 解析 |
| 内联链接 | URL 自动提取为侧边栏按钮 | 正则匹配 + 按钮渲染 |
| Footer | 状态图标 · 任务数 · 模型 · 耗时 · token 数 · 分支 | CLI result 元数据 |
| Footer | Context 用量告警（70%/85% 阈值） | input_tokens / context_window |
| Footer | 版本更新提醒 | 后台 auto-update 检测 |

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

**链接即上下文** — 消息中的飞书链接自动识别并抓取内容，发链接即可开始工作。

## 核心特性

### 实时流式渲染
CardKit v2 流式卡片 — 打字指示器 → loading 动画 → 100ms 级增量更新 → 完成后切换为静态卡片。自动适配飞书 Markdown 渲染限制（标题降级、代码块优化），体验接近原生 CLI 终端。

### 任务与 Agent 追踪
Claude Code 的 TodoWrite 进度和多 Agent 分发状态实时渲染到卡片中。单 Agent、多 Agent 并行分发、Agent Teams 协作三种模式均支持，完成自动标记。

### 会话与上下文管理
跨消息维持完整上下文，bridge 重启自动恢复会话。Context 用量实时监控（70%/85% 阈值告警），支持 `/compact` 压缩。每个聊天独立任务队列（单聊串行、多聊天 4 workers 并行），`/btw` 可在不中断当前任务的情况下侧问。

### 交互增强
- **Action buttons** — Claude 需要确认/选择时，卡片内直接渲染交互按钮，点击即响应
- **内联 URL 按钮** — 回复中的链接自动提取为侧边栏快捷按钮
- **Rich footer** — 每张卡片底部显示状态、模型、耗时、token、分支、context 告警、版本更新

### 80+ 飞书 CLI 命令
覆盖 13 个飞书服务：文档、表格、多维表格、Wiki、日历、任务、评论、邮件、云盘、搜索、消息等。Agent 自动按需调用，也可作为独立 CLI 工具使用。

### 多 Agent 后端

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
| `/agent [claude\|codex]` | 查看或切换当前 bot 的后端 |
| `/provider [default\|ollama\|...]` | 查看或切换当前后端配置 |
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
    "type": "claude",                   // claude | codex | local | pi
    "command": "claude",
    "commands": {                       // 可选：为热切换指定命令
      "claude": "claude",
      "codex": "codex",
      "pi": "pi"
    },
    "args": ["--verbose"],             // 可选：当前 agent 的额外 CLI 参数
    "env": {                            // 可选：当前 agent 的固定环境变量
      "ANTHROPIC_BASE_URL": "http://127.0.0.1:11434"
    },
    "args_by_type": {                   // 可选：按 agent 类型区分参数
      "codex": ["--oss", "--local-provider", "ollama"]
    },
    "env_by_type": {                    // 可选：按 agent 类型区分环境变量
      "codex": {
        "OPENAI_BASE_URL": "http://127.0.0.1:11434/v1"
      }
    },
    "prompt": {                         // 可选：控制 bridge 固定注入的 prompt
      "safety": "full",                 // full | minimal | off
      "feishu_cli": true,
      "cron_mgr": true
    },
    "timeout_seconds": 300
  }
}
```

发送 `/agent claude`、`/agent codex`、`/agent local` 或 `/agent pi` 可在当前 bot 进程内热切换后端。切换会清空旧会话映射，避免复用不兼容的 `session_id` / `thread_id`；该切换仅影响当前运行实例，重启后仍以配置文件中的 `agent.type` 为准。

`agent.args` / `agent.env` 是当前 `agent.type` 的简写；如需给热切换目标预置不同参数，使用 `args_by_type` / `env_by_type`。

如需在 bridge 内热切换 provider，而不是手改配置文件，可定义 `agent.providers`：

```jsonc
"agent": {
  "type": "claude",
  "provider": "default",
  "command": "claude",
  "providers": {
    "default": {},
    "ollama": {
      "env_by_type": {
        "claude": {
          "ANTHROPIC_BASE_URL": "http://127.0.0.1:11434"
        },
        "codex": {
          "OPENAI_BASE_URL": "http://127.0.0.1:11434/v1"
        }
      },
      "args_by_type": {
        "codex": ["--oss", "--local-provider", "ollama"]
      },
      "models": {
        "claude": "qwen3.5",
        "codex": "gpt-oss:120b"
      },
      "prompt": {
        "safety": "minimal",
        "feishu_cli": false,
        "cron_mgr": false
      }
    }
  }
}
```

随后可在飞书内直接发送：

```text
/provider ollama
/provider default
```

`agent.prompt` 用于控制 bridge 自己追加的 system prompt 片段；provider 下的 `prompt` 会覆盖全局设置。对本地 Ollama，通常建议关闭 `feishu_cli` / `cron_mgr`，并把 `safety` 降到 `minimal`，这样可以显著减少每次启动时固定注入的 token。

切换 provider 会像 `/agent` 一样清空旧会话映射，避免复用不兼容的 session。

### 连接本地 Ollama

可以继续使用现有 `claude` / `codex` runner，只把底层 CLI 指到本地 Ollama。这样仍能保留 bridge 对 Claude Code / Codex 事件流的卡片渲染能力。

**Claude Code → Ollama**

```jsonc
"agent": {
  "type": "claude",
  "command": "claude",
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:11434"
  }
}
```

模型名仍由 bridge 控制：在配置里写 bot 的 `"model"`，或在飞书里发送 `/model qwen3.5`、`/model deepseek-r1` 这类本地已安装模型名。

**Codex → Ollama**

```jsonc
"agent": {
  "type": "codex",
  "command": "codex",
  "args": ["--oss", "--local-provider", "ollama"],
  "env": {
    "OPENAI_BASE_URL": "http://127.0.0.1:11434/v1"
  }
}
```

同样可以通过 bot 配置里的 `"model"` 或 `/model gpt-oss:120b` 指定具体模型。若你已在 `~/.codex/config.toml` 配好 profile，也可以改用：

```jsonc
"agent": {
  "type": "codex",
  "command": "codex",
  "args": ["--profile", "ollama-launch"]
}
```

### 连接 Pi / oMLX

Pi runner 适合把本机 `pi` 作为 bridge 后端，再由 Pi 连接本地 oMLX。推荐先以只读工具启用，确认 Feishu 端 smoke 后再显式加入写入或 shell 工具。

前置条件：

- `pi` 已在 PATH 中，或在 `agent.commands.pi` 中使用绝对路径。
- `~/.pi/agent/models.json` 已配置 oMLX provider，例如 `baseUrl: "http://127.0.0.1:8000/v1"`。
- oMLX 服务已监听本机端口。

示例配置：

```jsonc
"agent": {
  "type": "claude",
  "provider": "default",
  "commands": {
    "claude": "claude",
    "codex": "codex",
    "local": "local",
    "pi": "/Users/feir/.local/bin/pi"
  },
  "providers": {
    "default": {},
    "pi-local": {
      "workspace": "/Users/feir/.claude",
      "models": {
        "pi": "Qwen3.6-35B-A3B-mxfp4"
      },
      "args_by_type": {
        "pi": [
          "--provider", "omlx",
          "--no-context-files",
          "--no-extensions",
          "--no-skills",
          "--no-prompt-templates",
          "--no-themes"
        ]
      },
      "prompt": {
        "safety": "minimal",
        "feishu_cli": false,
        "cron_mgr": false
      }
    }
  }
}
```

飞书内切换顺序：

```text
/agent pi
/provider pi-local
```

默认情况下，`PiRunner` 会自动注入只读工具 `read,grep,find,ls`。如果配置里显式传入 `--tools` 或 `--no-tools`，bridge 会尊重该设置，不再追加默认工具。

Pi 当前不支持 bridge 触发的 `/compact`；当当前 runner 是 Pi 时，`/compact` 会返回“不支持”，context 告警也会提示使用 `/new` 开始新会话。Pi 的 token 使用量来自 Pi JSON 事件中的 usage 字段，bridge 会把它映射到 `/status` 和消息 footer。

为避免本地模型自动吃进过多 Claude Code 上下文，推荐 staging/生产先保留 `--no-context-files --no-extensions --no-skills --no-prompt-templates --no-themes`。如果要给 Pi 增加规则，优先使用短上下文：

- 全局：`~/.pi/agent/AGENTS.md`
- 项目：`<workspace>/.pi/APPEND_SYSTEM.md`

写入或 shell 工具不建议直接加入生产配置。若确实要测试，应先在一次性目录里显式覆盖 `args_by_type.pi` 的 `--tools`，完成安全审查和 disposable-file smoke test 后再扩大范围。

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

### 后台任务

`feishu-cli bg` 用于把耗时命令交给 bridge 后台执行，完成后由 bridge 注入一条 `bg_task_completion` 合成 turn 回到原会话。已启动的 wrapper 运行在独立 session；bridge 主进程崩溃或重启不会中断已经 launch 的任务，重启时 reconciler 会从 SQLite 和 manifest 补投完成通知。

```bash
feishu-cli bg enqueue \
  --chat-id oc_xxx \
  --session-id sess_xxx \
  --on-done-prompt "done" \
  -- sleep 10

feishu-cli bg status <task_id>
feishu-cli bg list --chat-id oc_xxx --state running
feishu-cli bg cancel <task_id>
```

命令必须以 argv 形式传入：优先使用 `--` 分隔真实命令，或使用 `--cmd-json '["python3","script.py"]'`。bridge 始终用 `shell=False` 启动用户命令，不接受裸 shell 字符串；需要 shell 语义时显式写成 `-- bash -lc '...'`。

运行状态默认保存在 `~/.feishu-bridge/bg_tasks.db`，任务文件默认保存在 `~/.feishu-bridge/bg_tasks/`。同一台机器运行多个 bridge（例如 production + staging）时，必须给每个实例设置不同的 `FEISHU_BRIDGE_BG_HOME`，例如 `~/.feishu-bridge-staging`，否则多个 bot 会竞争同一个 SQLite 队列和 `wake.sock`。完成任务会写入 `completed/<task_id>/task.json.done`，manifest 主要字段如下：

```json
{
  "schema_version": 2,
  "task_id": "uuid4_hex",
  "state": "completed",
  "exit_code": 0,
  "signal": null,
  "duration_seconds": 1.2,
  "command_argv": ["sleep", "10"],
  "stdout_tail_b64": "",
  "stderr_tail_b64": "",
  "output_paths": [],
  "on_done_prompt": "done",
  "chat_id": "oc_xxx",
  "session_id": "sess_xxx"
}
```

升级到包含后台任务的版本后，首次启动会自动创建 `~/.feishu-bridge/`、`bg_tasks.db`、`wake.sock` 和 `bg_tasks/` 子目录；已有目录不会被覆盖。若 `bg_tasks.db` 损坏，启动 reconciler 会把旧文件重命名为 `bg_tasks.db.quarantine.<ts>`，再从已提交的 `task.json.done` manifest 重建可恢复行。

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
