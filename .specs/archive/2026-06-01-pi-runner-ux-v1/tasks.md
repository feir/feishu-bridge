# Tasks: pi-runner-ux-v1

> Validate 步骤给出具体命令/测试入口（plan-review finding#8）。新增测试集中在 `tests/unit/test_pi_runner.py`（新建）+ `tests/unit/test_bridge.py`（/new、ui）。

## 1. PiRunner 工具状态增强（Item 1）

- [x] 1.1 加 pi `_TOOL_NAME_MAP`（小写→PascalCase，含 `ls`→`Ls`、`find`→`Find`）+ `_normalize_pi_tool`，照抄 omp `runtime_omp.py:53,81`
  - Validate: `pytest tests/unit/test_pi_runner.py::test_normalize_pi_tool -q` —— 断言 read→Read、bash→Bash、ls→Ls、未知名 `.title()` 兜底
- [x] 1.2 StreamState 加 `_tool_seen` 集；**唯一权威源 = `toolcall_*`**：`_handle_message_update` 按 tool-call id 单次 emit `{name, hint_data}`；`parse_streaming_line` 中 `tool_execution_start`/`tool_execution_end` 改为对 `pending_tool_status` **完全 no-op**（删除现有 append）
  - Validate: `pytest tests/unit/test_pi_runner.py::test_tool_status_single_emit_per_id -q` —— 喂 tool_execution_start + toolcall_start + 两 end（同一 id），断言 `pending_tool_status` 恰 1 条 dict、name 已归一化、hint 来自 toolcall arguments
  - Validate: `pytest tests/unit/test_pi_runner.py::test_tool_execution_only_stream_no_status -q` —— 仅 `tool_execution_*`（无 toolcall_*、id-less）→ `pending_tool_status` 为空（已知限制，不产生裸名/重复）
- [x] 1.3 start 无 args 时不 emit；同 id 后续事件拿到 args 才 emit（不依赖 ui label-backfill）
  - Validate: `pytest tests/unit/test_pi_runner.py::test_emit_deferred_until_args -q` —— start 空 args → 0 条；end 带 args → 1 条带 hint。再加 `test_two_blank_starts_no_miscorrelation`：两个同名工具空参先后 start+end，断言各自 hint 正确、无错配
- [x] 1.4 ui/runtime 补 pi 特有取值：`ui.py` `_TOOL_STATUS_MAP` 加 `"Ls":"列出目录"`；`ui.py` `_format_tool_hint` 加 `Ls`→`os.path.basename` 分支（finding#3）；`runtime.py` `_extract_hint_data` 加 `Ls`（读 `path`）+ `Find` 兼容单数 `path`/`pattern`
  - Validate: `pytest tests/unit/test_pi_runner.py::test_format_tool_hint_ls -q` (co-located with Ls coverage)（Ls 全路径 `/a/b/c` → `c`）+ `pytest tests/unit/test_pi_runner.py::test_extract_hint_ls_find -q`（`_extract_hint_data("Ls",{"path":"/a"})` 非空、`("Find",{"path":"/a","pattern":"*.py"})` 非空）
- [x] 1.5 never-raises 防护：1.2/1.3 提取逻辑包 try/except，异常降级为裸工具名 + `log.debug`
  - Validate: `pytest tests/unit/test_pi_runner.py::test_tool_status_malformed_event_no_raise -q` —— 喂 `arguments` 非 dict / 缺字段 / 缺 id，断言不抛、流继续、降级为裸名

## 2. `/new` abort-then-new（Item 2）— 已存在，砍掉

> 实现中发现 `/new` 早已中止活跃 turn：main.py:1575-1581（消息接收/入队前路径）已 `runner.cancel(tag)` + `_chat_queue.drain(tag)`，commands.py:87 清 session。功能完整。唯一未做的是「区分中止 vs 重置」的文案（cosmetic），Captain 决定不做。Codex finding #6（漏 no-session 活跃 turn）一并作废：cancel 是 tag-based，与 session id 无关。

- [x] 2.1 ~~/new abort-then-clear~~ — 已存在（main.py:1580 `cancel(tag)`），无需改动


## 3. 按会话持久记忆（Item 3）

- [x] 3.1 新建 `feishu_bridge/pi_memory.py`：路径解析 mirror `_scope_hash(bot_id,chat_id,thread_id)`，root=`bridge_home()/feishu-bridge/pi-memory/`；`safe_read`（无锁、try/except、缺失=空）；`soft_tail_cap(raw, MAX_INJECT_BYTES)`（仅截断注入副本，**不写文件**）。**无任何写/prune 函数**
  - Validate: `pytest tests/unit/test_pi_runner.py::test_pi_memory_scope_and_read -q` —— 不同 (bot,chat,thread) → 不同路径；缺失文件 → 空串不抛；超 cap → 注入副本截断而磁盘文件字节数不变
- [x] 3.2 PiRunner `_build_system_prompt` 注入 "Persistent memory (this chat)" 段（已存内容 soft-cap + 绝对路径 + 写入协议：read-before-write/优先 edit/保留旧事实/字节预算）
  - Validate: `pytest tests/unit/test_pi_runner.py::test_memory_injection_isolation -q` —— scope A 注入含 A 内容、不含 B 内容（不同 thread_id 亦隔离）；prompt 含写入协议与绝对路径
- [x] 3.3 never-raises：memory 读取/注入异常 → 降级为不注入该段，turn 正常
  - Validate: `pytest tests/unit/test_pi_runner.py::test_memory_unreadable_no_raise -q` —— 文件不可读 → prompt 正常构建、无 memory 段、不抛
- [x] 3.4 确认 bridge 侧无任何对 memory 文件的写/删/prune 调用（CRITICAL 所有权约束）
  - Validate: `grep -rn "pi-memory\|pi_memory" feishu_bridge/ | grep -iE "write|open\(.+w|unlink|replace|prune"` 返回空（仅 read 路径）

## 4. 回归与集成

- [x] 4.1 全量单测
  - Validate: `pytest tests/unit/ -v` → 0 failed；新增用例全过
- [x] 4.2 真实 pi turn 冒烟（部署后手动，2026.06.01 实例）：触发 bash+read+ls，核对卡片
  - Validate: 飞书私聊触发工具 → 卡片显示中文标签 + 目标、单次计数、无裸名/重复。
  - Evidence: Captain 实测确认通过（2026-06-01），无问题。
- [x] 4.3 per-session memory 端到端（部署后手动，2026.06.01 实例）：让 pi 记一条事实 → 新 turn 读回
  - Validate: 飞书私聊「记住：偏好简体中文」→ 新 turn「我的语言偏好是什么？」→ pi 答「根据记忆文件中的记录…简体中文」。
  - Evidence: 磁盘文件 `~/.feishu-bridge/feishu-bridge/pi-memory/56598d95….md`（47B）由 pi 按注入协议写入：`## 用户偏好\n- 回复语言：简体中文`；bridge 读回注入，pi 据此应答。pi 独占写 + bridge 只读 + sha1(scope) 单文件均验证。隔离（chat B/另一 thread 读不到）由单测 test_memory_injection_isolation 覆盖，live 隔离待补（非阻塞）。
- [x] 4.4 文档核对
  - Validate: `grep -n "/new" README.md` 确认 `/new` 行为描述段；若行为变化已更新，否则记录"无需改"

## Review Report

### Round 1 (2026-06-01, basis: 989bd97+dirty)

**Codex Verdict: WARNING (0 CRITICAL, 1 HIGH, 1 MEDIUM, 1 LOW)**

- [HIGH] `worker.py:1171` — Pi memory fresh_context 注入缺少 process_message() 级集成测试（resume/new/非Pi三路径均未被 mock 断言）。Fix: 补 process_message mock test 三路径。
- [MEDIUM] `runtime_pi.py:249` — 畸形事件行为与 spec drift：实现静默 drop（no emit），spec 说"降级为裸名"。Fix: 有 name 无 hint 时 emit `{name, hint_data:""}` 而非 drop；更新测试。
- [LOW][SCOPE] `.specs/archive/` 文件混入 pi-runner-ux-v1 diff（spec housekeeping，非 NOT 项）。Fix: 独立 commit 处理或 proposal 声明 bundling。

### Round 2 (2026-06-01, basis: 989bd97+dirty)

**Codex Verdict: APPROVE-eq (0 CRITICAL, 0 HIGH, 0 MEDIUM, 1 LOW)** — Round-1 HIGH+MEDIUM 均验证解决，Codex 标 WARNING 仅因新 LOW；按 approval 标准无 CRITICAL/HIGH = Approve。

- [RESOLVED HIGH] worker 注入集成测试已补：`test_pi_memory_injected_on_new_session`（FRESHPIMEM）/`_on_resume`（PIMEM）/`test_non_pi_runner_fresh_context_unchanged`（FRESH / None）。Codex 确认三测试真实走 worker.py:1178-1210 分支、monkeypatch 忠实。
- [RESOLVED MEDIUM] 畸形事件改为降级裸名 `{name, hint_data:""}`（runtime_pi.py `_emit_tool_status`）；test 更新。Codex 确认 id-bearing 正常流仍单次 hinted emit，无 bare+hinted 双发。
- [LOW accepted] id-less split call 迟到 hint 丢失（runtime_pi.py:268）。真实 pi 每 toolCall 均带 id（session 数据已证），仅畸形流受影响，可接受降级，已加注释说明。不修。

## Spec-Check

- result: PASS
- reviewer: code-review
- basis: HEAD=4db8095 (released v2026.06.01)
- timestamp: 2026-06-01
- notes: 12/12 tasks 勾选并验证。Round-1 HIGH+MEDIUM 已修、Round-2 复审 Approve-eq（仅 1 accepted LOW：id-less split call 退化，真实 pi 必带 id，已注释）。全量单测 1060 passed。Live 验证（2026.06.01 实例）：4.2 工具状态卡片 Captain 实测通过；4.3 per-session memory 持久化读回 + pi 按协议写入磁盘文件（pi 独占写/bridge 只读/sha1 scope 均确认），隔离由 test_memory_injection_isolation 覆盖。LOW scope（archive housekeeping）已在提交时分离为独立 commit（8acd9f7/a7ec863），feature commit fedf230 纯净。已发布 v2026.06.01（PyPI + GitHub Release）。
