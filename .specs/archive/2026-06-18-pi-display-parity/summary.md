# Summary: pi-display-parity (Phase 3)

## 概要

将 feishu-bridge 的工具进度从 CardKit 流式卡内的平铺 markdown，改造为独立 IM 卡的 `collapsible_panel` 折叠面板，对齐 pi-feishu 的两卡分离架构；并通过 3 轮 Codex 评审闭环修复 Phase 3 落地后发现的空白主卡与 Subagent 渲染缺陷。

## 变更内容

- **Phase 3 主体**：
  - 流式卡瘦身：移除 `CARDKIT_TODO_ELEMENT_ID` / `CARDKIT_LOADING_ELEMENT_ID` 及相关常量/方法
  - 新增独立 IM 工具进度卡：`_send_tool_card` / `_update_tool_card` / `_build_tool_panels_for_streaming`，reply 到 source_message_id
  - `_render_progress` 拆分为 `_render_tool_progress`（独立 IM 卡）+ `_render_agent_progress`（CardKit `agent_status` 元素）
  - 限频保护 `_TOOL_CARD_THROTTLE_S = 0.8`
- **Hotfix（落地后发现）**：
  - 空白主卡修复：`tool_status_update` / `agent_list_update` 不再主动 `_ensure_card()`；主卡延迟到首个文本 chunk 或 deliver 才创建；`_tool_history` 与主卡解耦
  - Subagent 多任务支持：`_extract_hint_data` 与 `runtime_pi._emit_tool_status` 支持 `tasks[]` → `pending_agent_launches` 多条派发
  - Subagent 永不进 `pending_tool_status`：分支上移至 `if not args:` 之前；empty/non-dict args 走 warning + return
  - UI 跳过名单补 `"Subagent"`，防止 tool card 与 agent_status 双重渲染
- **测试**：新增 8 个用例覆盖 tasks[] 多任务、empty/non-dict args defer/warn、无主卡时 tool card 真发送路径
- **发版**：`v2026.06.18.3` 已发布到 PyPI + GitHub Release

## 关键决策

- **两卡分离 over 单卡集成**：PoC 验证 CardKit v2 流式卡 body 无法动态追加元素，预建空 panel 会显示空壳 → 采用 pi-feishu 同款两张卡方案
- **工具卡用 `im.message.patch` 整卡替换**：简单可靠，支持 collapsible_panel 动态数量变化
- **Subagent 完全独立于 tool_history**：UI 全面跳过 + runtime 完全闭合 fallback 路径，杜绝渲染歧义
- **主卡延迟创建**：tool-first 回合不再发空壳卡，等 deliver 时已有最终文本或 fallback 文案

## 影响范围

| 文件 | 改动 |
|------|------|
| `feishu_bridge/ui.py` | +247/-72：Phase 3 主体 + F1/F4 hotfix |
| `feishu_bridge/runtime.py` | +19：F2 `_extract_hint_data` tasks[] 支持 |
| `feishu_bridge/runtime_pi.py` | +63/-17：F3 Subagent 分支重构 |
| `tests/unit/test_pi_runner.py` | +199：tasks[] / empty args / non-dict args 测试 |
| `tests/unit/test_tool_progress.py` | +85/-7：无主卡 tool card 真发送测试等 |

最终：**1105 unit tests passed, 7 skipped, 0 failed**；Codex 3 轮评审 → APPROVE。
