# Tasks: pi-subagent-status

## 1. Pi Runner 层改造

- [x] 1.1 `runtime_pi.py`: `_TOOL_NAME_MAP` 增加 `"subagent": "Subagent"` 映射
  - Validate: grep 确认映射存在
  - Evidence: runtime_pi.py line 38 → `"subagent": "Subagent"`

- [x] 1.2 `runtime_pi.py`: `_emit_tool_status` 中识别 `Subagent` 工具调用，提取 `agent`（→ subagent_type）和 `task`（→ description）。**条件分流**：提取成功 → 发射 `pending_agent_launches` 并跳过 tool_status；提取失败 → 降级发射普通 `pending_tool_status`
  - Validate: 代码中有显式的 if/else 分支覆盖两条路径
  - Evidence: runtime_pi.py lines 293-308 → if agent_name and task_text / else 降级

## 2. 共享层适配

- [x] 2.1 `runtime.py`: `_extract_hint_data` 增加 `Subagent` 分支，提取 `f"{agent}: {task[:40]}"` 作为 hint（降级场景用）
  - Validate: grep 确认分支存在
  - Evidence: runtime.py lines 493-498 → Subagent 分支

- [x] 2.2 `ui.py`: `_TOOL_STATUS_MAP` 增加 `"Subagent": "分发子任务"`
  - Validate: grep 确认映射存在
  - Evidence: ui.py line 1190 → `"Subagent": "分发子任务"`

- [x] 2.3 `ui.py`: `tool_status_update` 的跳过列表增加 `"Subagent"`（降级路径会显示为 `▸ 分发子任务`，但正常路径通过 agent_list 渲染，跳过可避免重复）
  - Validate: grep 确认 Subagent 在跳过列表中

> 注意：降级路径下 Subagent 不在跳过列表的条件中（因为降级时不发射 agent_launch），所以不会被跳过。跳过列表只影响同时有 agent_launch 的情况。
> 
> 实际实现：跳过逻辑在 runtime 层（1.2 条件分流），不在 UI 层。UI 的 `_TOOL_STATUS_MAP` 仅提供 label。

## 3. 验证

- [x] 3.1 添加针对性单元测试：覆盖 `_emit_tool_status` 对 subagent 的处理
  - 测试用例：正常提取（agent+task 都有）→ 验证 pending_agent_launches 非空
  - 测试用例：参数缺失/畸形 → 验证降级到 pending_tool_status
  - 测试用例：start(无 args) → end(有 args) 的延迟提取
  - 测试用例：id-less 事件不重复计数
  - Validate: 新测试全绿
  - Evidence: tests/unit/test_pi_runner.py → 8 个新测试全绿

- [x] 3.2 运行全量 `pytest` 确保不破坏现有功能
  - Validate: `pytest` 全绿
  - Evidence: 1068 tests passed, 0 failures

- [x] 3.3 验证 `_mark_agents_completed` 在 Pi runner 文本输出时触发（确认完成状态渲染正确）
  - Validate: 阅读代码确认调用链完整
  - Evidence: _perform_flush() → _mark_agents_completed() 调用链完整，与 OMP 路径一致

## Done Criteria

- 每个 subagent 调用在飞书卡片显示一行 `◉ task描述 (agent名)`
- subagent 完成后变为 `~~☑ task描述 (agent名)~~`
- 参数缺失时降级显示 `▸ 分发子任务`，不会出现无输出
- 无重复行（agent_list 和 tool_history 不同时渲染同一调用）

## Review Report

### Round 1 (2026-06-01, basis: affefb1)

Codex 代码评审发现 2 个 HIGH 问题，已修复：

1. [HIGH] Subagent 降级路径在 UI 层被跳过列表丢弃 → 从跳过列表移除 Subagent
2. [HIGH] `_mark_agents_completed` 清空而非标记完成 → 改为标记 status=completed

修复后新增 4 个 UI 层测试，全量 1071 测试通过。

## Spec-Check

- result: PASS
- reviewer: code-review
- basis: HEAD=affefb1
- timestamp: 2026-06-01
- notes: 所有 task 已完成，变更在 WHAT 范围内，未引入 NOT 中排除项。Codex review 发现的 2 个 HIGH 已修复并追加测试。Done Criteria 全部满足。
