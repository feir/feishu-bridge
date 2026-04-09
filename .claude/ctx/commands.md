## Dev Commands

- 安装开发环境：`pip install -e '.[dev]'`
- 运行单元测试：`pytest tests/unit/ -v`
- 查看 CLI 帮助：`feishu-cli --help`
- 本地启动 bot：`feishu-bridge --bot <bot-name>`

## Deploy Commands

- macOS launchd：`bash contrib/feishu-bridge-launcher.sh`
- Linux systemd：
  - `cp contrib/feishu-bridge@.service ~/.config/systemd/user/`
  - `systemctl --user daemon-reload`
  - `systemctl --user enable --now feishu-bridge@<bot-name>`

## Validation Rules

- 改动桥接逻辑后，至少检查相关单元测试或补最小测试。
- 改动配置结构、部署步骤、命令格式后，必须核对 `README.md`。
- 改动会话、压缩、流式输出相关逻辑后，优先看 `tests/unit/test_bridge.py` 和 `tests/unit/test_quota.py` 是否需要更新。
