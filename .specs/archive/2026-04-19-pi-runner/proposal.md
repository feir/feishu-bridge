---
branch: master
start-sha: dd78a557ce0382950b9f47678b3d4074f22dd781
status: draft
scope: Standard
created: 2026-04-18
---

# Proposal: pi-runner

## WHY

`omlx-local` 当前通过 `LocalHTTPRunner` 直接调用本地 OpenAI-compatible endpoint。该路径适合验证 oMLX 推理能力，但不是完整 agent runtime：

- 不自动加载 `AGENTS.md` / `CLAUDE.md` / project context files
- 不提供成熟的 session tree、compaction、tool event、skills、extensions
- bridge 需要继续扩展 prompt loading、tool policy、rules retrieval，导致飞书适配层承担 agent 控制面职责

`badlogic/pi-mono` 的 `@mariozechner/pi-coding-agent` 已提供 coding agent runtime、context file loading、tools、sessions、JSON/RPC integration、custom providers。将 Pi 作为 bridge runner，可以把职责重新分层：

```text
Feishu Bridge = 飞书消息、卡片、按钮、状态展示
Pi Runner     = agent runtime、workspace、rules、tools、session
oMLX          = 本地模型推理服务
```

## WHAT

新增 `PiRunner`，将 `pi` 作为 `agent.type` 的一个可选 runner：

```jsonc
{
  "agent": {
    "type": "pi",
    "command": "pi",
    "provider": "pi-local",
    "providers": {
      "pi-local": {
        "workspace": "/Users/feir/.claude",
        "models": {
          "pi": "omlx/local-main"
        },
        "args_by_type": {
          "pi": [
            "--provider", "omlx",
            "--model", "local-main",
            "--tools", "read,grep,find,ls"
          ]
        }
      }
    }
  }
}
```

Phased delivery:

1. **Discovery** — Verify Pi installation, JSON/RPC protocol, custom provider config, context file loading, and oMLX compatibility.
2. **Read-only MVP** — Add `PiRunner` using `pi --mode json` or `pi --mode rpc`, with read-only tools only.
3. **Bridge Integration** — Map Pi events to bridge `RunResult`, streaming card output, session map, `/model`, `/provider`, `/status`.
4. **Safety + Context Policy** — Add Pi-specific defaults: no write/bash tools by default, compact context files, explicit workspace, documented trust boundary.
5. **Graduation** — Enable write/bash through explicit opt-in after smoke tests and review.

Known local source checkout:

```text
/Users/feir/projects/pi-mono
```

Current local state as of 2026-04-18:

- Source checkout exists.
- `pi` is not on current `PATH`.
- `npm install` completed successfully.
- Root `npm run build` failed at its first `cd packages/tui` step despite the directory existing.
- Package-by-package build completed successfully for `tui`, `ai`, `agent`, `coding-agent`, `mom`, `web-ui`, and `pods`.
- `node_modules/.bin/tsx` exists.
- `packages/coding-agent/dist/cli.js` exists and reports version `0.67.68`.
- JSON mode emits a session event but cannot complete until a provider/model is configured.

## NOT

- **不替换** ClaudeRunner / CodexRunner。
- **不让** Feishu Bridge 重新实现 Pi 的 skills/extensions/session tree。
- **不在 MVP 开启** `bash` / `write` / `edit` 工具。
- **不全量注入** `/Users/feir/.claude/rules/*.md` 或 `rules/lessons.md`。
- **不在首版实现** Pi web UI、Slack bot、vLLM pods。
- **不假设** Pi provider 已内置 oMLX；需要通过 custom provider 或 models config 验证。

## RISKS

| Risk | Impact | Mitigation |
|------|--------|------------|
| Pi JSON/RPC event schema 与 bridge 需要的流式状态不匹配 | 飞书卡片无法展示 tool/status | Discovery 阶段采样真实事件，先做最小文本流，再扩展 tool event |
| Pi 默认工具权限过宽 | 远程飞书入口可触发写文件或 shell | MVP 强制 `--tools read,grep,find,ls`；写入和 bash 另设 opt-in flag |
| Pi context files 过大或规则冲突 | local model 注意力下降 | 使用 `~/.pi/agent/AGENTS.md` + project `.pi/APPEND_SYSTEM.md` 的 compact profile |
| Pi custom provider 对 oMLX streaming 兼容不足 | 本地模型不可用或 usage 缺失 | 先用 `openai-completions` 验证；必要时写 Pi extension provider |
| Bridge session map 与 Pi session ID 语义不同 | `/new`、resume、restart 后行为不一致 | 将 Pi session file/id 作为 bridge session_id；restart smoke test 必须覆盖 |
| Pi package/extensions 供应链风险 | 任意代码执行 | 禁止自动安装第三方 Pi packages；只使用 repo-pinned local extension |

## Decision Log

| Date | Decision | Reason |
|------|----------|--------|
| 2026-04-18 | Pi 作为独立 `agent.type="pi"` runner，而不是伪装成 `local` provider | Pi 是 agent runtime；oMLX 才是 inference provider |
| 2026-04-18 | MVP 使用 read-only tools | Feishu Bridge 是远程入口，默认写入和 shell 风险不可接受 |
| 2026-04-18 | oMLX 通过 Pi custom provider 接入 | 保留 Pi 的 context/session/tool runtime，同时复用 oMLX 性能 |
| 2026-04-18 | 不全量加载 dotclaude rules | 当前 `CLAUDE.md + rules/*.md + docs/rules/agents.md` 约 60 KB，local model 不应常驻全量规则 |
| 2026-04-18 | 使用 `/Users/feir/projects/pi-mono` 作为 Phase 0 源码基线 | 本地 checkout 已存在；package-by-package build passed; `pi` not yet on PATH |

## Acceptance Criteria

- `agent.type="pi"` can be configured and selected without affecting existing runner types.
- A Feishu message can execute through Pi and return a final card response.
- The Pi process runs with cwd equal to configured workspace.
- Pi loads the expected context file set in a smoke test.
- MVP tools are limited to read-only tools by default.
- `/new`, `/provider`, `/model`, `/status`, `/stop` have defined behavior for PiRunner.
- Restarting bridge does not corrupt the Pi session mapping.
- README documents Pi runner setup, oMLX provider setup, and security caveats.
