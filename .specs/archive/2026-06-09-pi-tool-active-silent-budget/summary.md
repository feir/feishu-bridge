# Summary: pi-tool-active-silent-budget

## 概要
修复 pi 会话"长时间卡住不响应"。根因：固定 480s silent timeout 在 pi 执行单个长工具（下载/构建/sleep 循环）期间无助手文本输出时误判为 hang 并 SIGKILL。诊断经 Codex 跨模型确认，采用 Approach B（动态静默预算）。

## 变更内容
- `compute_silent_budget()`：max(base 480, bg-agent 3600, tool-active 1800) 动态合成，保留既有 bg_agent_running 单向 latch
- pi `tool_execution_start/end`（原 no-op）→ 驱动 `tool_active_count` + `pending_silent_reset` 心跳；turn_end/error 归零（best-effort，clamp ≥0）
- `_run_streaming` silent 计时器加 `_silent_lock` 代际守卫，关闭迟到心跳后旧 Timer 赢 kill 竞态；recompute-before-reset 有序路径保证工具结束后预算 1800→480
- todo/agent 进度重置与 UI 回调注册解耦
- `Popen stdin=DEVNULL` 加固回合后进程驻留
- silent-timeout 结果暴露 `tool_was_active` 元数据
- `PI_TOOL_ACTIVE_BUDGET_ENABLED` feature flag（默认 on）现场回滚

## 关键决策
- Approach B（双向动态预算）而非 A（单向 latch）：精确区分"工具在飞"vs"模型 hang"，保留 480s hang 兜底
- fix #4（auto-continue 进度判据）拆 follow-up，本 change 仅暴露 tool_was_active 元数据
- TOOL_ACTIVE_SILENT_TIMEOUT=1800（折中合法长工具与 hang 发现速度）
- 计时器代际守卫加锁（Codex code-review MEDIUM）而非 monotonic deadline（stdout 阻塞读不适配轮询）

## 影响范围
- feishu_bridge/runtime.py（+116/-28）、feishu_bridge/runtime_pi.py（+25）
- tests/unit/test_silent_budget.py（新增 17 测试）
- Claude/OMP runner 不受影响（新 StreamState 字段默认 inactive，全量 1085 passed 验证）
- 已发版 v2026.06.09
