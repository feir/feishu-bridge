---
branch: master
start-sha: 4db8095985e662cd792439def3132b007941fa61
status: active
bootstrap: false
---

# Proposal: pi-subagent-status

## WHY

飞书卡片查看者（用户）在 Pi runner 执行 subagent 时，只能看到 `▸ Subagent`，无法得知调用了哪个 agent（scout/developer/git-ops）、执行什么任务。期望效果：运行时显示 `◉ task描述 (agent名)`，完成后变为 `~~☑ task描述 (agent名)~~`。OMP runner 对 Claude Code 的 `Agent`/`Task` 已有完整的双通道支持可复用。

## WHAT

- Pi runner (`runtime_pi.py`) 识别 `subagent` 工具调用，提取 agent 名和 task 描述，发射到 `state.pending_agent_launches`
- **条件分流**：提取成功 → 走 agent 列表通道；提取失败 → 降级走工具历史通道（显示 `▸ 分发子任务`）
- 共享 hint 提取 (`runtime.py`) 增加 `Subagent` 分支，为降级路径提供 hint
- UI 层 (`ui.py`) 增加 `Subagent` 映射，条件性跳过工具历史通道

## NOT

- 不透传 subagent 内部进度（Pi 子进程事件流不暴露给 bridge，改造成本过高）
- 不修改 Pi 的 subagent 插件本身（`pi-minimal-subagent`）
- 不修改 OMP runner 的现有逻辑

## RISKS

| Risk | Mitigation |
|------|-----------|
| subagent 参数格式随 pi-minimal-subagent 插件更新变化 | Defensive coding，提取失败时降级走工具历史通道（`▸ 分发子任务`），不会出现两通道都没输出的情况 |
| subagent 完成标记时机 | 验证 `_mark_agents_completed` 在 Pi runner 文本输出时被触发，与 OMP 行为一致 |
| description 去重可能合并同文本不同 agent 的 launch | 接受为已知限制，实际场景中同一轮同文本 task 极少 |

## Decision Log

| Date | Decision | Reason |
|------|----------|--------|
| 2026-06-01 | 复用现有 agent 列表双通道，不新建渲染通道 | 最小改动，与 OMP 行为一致 |
| 2026-06-01 | 条件分流：提取成功走 agent_list，失败走 tool_history | Codex review CRITICAL #1: 避免双通道都无输出 |
| 2026-06-01 | description 去重合并为已知限制 | Codex review HIGH #2: 改动不对等 |
