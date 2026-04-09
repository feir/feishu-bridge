## Project Overview

- `feishu-bridge` 是 Python CLI + bridge，负责在飞书和 Claude Code / Codex 之间转发任务。
- 主入口：
  - `feishu-bridge` → `feishu_bridge.main:main`
  - `feishu-cli` → `feishu_bridge.cli:main`
- 默认配置位置：
  - `~/.config/feishu-bridge/config.json`
  - `~/.config/feishu-bridge/.env`
- 交互模型：
  - 飞书 WebSocket 事件进入本进程
  - bridge 调用 Claude Code / Codex CLI
  - 输出被重渲染为飞书消息卡片

## Working Rules

- 面向人的说明、注释、文档默认用简体中文；代码、命令、配置 key 保持英文。
- 涉及 bridge 行为、权限、上下文压缩、流式输出、事件字段时，先读实现或测试，不凭印象修改。
- 修改 CLI、配置结构、部署方式、权限模型后，要同步检查 `README.md` 是否仍然准确。
