# Tasks: pi-display-parity — Phase 3: 独立工具进度卡

## 1. 流式卡瘦身（P0）

- [x] 1.1 `build_cardkit_streaming_card()`：从 body elements 移除 `CARDKIT_TODO_ELEMENT_ID` 和 `CARDKIT_LOADING_ELEMENT_ID`
- [x] 1.2 清理相关常量和引用：删除 `CARDKIT_TODO_ELEMENT_ID`、`CARDKIT_LOADING_ELEMENT_ID`、`CARDKIT_LOADING_ICON_IMG_KEY`；删除 `_clear_loading_icon()`；删除 `_loading_icon_cleared`
- [x] 1.3 验证 pytest 全绿（1093 passed）

## 2. 工具进度卡（IM 可编辑卡）（P0）

- [x] 2.1 `ResponseHandle.__init__` 新增 `self._tool_msg_id: Optional[str] = None`
- [x] 2.2 `_send_tool_card(panels)`: 构建 `schema: "2.0"` IM 卡，reply 到 `source_message_id`
- [x] 2.3 `_update_tool_card(panels)`: `im.message.patch` 整卡替换，首次 `_send_tool_card`
- [x] 2.4 `_build_tool_panels_for_streaming()`: `collapsible_panel` 数组，最近 10 条，状态图标（⏳/✅）
- [x] 2.5 状态行：`**执行中 (N)**` 置于 panels 上方

## 3. `_render_progress` 改道（P0）

- [x] 3.1 删除工具 markdown 拼接逻辑
- [x] 3.2 `_render_agent_progress()`: 子 agent 状态 → `CARDKIT_AGENT_ELEMENT_ID`
- [x] 3.3 `_render_tool_progress()`: `_build_tool_panels_for_streaming()` → `_update_tool_card()`
- [x] 3.4 `CARDKIT_AGENT_ELEMENT_ID = "agent_status"` 预留在 streaming card body

## 4. 最终交付整合（P0）

- [x] 4.1 `_deliver_cardkit()` 保留现有 `_build_tool_panels()` 逻辑不变
- [x] 4.2 IM 工具卡在最终交付后保留最后一帧（✅ 全部完成状态）

## 5. 降级路径

- [ ] 5.1 工具卡失败时 fallback markdown（TODO: 后续迭代）
- [x] 5.2 限频保护：`_TOOL_CARD_THROTTLE_S = 0.8`，首次创建不受限，后续 patch 限频

## 6. 验证

- [x] 6.1 全量 pytest 通过（1093 passed）
- [ ] 6.2 E2E 手动验证：飞书卡片渲染
- [x] 6.3 清理 test_tool_progress.py 中 `_loading_icon_cleared` 引用（已移除）

## 7. Hotfix（落地后发现）

- [x] 7.1 `tool_status_update` 跳过名单补 `Subagent`（F1：防止 Subagent 同时在 tool card 和 agent_status 双重渲染）
- [x] 7.2 `_extract_hint_data` 的 Subagent 分支支持 `tasks[]` 多任务分发（F2）
- [x] 7.3 `_emit_tool_status` 的 Subagent 分支支持 `tasks[]` → `pending_agent_launches`（F3）
- [x] 7.4 流式卡延迟到首个文本 chunk 才创建（F4：修复空白主回复卡）
- [x] 7.5 `agent_list_update` 不再强制 `_ensure_card()`，缓存到 `_active_agents` 延迟渲染（F4 配套）
- [x] 7.6 更新相关测试：`test_extract_hint_subagent_tasks_multi`（新增），`test_subagent_tasks_multi_*`（4 个新增），`test_subagent_excluded_from_tool_history`（重命名/适配）
- [x] 7.7 全量 pytest: 1098 passed → 1105 passed (round 2) → 1105 passed (round 3 final), 7 skipped, 0 failed
- [x] 7.8 Round 2 修复：`tool_status_update` 解耦 tool_history 与主卡；`runtime_pi` Subagent 降级路径不再 emit `pending_tool_status`（Codex round-1 HIGH+MEDIUM）
- [x] 7.9 Round 3 修复：Subagent 分支提到 `if not args:` 之前，empty/non-dict args 全部 warning+return 不再 push tool_status；新增 `_send_tool_card` 真发送路径测试（Codex round-2 MEDIUM+LOW）

## Done Criteria

- ✅ CardKit v2 流式卡仅含文本 marker + agent status 元素，无 tool todo
- ✅ 独立 IM 工具卡：collapsible_panel，⏳/✅ 状态图标，reply 到用户消息
- ✅ `_build_tool_panels`（最终卡）未受影响
- ✅ 主卡延迟到首个文本 chunk 或 deliver 创建，避免 tool-first 回合空白卡
- ✅ Subagent 永不进 `pending_tool_status`，仅走 `pending_agent_launches` 或 warning
- ✅ 1105 passed, 7 skipped, 0 failed

## Review Report

### Round 1 (2026-06-18, basis: c3cff17f+dirty)

Codex (gpt-5.5[high]) 评审 F1–F4 hotfix:
- [HIGH] `tool_status_update` 早返回导致首个 tool 事件被丢弃 — `_tool_history` 不累积，独立 IM 工具卡不出现
- [MEDIUM] Subagent fallback path 被 UI 跳过名单静默丢弃 — pi runtime 降级 push `pending_tool_status` 后 UI 不渲染
- Verdict: BLOCK

### Round 2 (2026-06-18, basis: c3cff17f+dirty)

修复后 Codex 再评：
- HIGH: RESOLVED — `_tool_history` 解耦主卡，无主卡时仍累积并触发独立工具卡
- MEDIUM: NOT RESOLVED — empty/non-dict args 仍走 `if not args:` 通用分支推 `pending_tool_status`
- 新 [MEDIUM] Subagent empty-args 路径未真正闭合
- 新 [LOW] 测试 mock 了 `_render_progress`，没覆盖真实 `_send_tool_card` send 路径
- Verdict: BLOCK

### Round 3 (2026-06-18, basis: c3cff17f+dirty)

修复 round-2 finding 后 Codex 再评：
- MEDIUM: RESOLVED — Subagent 块上移至 `if not args:` 之前，所有 Subagent 路径在到达 `pending_tool_status.append` 之前 return
- LOW: RESOLVED — 新增 `test_send_tool_card_no_main_card_replies_with_collapsible_panel` mock IM client 验证 `message.reply` 被调用且 body 含 collapsible_panel
- 0 critical / 0 high / 0 medium / 0 low
- Verdict: APPROVE

## Spec-Check

- result: PASS
- reviewer: code-review
- basis: HEAD=1c95d52+dirty
- timestamp: 2026-06-18
- notes: Phase 3 主任务全部勾选；hotfix 7.1–7.9 覆盖 3 轮 codex 评审。Deferred items: 5.1 (工具卡 fallback markdown) 和 6.2 (E2E 飞书联调) 显式标记后续 — 不阻塞归档。1105 passed unit tests, 0 regressions.
