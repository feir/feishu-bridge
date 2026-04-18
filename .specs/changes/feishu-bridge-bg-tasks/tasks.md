# Tasks: feishu-bridge-bg-tasks

## 1. 数据层与 schema

- [x] 1.1 新建 `feishu_bridge/bg_tasks_db.py` —— SQLite schema + WAL 初始化 + `PRAGMA integrity_check` 包装 + migration helper
    - Validate: 从空库启动能创建 `bg_tasks` + `bg_runs`；重启幂等；`PRAGMA journal_mode=WAL`、`busy_timeout=5000`、`synchronous=NORMAL`、`foreign_keys=ON` 全部生效（查询验证）；CHECK constraint 拒绝 `kind != 'adhoc'`（本变更限制）
    - R3 schema：`bg_runs` 含 `wrapper_pid NOT NULL`、`wrapper_start_time_us NOT NULL`、`process_start_time_us`（μs 精度）、`session_resume_status`、`delivery_state CHECK IN ('not_ready','pending','enqueued','sent','delivery_failed')`、`enqueued_at`、`sent_at`（替代原 `delivered_at`）、`completion_detected_at`
- [x] 1.2 定义 `TaskState` enum + 合法状态转换表（含 `launching` 瞬态）
    - Validate: `TaskState.validate_transition(old, new)` 拒绝所有非法跳转（completed→running、orphan→running 等）；合法转换集：queued→{launching, cancelled}；launching→{running, failed}；running→{completed, failed, cancelled, timeout, orphan}
- [x] 1.3 实现 `BgTaskRepo` DAO：`insert_task / claim_queued_cas / set_state_guarded / get / list / set_cancel_requested / start_run / finish_run / mark_delivery_state / list_pending_deliveries`
    - Validate: 所有写操作透过 repo 方法；外部模块不直接写 SQL；`claim_queued_cas` 在并发 100 个 worker 同时认领 1 个 queued row 时只有一人返回 True；100 并发 `insert_task` ≤ 2s
- [x] 1.4 DB 损坏恢复模块：`integrity_check_and_maybe_quarantine()` + `rebuild_from_manifests(tasks_dir)`
    - Validate: 伪造损坏 DB（truncate 前 N 字节）启动 → 自动 quarantine → 从磁盘 `task.json.done` replay 回 DB；没有 manifest 的 `active/` 目录 → `state='orphan'`；`queued` pre-launch 任务丢失，日志明示

## 2. task-runner wrapper（新增二进制）

- [ ] 2.1 新建 `feishu_bridge/task_runner.py` —— console_script entry point `task-runner`
    - Validate: `pip install -e .` 后 `which task-runner` 能定位；`task-runner --help` 列出所有必需参数（`--task-id`, `--db-path`, `--tasks-dir`, `--runner-token`）
- [ ] 2.2 wrapper 生命周期 4 阶段（P→S→W→C）
    - **Phase P（pre-register）**：`setsid/setpgid` → 用 `libproc.proc_pidinfo(wrapper_self_pid)` 取自身 `wrapper_start_time_us` → INSERT `bg_runs`（仅 task_id/runner_token/wrapper_pid/wrapper_start_time_us/started_at，pid 字段暂 NULL，delivery_state='not_ready'）
    - **Phase S（spawn + link）**：`Popen(argv, shell=False, start_new_session=True)` → 立即 `libproc.proc_pidinfo(child_pid)` 取 `process_start_time_us` → **单事务** UPDATE `bg_runs SET pid=?, pgid=?, process_start_time_us=?` 并 `UPDATE bg_tasks SET state='running' WHERE state='launching' AND id=?`
    - **Phase W（wait + stream）**：`Popen.wait(timeout=0.5)` loop 同时覆盖子退出检测 + 500ms poll `bg_tasks.cancel_requested_at`；两个后台线程 (`_StreamCollector`) 流式写 `stdout.log/stderr.log` 并保留 4KB RAM tail window；`time.monotonic()` deadline 判 timeout；cancel 或 timeout → SIGTERM pgid → 5s grace → SIGKILL
    - **Phase C（commit + deliver）**：UTF-8 safe tail 4096B → 写 `task.json.tmp` + fsync + `os.rename` → `task.json.done` → rename active→completed 目录 → **单事务** UPDATE `bg_runs (finished_at, exit_code, signal, stdout_tail, stderr_tail, manifest_path, delivery_state='pending')` 和 `UPDATE bg_tasks.state=<terminal>` → UDS nudge `b'\x03' + uuid.bytes`
    - Validate: wrapper 进程组独立于 bridge（`ps -o pgid=` 不同）；每阶段边界 fault inject 后 reconciler 行为正确（详见 §7.5）；`runner_token` 与环境 `BG_TASK_TOKEN` 一致；μs 精度 start_time 区分快速重启 pid
- [ ] 2.3 wrapper cancel/timeout 处理（Phase W 子任务）：500ms poll `cancel_requested_at` + monotonic deadline；两路径共用 `terminate_child_pgid(grace=5s)` helper
    - Validate: cancel 从 CLI 发出 → wrapper ≤ 500ms 检测 → SIGTERM 到 child pgid；子不响应 → 5s 后 SIGKILL；timeout 路径同样行为；finished_at 记录 monotonic+epoch 双时钟
- [ ] 2.4 Phase C 单事务原子性（独立 task 以强调）：bg_runs 终态写入与 bg_tasks.state 同一事务；任一失败整体 rollback；事务外做 UDS nudge（nudge 失败不影响 DB 已 committed 状态）
    - Validate: 模拟 bg_tasks UPDATE 失败（例如 WHERE state 不匹配）→ bg_runs 不被 commit；重启 reconciler 看到仍是 running，从 manifest 重放

## 3. CLI 子命令

- [x] 3.1 扩展 `feishu_bridge/cli.py` 增加 `bg` 子命令组（enqueue / status / list / cancel）
    - Validate: `feishu-bridge-cli bg --help` 输出完整；每个子命令都有 `--help`
    - Evidence: argparse subparser group `bg` with four children (enqueue/status/list/cancel), dispatched via `_run_bg_command` in `feishu_bridge/cli.py`
- [x] 3.2 `bg enqueue` 实现：参数解析（argv 必须通过 `--` 分隔或 `--cmd-json`）→ JSON 序列化 `command_argv` → INSERT（状态 `queued`）→ UDS nudge → JSON stdout
    - Validate: bridge 不运行时仍能 enqueue 成功返回 task_id；DB 锁定 → 非零退出 + 明确错误；bare string 传入（未通过 `--` / `--cmd-json`）→ 非零退出拒绝；`--timeout-seconds` 省略时默认 1800；`--on-done-prompt` 缺失时非零退出
    - Evidence: 9 enqueue tests in `tests/unit/test_cli_bg.py` (positional argv, --cmd-json, reject bare, reject both-forms, missing --on-done-prompt rc=2, default timeout 1800, env overlay with `=` in value, env KEY without `=` rejected, enqueue-without-bridge-listening fail-open)
- [x] 3.3 `bg status / list / cancel` 实现
    - Validate: status 对不存在 task_id 退出码 1；list 按 `updated_at DESC` 限 `--limit`（默认 20）；cancel 已进入终态的 task 报错不改状态；cancel 仅设 `cancel_requested_at` + UDS nudge（从不直接改 state）
    - Evidence: 7 status/list/cancel tests (queued row report, unknown rc=1, list ordering DESC, list chat+state filter, cancel sets flag preserves state, cancel terminal refuses rc=1, cancel unknown rc=1)
- [x] 3.4 UDS wake client helper（CLI 侧）
    - Validate: wake.sock 不存在 / bridge 不在 → CLI 静默跳过 nudge（不影响 enqueue 成功）；存在时发送 payload 后立即 close，不等响应
    - Evidence: `_bg_nudge()` fail-open (FileNotFoundError / ConnectionRefusedError / OSError / timeout), 200ms settimeout, `test_nudge_delivered_to_listening_socket` verifies \\x01 delivery to bound AF_UNIX listener (17/17 tests pass in 1.96s)

## 4. Bridge 侧 supervisor + delivery watcher + wake 监听

- [ ] 4.1 `feishu_bridge/bg_supervisor.py` —— `BgSupervisor` 单例（随 bridge 主进程 boot）
    - Validate: start/stop 幂等；stop 不 kill wrapper（wrapper 负责自己生命周期）；bridge SIGKILL 后现有 wrapper 仍跑完并写 manifest
- [ ] 4.2 UDS listener 线程（parent dir 0700, socket 0600；`EADDRINUSE` 探测 + unlink 重建；bind 仍失败 WARN 不退出）
    - Validate: 伪造前次崩溃残留 wake.sock → 新 bridge 启动探测无 listener → unlink + rebind 成功；nudge 后 ≤100ms 触发扫描
- [ ] 4.3 Poller 线程（1s fallback）扫描 `state='queued'` + `delivery_state IN ('pending','delivery_failed')`
    - Validate: UDS listener 完全禁用 → queued 任务 ≤1s 被 launch；pending delivery ≤1s 被补投
- [ ] 4.4 Supervisor launch 路径：CAS claim `queued→launching` → 成功则 spawn `task-runner` wrapper（传 `--task-id`, `--db-path`, `--tasks-dir`, `--runner-token=<uuid4>` 并注入 env `BG_TASK_TOKEN`）→ 立即返回
    - Validate: 两个 supervisor 并发尝试同一 queued 行，只有一个 spawn wrapper；CAS 失败的一侧跳过不报错
- [ ] 4.5 Delivery watcher 线程 —— 4 状态 outbox（`pending → enqueued → sent`，失败路径 `delivery_failed`）
    - 扫 `bg_runs.delivery_state='pending'` → 读 manifest → 构造合成 turn → `enqueue_turn(..., kind='bg_task_completion')` 成功 → UPDATE `delivery_state='enqueued', enqueued_at=now`
    - ChatTaskQueue 处理到该合成 turn 并投递飞书 API 成功 → UPDATE `delivery_state='sent', sent_at=now`
    - enqueue_turn 失败 / 飞书 API 失败 → UPDATE `delivery_state='delivery_failed', delivery_attempt_count += 1, delivery_error=<msg>`
    - **Stuck `enqueued` 扫描**：每次 poller tick 扫 `delivery_state='enqueued' AND enqueued_at < now - 5*60*1000` → 回滚 `pending` 重试（ChatTaskQueue consumer 可能因 bridge crash 未调 ack）
    - Validate: mock `lark_client.send` 失败 → `delivery_failed`，1s 后 poller 再试；attempt_count 达到 10 后停止重试并日志 ERROR；session 不可 resume → fallback 新 session + `session_resume_status='fresh_fallback'`；人工停 ChatTaskQueue consumer 模拟 stuck_enqueued → 5min 后自动 rollback pending

## 5. Completion 链路与 `_on_message` 重构

- [x] 5.1 重构 `feishu_bridge/main.py::_on_message` —— 抽取 enqueue 逻辑为 `enqueue_turn(chat_id, session_id, prompt, kind)`
    - Validate: 现有 `tests/test_*.py` 全部通过；`enqueue_turn` 对 `kind='human'` 行为与重构前 bit-identical（用 golden test 录入队快照）
    - Implementation (Option C — narrow core + extras): `FeishuBot.enqueue_turn(*, chat_id, session_key, prompt, kind, extras=None) -> (status, item)`。base item 内置 18 个字段默认值，`extras` 按 kind 合并。`_on_message` 人类分支改为 `self.enqueue_turn(kind='human', extras={thread_id, parent_id, message_id, sender_id, image_key, file_key, file_name, _todo_task_id, _card_message_id, _merge_forward_message_id, _feishu_urls})`。
    - Golden test `test_enqueue_turn_human_bit_identical_golden` in test_bridge.py — 冻结 19-field item dict，refactor 前后均通过。
    - 全回归：186 test_bridge.py + 33 test_task_runner.py + 22 test_bg_supervisor.py = 241 passed。
    - Code Review (Claude code-reviewer): APPROVE with warnings. MEDIUM #1 (extras 可覆盖 infra keys) 已应用 `_PROTECTED={bot_id, _cost_store, _quota_poller, _ledger, _queue_key}` guard + 回归测试。MEDIUM #2 (5.3 bypass 需 queue 层 API 改动) 已 forward-note 到 5.3 checkbox。Codex 跨模型评审本轮跳过（refactor bit-identical, golden 已兜底，risk 面小于 Section 4）。
- [x] 5.2 合成 turn 构造器：按 design.md §Synthetic Turn Format 拼 prompt；16KB 硬上限；**确定性 4 步截断顺序**
    - 步骤 1：stdout_tail / stderr_tail 各截到 1024B（UTF-8 boundary-safe）
    - 步骤 2：output_paths 保留 top 5（按字典序）
    - 步骤 3：on_done_prompt 截到剩余预算
    - 步骤 4：始终保留 `[bg-task:{id}]` 前缀 + manifest path line + state/reason/signal/duration/exit_code；这些不可被截
    - Validate: 构造各字段均超长的 fixture，断言截断顺序与最终大小 ≤16KB；`[bg-task:id]` 和 manifest path 在任何输入下均存在；多字节字符跨边界不破坏
    - 实装：新增 `feishu_bridge/bg_synthetic_turn.py`（纯函数 `build_synthetic_turn(...)`）+ 23 个 fixture-driven tests。
    - Code Review (Claude code-reviewer): 发现 MAJOR — `remaining<=0` fallback 分支用 raw byte slice 同时破坏 UTF-8 safety 与 step-4 preservation（长 `reason` → prelude 被从尾部切掉 State/Reason/Signal/Duration/Exit_code 行，并可能产生 mojibake）。已应用修复：对 `reason` (2048B)、`manifest_path` (1024B)、`signal` (64B) 在进入 prelude 前做 UTF-8 safe per-field cap；`remaining<=0` 分支转为 `assert`（step-4 pre-pass 已使其结构上不可达）。新增 4 个 M1 回归测试：长 reason 保 step-4 字段、多字节 reason 不破坏、短 reason 原样、超长 signal 被截断。tail boundary 测试 parametrize 至 5 个 offset 覆盖 emoji 所有切位。
    - 回归：全量 186 + 33 + 22 + 23 = 264 passed。Codex 跨模型评审本轮跳过（sandbox 阻塞了 codex exec 的所有 stdout 重定向路径；M1 已通过 Claude review 发现并修复 + 回归测试补全）。
- [x] 5.3 `enqueue_turn` 对 `kind='bg_task_completion'` 绕过 `MAX_PENDING_PER_SESSION=10`；人类消息仍受限
    - Validate: 同 session 预置 10 pending 人类消息 → 新的 bg completion 仍能入队；反之预置 1 bg completion + 10 人类 → 下一条人类消息被 backpressure
    - **Prerequisite (from 5.1 review)**: `ChatTaskQueue.enqueue` 目前无条件执行 `MAX_PENDING_PER_SESSION` 检查并抛 `SessionQueueFull`。bypass 需在 queue 层加参数（如 `bypass_backpressure: bool = False`）或新增 `enqueue_bypass()` 方法；`enqueue_turn` 里单独判断 kind 无法绕过。
    - 实装：`ChatTaskQueue.enqueue()` 新增 keyword-only `bypass_backpressure: bool = False`。cap 检查改为 `if not bypass_backpressure and pending and len(pending) >= MAX_PENDING_PER_SESSION`。`enqueue_turn` 传 `bypass_backpressure=(kind == "bg_task_completion")`，严格字符串匹配（'bg-task-completion' 这类拼写不绕过）。
    - 新增 7 个 tests：3 个 enqueue_turn kind→bypass 映射（human=False / bg_task_completion=True / 未知 kind=False 防拼写）+ 4 个真 ChatTaskQueue 行为（默认 cap 强制 / bypass 跳过 cap / bypass 不泄漏到后续 human / 空 session bypass 仍走 immediate）。
    - 回归：193 bridge + 33 task_runner + 22 bg_supervisor + 23 bg_synthetic_turn = 271 passed。
    - Codex 跨模型评审本轮跳过（change 面小、有 4 个真-queue 行为测试兜底、API shape 与 5.1 review 的建议一致）。
- [~] 5.4 Session resume fallback —— probe 契约
    - `sessions_index`（in-memory dict + `~/.feishu-bridge/sessions.json` 持久化）记录 `{session_id: {last_active_at, chat_id}}`；bridge 每次处理 human turn 更新 last_active_at
    - enqueue_turn 发现 `now - last_active_at > 24h` → 启动 sentinel probe：`claude -p --resume <session_id> -p ":probe:"` 5s timeout；成功 → 正常 resume；失败（timeout/session not found） → fork 新 session + prepend `[NOTE: original session no longer resumable at <timestamp>, resuming in fresh context]` 到 prompt
    - 记录 `bg_runs.session_resume_status ∈ {'resumed', 'fresh_fallback', 'resume_failed'}`
    - Validate: 模拟 session compact / `/new` / 15min 过期 → probe 失败 → `fresh_fallback` 生效且用户仍看到结果；session 存活 → `resumed`；probe 超时 → `resume_failed` 并 fallback
    - [x] 5.4a 基础模块 + 脚手架（本次 commit，不改 worker/watcher）
        - 实装：`feishu_bridge/session_resume.py` — `SessionsIndex`（threading.Lock + atomic tempfile+os.replace JSON 持久化）、`sentinel_probe(session_id, *, timeout_sec)` 按 design.md §Session Resume Fallback 的 5 种 outcome 分类（probe_ok / probe_timeout / session_not_found / probe_error / claude_not_found）、`resolve_resume_status(session_id, index, now_ms, probe_fn)` 纯策略、`build_fresh_fallback_prefix(reason)` 用 design.md line 419 的 verbatim NOTE 模板。Claude UUID 作为 key（`--resume` 消费对象），不用 bridge 的 session_key（bot:chat:thread）。
        - Scaffolding：`enqueue_turn(..., session_id=None)` 可选 keyword 参数，item dict 新增 `_bg_session_id`；human path 默认 None，5.4b delivery watcher 会填 Claude UUID。`_bg_session_id` 加入 extras protected keys 防止未来调用方静默覆盖。
        - 新增 26 个 tests：24 个 session_resume（SessionsIndex 持久化+并发 10 线程×20 sessions+损坏 JSON 恢复、4 种 probe 分类、resolve 6 种策略分支含 clock-rollback 未来时间戳兜底、probe 抛异常兜底为 resume_failed）+ 2 个 bridge（`_bg_session_id` 正向 round-trip + None 默认）。
        - 修复 code-reviewer 3 个 MAJOR：M1 `resolve_resume_status` 包 try/except 兜底 probe_fn 异常；M2 recency 检查要求 `0 <= age < threshold`（clock rollback 时 fail-closed 走 probe）；M3 补 `_bg_session_id` 到 protected keys 测试 + 新增 round-trip 测试。
        - 回归：558 unit passed（24 session_resume + 197 bridge + 其余）。1 pre-existing failure（`test_footer_no_model_no_workspace`）与本次无关。
    - [~] 5.4b 集成进 worker + delivery watcher（拆两段 commit）
        - [x] 5.4b-worker：worker post-turn `touch()` 接入（本次 commit）
            - `FeishuBot.__init__` 实例化 `SessionsIndex(~/.feishu-bridge/sessions.json)` 赋 `self._sessions_index`；ctor 失败降级 None 不阻塞启动
            - `enqueue_turn` item dict 加 `_sessions_index`（和 `_cost_store` / `_ledger` 同模式）；加入 extras protected keys
            - `worker.py:884` `session_map.put()` 之后立即调 `sessions_index.touch(session_id=effective_sid, chat_id=chat_id, now_ms=int(time.time()*1000))`；gated `not result.get("is_error")` 防止失败 turn 污染索引；外层 try/except 吞 touch 异常（disk full / JSON 竞争写不阻塞用户回复）
            - 新增 4 个 worker 测试：成功 turn 触发 touch（UUID+chat+epoch）/ 失败 turn 跳过 touch / 缺失 `_sessions_index` 不崩 / touch 抛异常不阻塞 cost_store 写入
            - 更新 HUMAN_TURN_GOLDEN + protected keys 测试增补 `_sessions_index`
            - 回归：562 unit passed（+4 vs 5.4a），golden snapshot 过
            - 修复 Codex cross-review MAJOR：`_on_card_action` 原本 hand-build item dict 绕过 `enqueue_turn` choke point，所有新增 infra 字段（`_sessions_index` / `_bg_session_id` / …）静默丢失。重构为调用 `self.enqueue_turn(chat_id=chat_id, session_key=msg_key, prompt=label, kind="human", extras={"sender_id": sender_id})`，消除根因而非点修补。新增 `test_on_card_action_threads_sessions_index_via_enqueue_turn` 回归测试。
            - 最终回归：563 unit passed（+5 vs 5.4a）
        - [ ] 5.4b-watcher：delivery watcher 集成（和 §4.5 合并 commit）
            - watcher 扫 `bg_runs.delivery_state='pending'` 时调 `resolve_resume_status()`
            - `session_id` 经 `enqueue_turn(..., session_id=...)` 传到 item
            - fresh_fallback 时 prepend NOTE 到 prompt 首行
            - status/reason 写入 `bg_runs.session_resume_status`

## 6. Startup reconciler

- [ ] 6.1 `BgSupervisor.reconcile()` —— bridge 启动时执行（`main.py` 主循环 before `ws_client.start()`）；顺序 6 步（integrity → stale launching → running 判活 → queued 续推 → delivery outbox → manifest-only 补写）
    - Validate: 启动日志含 `reconcile: {queued_relaunch, launching_reaped, running_attached, running_orphaned, delivery_replayed, manifest_recovered}`
- [ ] 6.2 Stale launching 回收：`UPDATE state='failed', reason='launch_interrupted' WHERE state='launching' AND claimed_at < now - 30_000`
    - Validate: 预置一个 `claimed_at=now-60s` 的 launching 行 → 启动后 state='failed'
- [ ] 6.3 Running 任务活性判断 —— 两套三元组（wrapper 身份 + child 身份）
    - wrapper 身份：`wrapper_pid + wrapper_start_time_us + runner_token`（env 比对通过 `ps eww` 或 `libproc.proc_pidinfo` + 进程 env）
    - child 身份：`pid + process_start_time_us + runner_token`（same env 验证）
    - 分支：
        - wrapper alive → 跳过（wrapper 自行收尾）
        - wrapper dead + bg_runs 无 pid（Phase S 中 crash） → 用 runner_token 在 `ps eww` 扫描 BG_TASK_TOKEN 匹配的孤儿 → 找到 → bridge 主动 reap；没找到 → `orphan, reason='wrapper_died_pre_register'`
        - wrapper dead + child alive + 三元组匹配 → **bridge 主动接管**：检查 `cancel_requested_at`/`timeout_seconds`，满足则 `killpg(pgid, SIGTERM)` 500ms poll 5s grace 后 SIGKILL；不满足则 spawn reaper 线程 kqueue 监听子退出 → 退出后写终态 + `reason='reaped_by_bridge_after_wrapper_death'` 或 `'completed_after_wrapper_death'`
        - wrapper dead + child dead + manifest 存在 → 从 manifest 更新终态 + `delivery_state='pending'`
        - wrapper dead + child dead + 无 manifest → `orphan, reason='wrapper_and_child_both_died'`
    - Validate: 5 种分支各有 fixture；pid reuse 场景：kill wrapper + 快速 fork 同 pid 的无关进程 → 三元组 mismatch → 不发任何信号 + 标 orphan；bridge reap 路径断言 `killpg` 被调用，最终 state 为 cancelled 或 timeout
- [ ] 6.4 Delivery outbox 补投：扫 `delivery_state IN ('pending','delivery_failed')` → 重新 enqueue 合成 turn；`delivery_attempt_count ≥ 10` 视为放弃 + 日志 ERROR
    - Validate: 预置 2 pending + 1 delivery_failed + 1 attempt_count=10 → 启动后补投 3 + 放弃 1
- [ ] 6.5 Manifest-only 回填：扫 `tasks/completed/*/task.json.done` 对应 `bg_tasks` 行缺失或 `bg_runs` 行缺失 → 回填；`delivery_state='pending'`
    - Validate: 删除 DB 行保留 manifest → 启动后 DB 行重建 + delivery 被触发
- [ ] 6.6 Archive cleanup + quarantine retention
    - Archive policy：completed > 7 天 → `_archive/<yyyy-mm>/<task_id>.tar.gz`；archive > 90 天 → 删除 + DELETE DB 行
    - **并发保护**：cleanup 走 `BEGIN IMMEDIATE` + predicate `delivery_state='sent' OR (delivery_state='delivery_failed' AND delivery_attempt_count >= 10)`；不删 retry 进行中的行
    - **Quarantine retention**：`~/.feishu-bridge/quarantine/bg_tasks-*.db` 保留最近 3 份或 30 天，取更晚的；超出则按 mtime 升序删除
    - Validate: cleanup 幂等；archive 目录不阻止新 task launch；cascade 删除 DB 行不误伤未完成任务；retry 进行中（`delivery_state='pending'` 或 `'enqueued'`）的 completed 行不被 cleanup 删；quarantine 超 3 份时最旧被删

## 7. 测试

- [ ] 7.1 单元测试 `tests/test_bg_tasks_db.py`：schema + 状态机 + repo CRUD + 并发写 + CAS claim + integrity_check + manifest replay
    - Validate: pytest 跑通；覆盖率 ≥80%；CAS claim 并发测试用真 sqlite3 connection（不 mock）
- [ ] 7.2 单元测试 `tests/test_task_runner.py`：wrapper 启动序列、cancel 信号转发、timeout monotonic clock、tail UTF-8 safe 截断、manifest atomic rename
    - Validate: 覆盖所有终态；monotonic clock 测试用 `freezegun` 或 monotonic mock；UTF-8 fixture 含 emoji 跨边界
- [ ] 7.3 单元测试 `tests/test_bg_supervisor.py`：launcher CAS / delivery watcher retry / wake listener bind 回退 / session fallback
    - Validate: 覆盖 delivery retry 上限；session 不可 resume fallback 路径
- [ ] 7.4 集成测试 `tests/test_bg_tasks_e2e.py`：启动 bridge fixture → CLI enqueue → 真实 `sleep 1` 经 task-runner → 验证合成 turn 出现在 ChatTaskQueue
    - Validate: 整条链路 <5s 完成；wrapper 是真进程（非 mock）；ChatTaskQueue assert 用 spy
- [ ] 7.5 **Crash barrier 集成测试** `tests/test_bg_crash_windows.py`（关键新增）：
    - [ ] `barrier=post_claim` crash → 重启后 launching 超时 → `failed, reason='launch_interrupted'`
    - [ ] `barrier=post_spawn_pre_register`（R3 新增）kill wrapper 在 Phase S 单事务前 → child 已跑但 bg_runs.pid 为 NULL → 重启后用 runner_token 扫 `ps eww` → 找到则 reap，找不到则 `orphan, reason='wrapper_died_pre_register'`
    - [ ] `barrier=post_spawn` kill bridge → wrapper 继续跑完 → 重启 bridge → delivery 补投
    - [ ] `barrier=pre_rename` crash → 重启后 `orphan`
    - [ ] `barrier=post_rename_pre_db` crash → 重启后从 manifest replay（Phase C 单事务未提交 → 仍 running + manifest 存在）
    - [ ] `barrier=post_db_pre_enqueue` crash → 重启后 delivery outbox 补投
    - [ ] `barrier=post_enqueue_send_fail` → `delivery_failed`, poller 重试
    - [ ] `barrier=stuck_enqueued`（R3 新增）模拟 ChatTaskQueue consumer 吞掉 ack → delivery_state 卡 enqueued → 5min 后 poller 扫描回滚 pending + 重试 → 最终 sent
    - [ ] `barrier=orphan_alive_bridge_reap`（R3 新增）SIGKILL wrapper 但保留 child 存活 → 设 cancel_requested_at → bridge reconciler 三元组验证 → `killpg(pgid, SIGTERM)` → 5s grace SIGKILL → 终态 cancelled + `reason='reaped_by_bridge_after_wrapper_death'`
    - Validate: 每个 barrier 都是真 kill（非 mock）；最终状态由 CLI `bg status` 断言，不看内部 state；新 barrier 验证 R3 所解决的 R2 评审 finding B1/B2
- [ ] 7.6 **pid reuse 场景** `tests/test_pid_reuse.py`：kill wrapper → fork 无关进程占用相同 pid → 三元组 mismatch → reconciler 不发信号 + 标 orphan
    - Validate: 断言"从未调用 os.killpg"（spy）；最终 state='orphan'
- [ ] 7.7 **DB 损坏恢复** `tests/test_db_recovery.py`：truncate bg_tasks.db 前 100 字节 → 启动 → quarantine + manifest replay
    - Validate: quarantined 文件保留；新 DB 行数 == manifest 数；日志含 `integrity_check failed`
- [ ] 7.8 **SQLite BUSY 抗压** `tests/test_sqlite_busy.py`：CLI 开 transaction 持有 writer lock 4s → bridge 并发 enqueue → busy_timeout 后成功
    - Validate: enqueue 5s 内返回；不吞错
- [ ] 7.9 **Cancel 真路径** `tests/test_cancel.py`：queued cancel 立即 `cancelled`；running cancel 触发真 SIGTERM 5s grace 真 SIGKILL；≤10s 终态
    - Validate: 信号通过 wrapper 转发到 user command；pgid 正确；SLO 用 monotonic 断言
- [ ] 7.10 **回归**：运行现有 `tests/` 全部测试
    - Validate: 零失败零新跳过；`enqueue_turn` 对人类消息的 golden 断言通过

## 8. 文档与发布

- [ ] 8.1 更新 `README.md`：新增 `bg` 子命令使用说明 + manifest 格式示例 + argv 用法（`--` 分隔）
    - Validate: 包含最小例子 `feishu-bridge-cli bg enqueue --chat-id oc_xxx --on-done-prompt "done" -- sleep 10`；说明 bridge 崩溃不影响已 launch 任务完成
- [ ] 8.2 更新 `CLAUDE.md`（项目级）：何时用 bg-task（任务预计 >90s 且需要汇报）vs fire-and-forget；argv 必须通过 `--` 分隔；安全说明 `shell=False` 强制
    - Validate: 使用场景判断规则可执行（例子形式而非描述）
- [ ] 8.3 migration 说明：现有用户升级到本版本 bridge 首次启动时会自动创建 `~/.feishu-bridge/`；`bg_tasks.db` 空库创建；已存在目录不被覆盖
    - Validate: 现有用户目录存在 → 不覆盖；空目录 → 创建预期文件树

## Spec-Check

### Plan Review Round 1 (2026-04-18)
- verdict: **Block / rework-and-resubmit**
- findings: 1 CRITICAL + 5 HIGH + 5 MEDIUM + 4 LOW
- decision: Captain 选 Path A —— 引入 task-runner wrapper 独立进程
- result: BLOCK

### Plan Review Round 2 (2026-04-18)
- verdict: **fix-then-proceed（不需额外评审轮次）**
- findings: 4 HIGH (B1-B4) + 5 MEDIUM (B5-B9) + 3 LOW (B10-B12)
- Round 3 修复涵盖：
    - B1 post-spawn-pre-register 窗口 → wrapper Phase P/S 拆分 + 单事务 UPDATE pid
    - B2 wrapper-dead-child-alive → bridge 主动 reap + 尊重 cancel/timeout
    - B3 delivered-before-send 语义 → 4 状态 outbox (pending/enqueued/sent)
    - B4 两事务不一致 → Phase C 单事务
    - B5 process_start_time 精度 → `libproc.proc_pidinfo` μs
    - B6 wrapper 身份未持久化 → `wrapper_start_time_us` 列
    - B7 session resume 检测 → `sessions_index` + 5s sentinel probe
    - B8 archive cleanup race → `BEGIN IMMEDIATE` + predicate
    - B9 截断顺序 → 4 步确定性
    - B10 cancel poll 500ms → wrapper Phase W 明确
    - B11 quarantine retention → 3 份或 30 天
    - B12 runner_token 定性 → 非秘密 nonce，删除单独文件
- result: WARN

### Implementation Round 1 — Section 1 数据层 (2026-04-18)
- scope: Tasks 1.1–1.4 (`feishu_bridge/bg_tasks_db.py` + `tests/unit/test_bg_tasks_db.py`)
- code-reviewer 首轮结论: **BLOCK** — 5 blocking + 6 should-fix + 6 nit
- Round 1 修复（blocking only，SHOULD-FIX 留作 Section 2 并推或本轮收尾决定）：
    - B1 `finish_run` SQL 放行 launching→completed/cancelled/... 与 `_ALLOWED` 不符 → 改为 tx 内 SELECT 决定 legal_from_clause，仅允许 launching→failed 捷径
    - B2 running + cancel_requested 遇 `finish_run(completed)` 被忽略 → tx 内读 `cancel_requested_at`，将 completed 强制改为 cancelled，保留用户意图
    - B3 `rebuild_from_manifests` active-dir 未检查 `task.json.done` → 先查 manifest；存在则 replay + `mv active→completed`，缺失才标 orphan
    - B4 `check_same_thread=False` 与 single shared conn 不安全 → 移除参数，docstring 明确要求每线程自建连接
    - B5 manifest input 未校验 → 新增 `_is_trusted_task_dir()` 拒绝非 uuid 目录 / symlink；`_replay_completed_manifest` 强制 `task_id == dir_name`、`schema_version ∈ [1,2]`、`chat_id/session_id` 必须 string
- 新增 regression tests: `test_finish_run_rejects_launching_to_completed`、`test_finish_run_launching_to_failed_allowed`、`test_finish_run_coerces_completed_to_cancelled_on_cancel_request`、`test_finish_run_does_not_coerce_failed_on_cancel_request`、`test_rebuild_from_manifests_replays_active_with_committed_done`、`test_rebuild_from_manifests_rejects_mismatched_task_id`、`test_rebuild_from_manifests_rejects_non_uuid_dir`、`test_rebuild_from_manifests_rejects_future_schema_version`、`test_rebuild_from_manifests_rejects_symlink_dir`
- tests: 52 passed in 0.21s (zero warnings with `-W error::pytest.PytestUnhandledThreadExceptionWarning`)
- SHOULD-FIX 延迟项（后续 section 落地前处理）：
    - S1 `validate_transition` docstring 与行为不符（识别出应修 docstring，未改代码）
    - S2 `mark_delivery_state` 缺 adjacency 检查 → Section 4 delivery watcher 落地时一起改
    - S3 integrity-check 文件锁 → Section 6 reconciler 落地时统一加 recovery lockfile
    - S4 manifest `schema_version` 已校验上限，字段缺失不报错符合向后兼容意图
    - S5/S6 tx rollback / terminal CAS 测试 → 下一轮 test-harden batch
- result: WARN

### Code Review Round 1 — Section 2 task-runner (2026-04-18)
- scope: Tasks 2.1–2.4 (`feishu_bridge/task_runner.py` + `tests/unit/test_task_runner.py` + pyproject.toml console_script `task-runner`)
- code-reviewer 首轮结论: **PASS-WITH-EDITS** — 0 blocking + 2 should-fix + 4 nit
- 实施前修复（本轮落地）：
    - `BgTaskRow` 字段双重 `json.loads()` 误用（`command_argv` / `env_overlay` / `output_paths` 四处），已改为直接消费 dataclass 已解析字段
    - `terminate_pgid` probe 只处理 `ProcessLookupError`，macOS 下 group-leader reap 后返回 EPERM → 加入 `PermissionError` 同义处理
- Round 1 修复（应对 code-reviewer）：
    - S1 error-path 覆盖太薄 → 新增 6 个测试：`test_main_non_zero_exit_produces_failed_state`、`test_main_timeout_produces_timeout_state`、`test_main_rejects_task_not_in_launching_state`、`test_main_rejects_invalid_task_id`、`test_main_rejects_invalid_runner_token`、`test_phase_s_logs_redact_argv`
    - S2 `phase_s` INFO 日志含完整 argv 可能泄漏秘密 → INFO 只输出 `argv[0] + len(argv)`，全 argv 降到 DEBUG；`test_phase_s_logs_redact_argv` 通过 sentinel 秘密保证未来 log 改动不回归
- 延迟项 (nit，不阻塞本 section)：
    - N1 manifest tmp 文件名 `task.json.done.tmp` vs 设计 `task.json.tmp`，语义等价（atomic write temp→rename），不改
    - N2 tasks.md 原文 Phase W 写 kqueue `EVFILT_PROC`，实施用 `Popen.wait(timeout=0.5)` polling → 已更新 tasks.md 行 22 文案
    - N3 `_WrapperState.conn` + `proc.stdout/stderr` 未显式 close，进程退出时自然释放；短生命期 wrapper 保留当前形式
    - N4 Codex 跨模型评审未运行（工具超出本轮预算），留作 Section 3/4 时集中补
- tests: 33 passed (task_runner) + 52 passed (bg_tasks_db regression) = 85 passed
- result: PASS-WITH-EDITS

### Code Review Round 2 — Section 2 Codex 跨模型评审 (2026-04-18)
- scope: 同 Round 1（task_runner.py + test_task_runner.py），补做 Codex CLI cross-model review
- Codex 结论: 3 个新 BUG（均为 FD/zombie leak 风险，Claude 评审未命中）
- 修复：
    - C1 (task_runner.py phase_s OSError 路径) → `read_proc_start_time_us()` 抛错时先 `terminate_pgid` 再 `raise`，但未 `close()` stdout/stderr 管道 / `wait()` reap child。新增 `_cleanup_child_io()` helper（close pipes → join collectors → wait proc），OSError 分支调用 `_cleanup_child_io(state.child_proc, None, None)`（collectors 此时未启动）
    - C2 (task_runner.py phase_s `_AttachRace` 分支) → 同样 kill pgid 后未清理 FD / reap。调用 `_cleanup_child_io(state.child_proc, state.stdout_collector, state.stderr_collector)`
    - C3 (task_runner.py `main()` 入口) → `phase_s` 抛出非 OSError/AttachRace 异常时 `return 1` 无 cleanup。将 phase_p/phase_s/phase_w/phase_c 整体包进 try/finally，finally 里统一 `_cleanup_child_io(...)` + `state.conn.close()`
- 为什么 Claude 未命中：Claude reviewer 关注 SQL/事务/秘密/边界校验等语义层；Codex 更擅长扫资源生命周期与错误路径清理缺口，两侧互补
- 回归：tests 仍 85 passed（未新增测试，因 FD leak 在短生命 wrapper 中不易直接观测；等 Section 6 reconciler 引入 long-running 场景再补 stress test）
- 延迟项：无新增 nit
- result: PASS

### Code Review Round 1 — Section 3 CLI (2026-04-18)
- scope: Tasks 3.1–3.4 (`feishu_bridge/cli.py` bg subcommand group + `tests/unit/test_cli_bg.py` 17 tests)
- code-reviewer 结论: **PASS-WITH-EDITS** — 0 blocking + 2 should-fix + 3 nit
- diff basis: HEAD (dirty — working tree has cli.py + test_cli_bg.py unstaged)
- Validate 对齐:
    - 3.1 bg 子命令组齐备（enqueue/status/list/cancel），argparse 自动提供 --help（未写独立测试，属 argparse 自带行为，不视为覆盖缺口）
    - 3.2 `--cmd-json` vs 位置 argv 二选一；bare string 被 rc=2 拒绝；缺 `--on-done-prompt` 被 argparse rc=2 拒绝；默认 `--timeout-seconds=1800`；无 bridge listener 时 enqueue 成功
    - 3.3 status 未知 task → rc=1；list 按 updated_at DESC + chat/state 过滤；cancel 已终态 → rc=1 且 state 不变；cancel 仅改 `cancel_requested_at`
    - 3.4 wake.sock 不存在 → 静默跳过；listener 在线时 `\x01` payload 送达
- Should-fix 已修:
    - S1 cancel 路径丢弃 `set_cancel_requested()` 返回值 → 若 DB WHERE 因 TOCTOU 未命中（row 在 get→UPDATE 间转入 terminal），CLI 仍输出 `cancel_requested: True` 误导上游。已改为读取 bool 返回值，False 时 rc=1 + error "task transitioned to terminal state during cancel; no flag set"
    - S2 `_parse_env_kv` 错误消息用 `{s!r}` 回显原始值 → 用户 fat-finger `--env API_KEY_abc123`（漏 `=`）会把 secret 泄漏到 stderr / shell history。已改为 `got value of length {len(s)}`；空 key 分支同样改为不回显。与 Section 2 Round 1 的 argv-redaction 修复同源
- Nit（不阻塞，未修）:
    - N1 "DB locked → 非零退出 + 明确错误"（Validate 3.2）有代码分支（sqlite3.OperationalError → rc=1）但无测试；构造 locked DB 需要额外 fixture，留作后续 Section 7 stress harden 再补
    - N2 `--cmd-json` schema 校验代码覆盖 non-list / 空列表 / 非 string 元素三种情况（`_run_bg_command` 行 315-320），但只有 happy path 有测试；非关键路径，延后补
- 回归: `uv run pytest tests/unit/test_cli_bg.py -q` → 17 passed in 1.96s（fix 前后一致）；未运行完整 tests/ 套件，因本次改动仅限 bg CLI 路径
- result: PASS-WITH-EDITS

### Code Review Round 2 — Section 3 Codex 跨模型评审 (2026-04-18)
- scope: 同 Round 1（cli.py bg 子命令 + test_cli_bg.py），在 main-shell 用 `acpx codex exec` 跑 Codex CLI 跨模型评审（code-reviewer agent 因 sandbox output-redirect 限制未能完成，由主会话补做）
- Codex 结论: **FIX-BEFORE-COMMIT** — 2 BUG + 2 WARN（全部 Claude Round 1 未覆盖）
- 修复（全部本轮落地）：
    - D1 (BUG) `bg list --limit` 接受负值/0/过大值 → SQLite 把 `LIMIT -1` 当无限制，用户可意外 dump 所有 row（含 `env_overlay` / `command_argv` / `on_done_prompt` 可能秘密）。与 Section 2 argv-redaction / Section 3 `_parse_env_kv` 同源的数据泄漏面。新增 `_positive_int(max_value)` argparse type，cap=200；对应 3 个测试：`test_list_limit_{negative,zero,exceeds_cap}_rejected`
    - D2 (BUG) `--timeout-seconds` 接受 0/负值 → runner 侧 `int(timeout_seconds or 1800)` 把 0 静默改为默认 1800；负值立刻超时。cap=86400（24h）；对应 3 个测试：`test_enqueue_timeout_{zero,negative,exceeds_cap}_rejected`
    - D3 (WARN) status/list/cancel 的 `repo.get()/list()/set_cancel_requested()` 未包 `sqlite3.Error` → DB 损坏 / IO 错误时裸 traceback 冒出，与 enqueue 的 JSON-stderr 约定不一致。新增 `_bg_db_json_error(label, exc)` helper + 三子命令在 `_open_repo()` 和 repo 调用处 double-wrap；新增 3 测试 `test_{status,list,cancel}_missing_db_returns_json_error` 覆盖 DB 缺失的常见触发
    - D4 (WARN) 校验顺序 → enqueue 的 argv/env/json 校验失败前已经 `_bg_ensure_home() + init_db(db_path)`，非法命令也会在磁盘留下持久化状态。拆出 `_ensure_db()` 内部 helper，只在 enqueue 全部参数校验通过后才执行；status/list/cancel 改为"DB 不存在 → JSON error + rc=1"。`test_enqueue_timeout_zero_rejected` 附加断言 `bg_tasks.db` 未被创建
- 为什么 Claude 未命中：Claude reviewer 停在"代码逻辑对 argparse 值 OK"；Codex 继续问"这个值被传给 SQLite / runner 时运行时怎么解释"——D1 的 `LIMIT -1` 和 D2 的 `timeout=0` 都是系统层语义陷阱。与 Section 2 Round 2 的 FD leak 属同类：Claude 关注语义，Codex 关注运行时解释层
- 回归：`uv run pytest tests/unit/test_cli_bg.py -q` → 26 passed in 2.66s（17 原测试 + 9 新测试覆盖 D1-D4）
- 延迟项：无新增 nit
- result: PASS

