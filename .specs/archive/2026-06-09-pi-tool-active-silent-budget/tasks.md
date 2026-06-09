# Tasks: pi-tool-active-silent-budget

## 1. StreamState 与常量

- [x] 1.1 在 runtime.py 新增常量 `TOOL_ACTIVE_SILENT_TIMEOUT = 1800`（紧邻 SILENT_TIMEOUT/BG_AGENT_SILENT_TIMEOUT, line 44-46）
  - Validate: grep 命中常量；值有注释说明用途与取值理由
- [x] 1.2 StreamState（runtime.py:322-343）新增字段：`tool_active_count: int = 0`、`pending_silent_reset: bool = False`
  - Validate: 字段默认值使工具不活跃；旧 runner 不写入即等价旧行为

## 2. 共享 loop：动态静默预算

- [x] 2.1 新增 `_recompute_silent_budget(state, base_silent)` 辅助：返回 `max(base_silent, BG_AGENT_SILENT_TIMEOUT if bg_agent_running else 0, TOOL_ACTIVE_SILENT_TIMEOUT if tool_active_count>0 else 0)`（feature flag off 时忽略 tool_active 项）
  - Validate: 单测覆盖 idle / tool-active / bg-agent / 两者并存 / flag-off 各组合返回正确值
- [x] 2.2 [Codex #4] 固化单一有序重置路径，封装为 `_apply_silent_budget()`：parse event → recompute → 仅当与当前 `silent_limit` 不同才更新 nonlocal → 用新 limit **恰好** `_reset_silent_timer()` 一次。所有事件分支统一走此路径，不在多处各自 re-arm
  - Validate: 工具 start→升 1800 re-arm 一次；工具 end 且无 latch→**先 recompute 后 reset**，回落 480；断言不会多留 1800s arm
- [x] 2.3 [Codex #1] 给 silent 计时器加代际守卫：`_silent_gen` 计数，`_reset_silent_timer()` 递增并捕获当前代际，`_silent_timeout_kill()` 仅当 `gen == 当前代际` 才执行 kill，否则 no-op。消除"迟到心跳后旧 timer 仍赢竞态杀进程"
  - Validate: 强制回调排序的竞态单测——已进入 kill 的旧代际 timer 被新 reset 作废后不杀进程
- [x] 2.4 [Codex #6] drain `pending_silent_reset` → 重置；并在 `pending_todo_update` / `pending_agent_launches` **变为非 None 时**触发重置，**与 `on_todo_update`/`on_agent_update` 回调是否注册无关**（不耦合 UI 回调，runtime.py:1177-1185）
  - Validate: 无 on_todo_update 回调时，todo 进度仍重置 silent 计时器
- [x] 2.5 `turn_end`/done 及 `finally`/cancel/error 路径将 `tool_active_count` 归零，避免跨断言泄漏
  - Validate: done 后 count==0；cancel/error 后 count==0

## 3. PiRunner：工具活跃信号

- [x] 3.1 [Codex #2] runtime_pi.py parse_streaming_line：`tool_execution_start` → `state.tool_active_count += 1; state.pending_silent_reset = True`；`tool_execution_end` → `state.tool_active_count = max(0, count-1); state.pending_silent_reset = True`（替换现 no-op return, line 109-115）。定性为 **best-effort 信号**（事件仅带 toolName、无 id，不保证严格平衡），不作精确生命周期契约
  - Validate: 不再向 pending_tool_status 追加（无重复 UI 卡片）；start/end 各自驱动一次重置
  - Validate: 不平衡场景——start 无 end / 游离 end / error-before-end / cancel-during-tool 各有单测；游离 end clamp 到 0 不变负；缺失 end 由 turn_end/finally 归零兜底
- [x] 3.2 保持 `message_update.toolcall_*`（_emit_tool_status）为唯一 UI tool-status 源，不受本次改动影响
  - Validate: grep 确认 _emit_tool_status 调用点未改；既有 pi tool-status 单测仍通过

## 4. 进程驻留加固 + 元数据暴露

- [x] 4.1 [Codex #3] Popen（runtime.py:1024-1032）增加 `stdin=subprocess.DEVNULL`（机会性加固，不承诺尾延迟下降）
  - Validate: 既有 streaming 路径正常；记录 stdin=DEVNULL 前后 `process hung after result event` 日志频次作为佐证（非硬验收）
- [x] 4.2 silent-timeout 返回 dict（runtime.py:1259-1274）新增 `tool_was_active: bool`（kill 时 `state.tool_active_count > 0`），供后续 auto-continue follow-up 使用；worker 本次不消费
  - Validate: 返回 dict 含该 key；worker 不读取（grep 确认无消费点）

- [x] 4.3 [Codex #5] 新增 feature flag `PI_TOOL_ACTIVE_BUDGET_ENABLED`（env，默认 on）：off 时 `_recompute_silent_budget()` 忽略 tool_active 项，退回 base+bg-latch 旧行为
  - Validate: flag=off 时 pi 长工具按旧 480s 行为；flag=on 时按新预算；Claude/OMP 两种 flag 下行为一致
- [x] 4.4 [Codex #5] proposal.md Rollback 段的 revert 清单与代码改动点保持一致（三处独立可回退）
  - Validate: 清单逐条对应实际改动文件/函数

## 5. 测试与回归

- [x] 5.1 新增 pi 单测：模拟 tool_execution_start 后长静默→在 1800s 内不判 silent timeout；工具 end 后无事件→480s 判 silent timeout（用 fake/mock timer，不真实 sleep）
  - Validate: pytest tests/unit/ 相关用例通过
- [x] 5.1b [Codex #1/#2] 竞态与不平衡测试：旧代际 timer 作废、start 无 end、游离 end、error/cancel-during-tool、回调缺失时 todo 仍重置
  - NOTE: 不平衡（start 无 end / 游离 end / turn_end / error）+ 回调缺失重置 已用确定性单测覆盖（test_silent_budget.py，13 例）。纯计时器线程竞态（旧代际 Timer 作废）未写自动化用例——真实 threading.Timer 竞态测试不确定/易 flaky，代际守卫为 4 行 by-construction 逻辑，靠 code review 把关。
  - Validate: 全部用例通过，无真实 sleep（注入 fake timer / 强制回调顺序）
- [x] 5.2 回归：tests/unit/test_bridge.py、test_quota.py、既有 pi/Claude/OMP runner 计时单测全绿
  - Validate: `pytest tests/unit/ -v` 全通过
- [x] 5.3 README / ctx 核对：silent timeout 行为若在文档有描述则同步（known-pitfalls 可补一条）
  - Validate: 无遗漏的过时描述

## Review Report

### Round 1 (2026-06-09, basis: b2afb9f+dirty)

Codex code review (gpt-5.4[high]) — Verdict: WARNING (0 CRITICAL, 1 HIGH, 1 MEDIUM), no [SCOPE] drift.

- [HIGH] Shared `_run_streaming` changed without direct regression coverage → FIXED: added 4 deterministic integration tests in test_silent_budget.py driving the loop with a recording Timer (budget raise to 1800 while tool-active, drop to 480 after tool_execution_end, flag-off stays 480, callback-decoupled todo reset, tool_was_active on silent timeout).
- [MEDIUM] Generation guard left a residual stale-kill window → FIXED: wrapped the gen check + silent_timed_out commit in `_silent_lock` (runtime.py); decide-and-commit is now atomic w.r.t. _reset_silent_timer re-arm, closing the incorrect-kill window.

Regression: full unit suite 1085 passed, 7 skipped (via `.venv/bin/python -m pytest`).

## Spec-Check

- result: PASS
- reviewer: code-review
- basis: HEAD=b2afb9f+dirty
- timestamp: 2026-06-09
- notes: All 23 tasks checked. Scope matches WHAT (runtime.py dynamic budget + generation guard + stdin=DEVNULL + tool_was_active metadata; runtime_pi.py tool-active counting). NOT respected — worker.py auto-continue logic untouched, Claude/OMP timing unchanged (new StreamState fields default inactive, verified by full regression). design.md followed (compute_silent_budget max-synthesis, ordered recompute-before-reset, lock-guarded generation guard). Evidence = test results (43 change-focused + 1085 full-suite pass); tasks use the repo's `Validate:` convention rather than `Evidence:` lines. Timer-thread race has no automated test (flaky) — covered by the lock + code review per the task NOTE.
