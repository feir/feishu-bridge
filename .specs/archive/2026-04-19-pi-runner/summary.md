# Summary: Pi Runner

Date: 2026-04-19

Pi is now integrated as a Feishu Bridge runner for local oMLX-backed usage.

## Completed

- Added `PiRunner` with JSON streaming support.
- Registered `agent.type = "pi"`.
- Added model aliases including `Qwen3.6-35B-A3B-mxfp4`.
- Enforced read-only default tools: `read,grep,find,ls`.
- Mapped Pi text, tool status, usage, model usage, and error events into the bridge runner contract.
- Exposed actual Pi model names through `/model`, `/status`, and response metadata.
- Documented Pi/oMLX setup and safety defaults in README.
- Added compact Pi context files:
  - `/Users/feir/.pi/agent/AGENTS.md`
  - `/Users/feir/.claude/.pi/APPEND_SYSTEM.md`
- Validated staging bot behavior after upgrade to `v2026.04.19.6`.
- Completed read-only dogfood and final review.

## Deferred

- Write/edit tool enablement.
- Shell tool enablement.
- Measuring token impact before enabling Pi context files in staging/production.
- Optional write/edit and shell tracks can reopen as a separate spec when needed.

## Release

- Commit: `918423f Improve Pi runner error handling`
- Tag: `v2026.04.19.7`
- Staging: upgraded and restarted on `feishu-bridge-staging`
