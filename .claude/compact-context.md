Project: feishu-bridge

- Python package and CLI bridge for Feishu <-> Claude Code / Codex.
- Main commands:
  - install dev env: `pip install -e '.[dev]'`
  - run tests: `pytest tests/unit/ -v`
  - start bot: `feishu-bridge --bot <bot-name>`
  - inspect CLI: `feishu-cli --help`
- Deploy:
  - macOS: `bash contrib/feishu-bridge-launcher.sh`
  - Linux: `contrib/feishu-bridge@.service` + `systemctl --user`
- Config:
  - `~/.config/feishu-bridge/config.json`
  - `~/.config/feishu-bridge/.env`
- Feishu constraints:
  - group bots only receive messages that @mention the bot
  - bot sender identity may require REST lookup in worker layer
  - do not add network I/O inside WebSocket event callbacks
- Documentation rule:
  - README claims must be checked against code after behavior/config/deploy changes
