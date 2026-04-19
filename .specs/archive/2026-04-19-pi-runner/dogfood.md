# Dogfood: Pi Runner Read-Only Graduation

Date: 2026-04-19

## Environment

- Bridge version: `v2026.04.19.6`
- Bot: `feishu-bridge-staging`
- LaunchAgent: `com.feishu-bridge-staging`
- Runner: `pi`
- Provider profile: `pi-local`
- Model: `Qwen3.6-35B-A3B-mxfp4`
- Workspace: `/Users/feir/.claude`
- Default tools: `read,grep,find,ls`

## Sessions

1. Basic conversation
   - Input: `hi`
   - Result: PASS
   - Evidence: staging bot returned a normal Pi response.

2. Read-only tool smoke
   - Input: ask Pi to list the workspace top-level files.
   - Result: PASS
   - Evidence: Pi called `ls`, tool result `isError=false`, final Feishu card update succeeded.

3. Restart and resume
   - Action: restart `com.feishu-bridge-staging`, then send a follow-up message.
   - Result: PASS
   - Evidence: bridge loaded session map with `_agent_type=pi:pi-local`; follow-up logged `Pi: resume=True`; Pi answered from previous session context.

4. Operational commands
   - `/model`: PASS, returned `Qwen3.6-35B-A3B-mxfp4` and aliases `pi / qwen / gemma / qwen35b`.
   - `/status`: PASS, returned `1,646 / 32,768 tokens` and model `Qwen3.6-35B-A3B-mxfp4`.

## Failures

No read-only dogfood failures recorded.

## Decision

PiRunner is acceptable for read-only staging use. Write/edit and shell tools remain blocked until separate disposable-file and command-policy tests are completed.

