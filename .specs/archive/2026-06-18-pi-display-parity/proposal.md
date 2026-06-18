---
branch: master
start-sha: c3cff17f95a2575a7e912b5bfcf9c2eb1c98517a
status: active
bootstrap: false
---

# Proposal: pi-display-parity (Phase 3: 独立工具进度卡)

## WHY

feishu-bridge 的工具进度在流式期间以**纯 markdown 文本**渲染在 CardKit v2 流式卡片的 `todo_progress` 元素中，表现为平铺展开的多行：

```
▸ 执行命令 `date`
▸ 执行命令 `ls`
▸ 执行命令 `launchctl`
```

而 pi-feishu 同样使用 CardKit v2 schema（`schema: "2.0"`），每个工具调用却是 **`collapsible_panel`**，默认折叠只显示一行 header（如 `⏳ Shell · date`），视觉效果紧凑干净。

## 对比分析：pi-feishu vs feishu-bridge

### 共同点
两者都使用 CardKit v2 的 `schema: "2.0"` 格式，元素类型（`markdown`、`collapsible_panel` 等）完全一致。

### 差异

| 维度 | pi-feishu | feishu-bridge |
|------|-----------|---------------|
| Card schema | `schema: "2.0"` | `schema: "2.0"` |
| 创建 API | `im.message.create` | `cardkit.v1.card.create` |
| 更新 API | `im.message.patch`（整卡替换） | `cardkit.v1.card_element.content`（元素 patch） |
| 流式模式 | 无（`streaming_mode=false`） | 有（`streaming_mode=true`） |
| 工具展示 | `collapsible_panel` body 元素，动态追加 | 流式期：`markdown` 文本；终态：`collapsible_panel` |
| 文本流式 | 不支持（整卡刷） | ✅ 支持（元素级增量） |
| 卡片数量 | **2 张卡**：工具进度卡 + 文本回复卡 | **1 张卡**：全塞一张流式卡 |

### 为什么不能在 CardKit v2 流式卡里直接实现

1. **body 不可动态增删**：流式卡 body 元素在创建时固定，`card_element.content` 只能更新已有元素内容，不能追加/删除元素
2. **预建面板池方案失败**：PoC 验证表明，预建的空壳 `collapsible_panel`（`content=""` + `expanded=false`）在流式卡中仍显示为可见空面板，不可接受
3. **`card_element.content` 只接受 string**：无法将 markdown 元素替换为 collapsible_panel

## WHAT

**采用两张卡分离架构**，与 pi-feishu 一致：

1. **文本流式卡**（CardKit v2 streaming）→ 保持现有逻辑，只管助手文本流式显示
2. **工具进度卡**（IM 可编辑卡）→ 新建，通过 `im.message.patch` 整卡替换追加面板

### 数据流

```
工具开始/结束事件
  → 构建 collapsible_panel 元素数组（所有已知 tool_history entry）
  → im.message.patch 整卡替换工具进度卡
  → 用户看到实时更新的折叠面板列表

助手文本 chunk
  → card_element.content 更新文本流式卡（不变）

最终回复
  → 停止流式，交付最终文本卡 + 工具面板（可选合并为 1 张完成卡）
```

### 变更范围

1. `ResponseHandle` 新增属性：
   - `_tool_msg_id: str | None` — 工具进度卡消息 ID
   - `_tool_card_pool_prepared: bool` — 是否已发送工具卡
2. `_render_progress()`：不再向 CardKit todo 元素写 markdown，改为构建 panels → `im.message.patch` 工具卡
3. `build_cardkit_streaming_card()`：从 body elements 移除 `CARDKIT_TODO_ELEMENT_ID` 和 `CARDKIT_LOADING_ELEMENT_ID`
4. `_perform_flush()` / `deliver()`：最终交付时合并或独立发送工具面板
5. 新增 `_build_tool_progress_card(panels)`：构建 IM 可编辑卡 JSON

### 工具面板格式

```json
{
  "collapsible_panel": {
    "expanded": false,
    "header": {
      "title": {"tag": "markdown", "content": "⏳ 执行命令 · date"},
      "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined", "size": "16px 16px"},
      "icon_position": "right", "icon_expanded_angle": -180
    },
    "border": {"color": "grey", "corner_radius": "5px"},
    "vertical_spacing": "8px", "padding": "8px 8px 8px 8px",
    "elements": [{"tag": "markdown", "content": "**工具名**: Bash\n**参数摘要**: `date`"}]
  }
}
```

## NOT

- 不修改 CardKit v2 流式卡的创建/更新逻辑（除了移除无用元素）
- 不改变 `_tool_history` 数据结构
- 不处理 subagent 结果的实时面板更新（Phase 2 已预留字段，仍走 `_render_progress` 中 agents 部分）
- 不做最终卡片的 collapsible_panel 合并（`_build_tool_panels` + `build_cardkit_final_card` 已经处理）

## RISKS

| Risk | Mitigation |
|------|-----------|
| `im.message.patch` 限频 | 工具事件已有自然间隔；加 `_render_progress` 的 throttle 复用现有的 flush throttle |
| 工具卡创建失败 | 降级回 CardKit todo 元素 markdown 文本（保留旧渲染路径作为 fallback） |
| 两张卡视觉分离不协调 | 工具卡用 reply 到用户消息（同一 thread），与流式卡紧邻 |
| 低工具量场景（1-2 个工具）两张卡占空间 | 延迟创建工具卡（第一个工具到达时才创建），无工具时不创建 |

## Decision Log

| Date | Decision | Reason |
|------|----------|--------|
| 2026-06-18 | Phase 3 方案：独立工具进度卡（IM API） | PoC 证实 CardKit v2 流式卡 body 无法动态追加元素，预建空面板会显示空壳；pi-feishu 已验证两张卡分离方案 |
| 2026-06-18 | 工具卡使用 `im.message.patch` 整卡替换 | pi-feishu 的同款方式，简单可靠，支持 collapsible_panel 动态数量变化 |
| 2026-06-18 | Hotfix: F1 跳过名单补 Subagent；F2/F3 Subagent tasks[] 多任务支持；F4 延迟主卡创建 | 落地后发现 Subagent 多任务分发错通道 + 无文本回合产生空白卡，4 处精准修复 |
