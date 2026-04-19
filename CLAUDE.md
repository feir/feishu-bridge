# Feishu Bridge Project Rules

本仓库的长期上下文索引在 `.claude/CLAUDE.md`，进入项目时优先读取它，再按需读取 `.claude/ctx/` 下的细分文档。

## Background Tasks

- 预计运行时间超过 90 秒、且用户需要完成汇报时，使用 `feishu-cli bg enqueue`。
- 短命令、需要即时交互的命令，直接在当前 runner 执行，不放入 bg task。
- 完全不需要回报给会话的维护命令，用普通 fire-and-forget 机制；不要伪造 `on_done_prompt`。
- bg task 命令必须用 argv：`feishu-cli bg enqueue --chat-id oc_xxx --on-done-prompt "done" -- sleep 10`。
- 不传裸 shell 字符串。确实需要 shell 语义时，显式写成 `-- bash -lc 'make test > out.log 2>&1'`。
- bridge/task-runner 用 `shell=False` 执行用户命令；不要绕过这个约束。
- 长任务应声明可审计输出路径：重复传 `--output-path /path/to/artifact`，完成通知会带上这些路径。
- 取消任务用 `feishu-cli bg cancel <task_id>`，不要直接 kill 用户进程，除非是在调试 bg reconciler 本身。

## Examples

```bash
feishu-cli bg enqueue \
  --chat-id oc_xxx \
  --session-id sess_xxx \
  --on-done-prompt "read stdout/stderr and summarize the result" \
  --output-path /tmp/report.txt \
  -- python3 scripts/slow_report.py

feishu-cli bg status <task_id>
feishu-cli bg cancel <task_id>
```
