---
branch: master
start-sha: b031cebdb18b0c47c7cabcf3578fd73701157113
status: active
scope: SINGLE
---

# Proposal: feishu-bridge-bg-tasks

## WHY

Claude 在 turn 内启动 3-15 分钟后台任务（benchmark / research / batch analysis）后，因 bridge 的 request-response 模型无法在完成时自动向用户汇报，用户必须手动 ping 才能推进。很多长任务因此脱离 AI 协作闭环，产生"启动了但忘了看"的工作丢失模式。

本变更让 bridge 支持"Claude 发起 → 任务后台执行 → 完成时 bot 主动推送分析结果"的完整链路。

## WHAT

- 新增 CLI 子命令：`feishu-bridge-cli bg {enqueue|status|list|cancel}`
- 新增 `task-runner` wrapper 二进制（console_script，与 bridge 同包）——独立进程，真正 own 子进程生命周期，负责 `wait()` + 写 manifest + 更新 DB；bridge 崩溃/重启不影响它的收尾
- bridge 侧新增：
  - SQLite 两表 `bg_tasks` + `bg_runs`（含 `launching` 瞬态 + runner_token / process_start_time 身份三元组 + delivery outbox 列）
  - UDS wake socket（`~/.feishu-bridge/wake.sock`）——CLI INSERT 后单字节 nudge；wrapper 完成时也 nudge（delivery ready）
  - Wake listener 线程 + 1s poller，扫 `queued`（用 SQL CAS claim）+ `delivery_state='pending'`（outbox 重投）
  - Supervisor（只 spawn wrapper，不直接管 user command）
  - Delivery watcher（独立于 supervisor，crash-safe outbox pattern）
  - Startup reconciler（`PRAGMA integrity_check` → 损坏时从 manifest replay；stale `launching` 回收；三元组身份判活；manifest-only 行补写；pending delivery 补投）
- Completion 路径：wrapper 退出时原子写 `task.json.done` → `UPDATE bg_runs.delivery_state='pending'` → bridge delivery watcher 读 manifest → `enqueue_turn(kind='bg_task_completion')`
- `_on_message` 重构：抽取 enqueue 逻辑为可复用入口 `enqueue_turn(chat_id, session_id, prompt, kind)`，供合成 turn 复用
- Session resume fallback：原 session 若已 compact / `/new` / 不存在 → 用 chat_id 起新 session，在 prompt 注明 `[NOTE: original session no longer resumable ...]`

## NOT

- Cron-style 定时任务（设计需**能扩展**到这个方向，但本 change 不实现）
- 多机/多 bridge 分布式执行
- Web UI / 任务 status dashboard
- 替换现有 `subprocess.Popen(run_in_background=True)` 模式（bg-task 是 opt-in 异步汇报，fire-and-forget 继续有效）
- 任务资源配额/隔离（CPU cgroup、内存限制）
- HTTP ingress（保持 WebSocket-only 安全姿态）

## Acceptance Criteria

- [ ] Claude 在一个 turn 内调用 `feishu-bridge-cli bg enqueue [flags] -- cmd args` 能**同步**获得 task_id（<2s；bridge 不运行时 CLI 仍 INSERT 成功，wake 失败静默跳过）
- [ ] 任务跑完后，用户在对应飞书群看到一条由 bot 发出的新消息，包含 Claude 对结果的分析，且用户无需任何输入
- [ ] bridge 重启后（launchd reload / crash / SIGKILL）：
  - `queued` 任务按原意图 launch（CAS claim 防双启）
  - `launching` 瞬态若 > 30s 未进入 running → 标记 `failed, reason='launch_interrupted'`
  - `running` 任务若 task-runner wrapper 存活 → wrapper 自行完成收尾（manifest + 单事务同步更新 bg_runs+bg_tasks）
  - wrapper 死（Phase S 中途） + bg_runs 已 INSERT 但 pid 未写 → 用 runner_token 在 `ps eww` 扫描存活子 → killpg 或标记 orphan
  - wrapper 死 + user command 死 + manifest 存在 → 从 manifest 更新为终态；`delivery_state='pending'` 触发合成 turn 补投
  - wrapper 死 + user command 死 + 无 manifest → `orphan, reason='wrapper_and_child_both_died'`
  - wrapper 死 + user command 仍在 → bridge reconciler 三元组验证（pid+process_start_time_us+runner_token）通过后主动接管：尊重 `cancel_requested_at` / `timeout_seconds`；否则 spawn reaper 线程 poll 子退出，退出时从 kqueue `NOTE_EXIT` 取 exit_code 写终态
  - `delivery_state='pending'` → 重新 enqueue；`enqueued` 卡超过 5 min → 回滚 `pending` 重试；`delivery_failed` → retry 计数 < 10 上限
- [ ] **DB 损坏恢复**：`PRAGMA integrity_check` 失败 → quarantine DB（保留最近 3 份或 30 天）+ 从 `task.json.done` manifest 重建 `bg_tasks`/`bg_runs` 行；`queued` 未 launch 的任务丢失在 CLI `--help` 中明确声明
- [ ] **pid reuse 防御**：身份验证用三元组（pid + `process_start_time_us` via `libproc.proc_pidinfo` + `runner_token` env）；mismatch → 不发信号，直接 orphan
- [ ] **Wrapper 身份验证**：bridge supervisor 记录 `wrapper_pid` + `wrapper_start_time_us`，reconciler 判活时同样三元组验证，防 wrapper pid reuse
- [ ] **Delivery outbox 4-state**：`not_ready → pending → enqueued → sent`（+`delivery_failed`）；wrapper Phase C 单事务把 bg_runs 置 pending 并更新 bg_tasks；bridge delivery watcher 扫 pending → 转 enqueued → 送达后 sent；发送失败 → delivery_failed + attempt_count++
- [ ] **Session resume fallback**：wrapper Phase C 前 / bridge delivery 前检查 `sessions_index`；>24h 发 5s `:probe:` sentinel；probe 失败或 session 不存在 → 用 chat_id 起新 session + `[NOTE: original session no longer resumable ...]` prefix；`bg_runs.session_resume_status` 记录 `resumed|fresh_fallback|resume_failed`
- [ ] **Archive cleanup 并发保护**：cleanup job 用 `BEGIN IMMEDIATE` + predicate（`delivery_state='sent' OR (='delivery_failed' AND attempt_count>=10)`），不删 retry 进行中的行
- [ ] **合成 turn 截断**：16KB 预算，确定性 4 步顺序：tails 截到 1024B → output_paths top 5 → on_done_prompt 截断 → 始终保留 `[bg-task:{id}]` 标签和 manifest path
- [ ] `bg status/list/cancel` 从任意 turn 调用都能正常工作；cancel 后 ≤10s 内 `state` 转为终态（wrapper 500ms poll cancel_requested_at → SIGTERM → 5s grace → SIGKILL；deadlines 用 `time.monotonic()`，持久化用 epoch μs）
- [ ] **Command argv 安全**：CLI 要求 `--` 分隔或 `--cmd-json`；禁止 bare string → shlex split 路径；wrapper 一律 `Popen(argv, shell=False)`
- [ ] **Tail 截断**：stdout/stderr 各 4096 字节上限，向前回退到 UTF-8 boundary，不破坏 multi-byte 字符
- [ ] **ChatTaskQueue overflow**：人类消息 `MAX_PENDING_PER_SESSION=10` 不变；`kind='bg_task_completion'` 绕过此限制
- [ ] **回归**：现有 `_on_message` / reaction / card action 外部行为不变；现有 `test_*.py` 全部通过；`enqueue_turn` 对 `kind='human'` 语义与重构前等价

## Approaches Considered

### Approach A: SQLite + UDS wake + task-runner wrapper + manifest outbox（Selected）
- SQLite `bg_tasks`（含 `launching` 瞬态 + 身份三元组列）+ `bg_runs`（含 delivery outbox 列），WAL + `busy_timeout=5000`
- UDS socket（`b'\x01'` ping / `b'\x02' + uuid.bytes` 指定 task / `b'\x03' + uuid.bytes` wrapper delivery-ready）
- Bridge supervisor 只 spawn `task-runner` wrapper 二进制；**wrapper 独立进程 own user command 生命周期**，负责 wait() + manifest + DB 更新；bridge 崩溃不影响收尾
- Bridge delivery watcher（独立于 supervisor）按 `delivery_state='pending'` 的 outbox pattern 投递合成 turn；crash-safe 重试
- Startup reconciler：integrity_check → stale launching 回收 → 三元组判活 → manifest replay → pending delivery 补投
- **Effort**: L+（约 800-1000 LOC 核心 含 wrapper + 300 LOC 测试）
- **Risk**: Med — 并发 SQLite / UDS / wrapper 三进程协作 / crash-window 测试矩阵
- **Pros**: 生命周期所有权清晰（wrapper=父；bridge=launcher+watcher）；crash-safe outbox；pid reuse 防御到位；可扩展 cron（加 `schedule_expr` 列）
- **Cons**: 需新增 wrapper 二进制；crash-window 测试矩阵较大；debug 需看两份日志（supervisor + task-runner）

### Approach B: 文件 spool + 轮询 + manifest（MVP）
- CLI 只写 JSON 到 `~/.feishu-bridge/tasks/queued/<uuid>.json`（无 SQLite）
- Bridge 每 1s 轮询 `queued/`，移动到 `active/`，launch subprocess
- Completion → 写 `done/<uuid>.json` manifest → 合成 turn
- Cancel：CLI 写 `cancel/<uuid>` marker，bridge 轮询看到就 SIGTERM
- **Effort**: S-M（250-350 LOC 核心 + 100 LOC 测试）
- **Risk**: Low-Med — 文件锁/原子 rename 是成熟模式
- **Pros**: 无新 IPC；代码增量小；crash 恢复靠目录扫描
- **Cons**: status/list 遍历目录不如 SQL 干净；register→launch 延迟被轮询周期限制（>0.5s）；cancel 响应慢；task metadata 散落不利于 cron 扩展

### Approach C: launchd 代管 + bridge 只做结果转发（lateral）
- Claude 用 `launchctl bootstrap` 注册临时 launchd job
- Job 完成 `exit 0` → bridge keep-alive job 订阅 → 读 manifest → 合成 turn
- **Effort**: M，但依赖 launchd 专业知识
- **Risk**: High — launchd 临时 job 语义晦涩；非 macOS 不可移植；调试难
- **Pros**: OS 原生 scheduler 免费获得持久化 + cron + resource limits
- **Cons**: 跨平台死；plist 管理独立复杂度

**Selected: A** — 用户希望解决 general 能力（不只 bench 场景），A 的 schema + UDS 分层投资在后续所有 async 任务复用；Codex 与 Claude 双模型评审均推荐方案；扩展到 cron 只需加字段不改核心。

**Plan Review Round 1 修订**（2026-04-18）：引入 task-runner wrapper 二进制解决 "bridge 崩溃后无人 wait() child" 的 CRITICAL 设计缺陷；加 `launching` 瞬态 + SQL CAS claim 防双启；delivery_state 升级为 outbox pattern；身份识别用 pid + process_start_time + runner_token 三元组；DB 损坏从 manifest replay；session resume 加 fallback。

## RISKS

| Risk | Impact | Mitigation |
|---|---|---|
| CLI + bridge + wrapper 三方并发写 SQLite | 注册/更新失败 | WAL + `busy_timeout=5000ms`；所有写透过 `BgTaskRepo` 短事务；wrapper 写 terminal state 使用 state-machine-guarded UPDATE |
| Queued→running 双启动 race | 同一 task 被两个 wrapper 同时跑 | `launching` 瞬态 + SQL CAS `UPDATE ... WHERE state='queued'`；只 claim 成功的 spawn wrapper |
| Bridge 崩溃后 child 被 init 收养 | 无人 wait()，manifest 永不写 | `task-runner` wrapper 独立进程 own user command 生命周期；bridge 崩溃 wrapper 继续跑完 |
| Bridge+wrapper 都崩而 user command 存活 | 无法 re-attach | 已知限制：轮询 user command 退出后若无 manifest → orphan；AC 明确声明此分支 |
| UDS wake 丢失（bridge 临时阻塞） | 注册后延迟 launch/delivery | 1s poller fallback；`delivery_state='pending'` outbox 保证 completion 不丢 |
| Completion 送达失败（bridge crash / send 错误） | 用户看不到结果 | `bg_runs.delivery_state='pending'` outbox pattern；startup + poller 按 `delivery_attempt_count < 10` 重试 |
| 合成 turn 穿透 ChatTaskQueue 插队 | 用户体验混乱 | 合成 turn 走同一 `enqueue_turn` 路径，FIFO 不变；仅 `MAX_PENDING_PER_SESSION` 对 `kind='bg_task_completion'` 放行（避免永久丢失） |
| chat_id 失效 / session 不可 resume | 结果无法送达或 resume 错乱 | session 不存在 → fallback 到 chat_id 起新 session；chat_id 失效 → `delivery_failed` + alert |
| pid reuse → killpg 打到无关进程 | 数据损失 / 误杀 | 身份三元组（pid + `process_start_time_us` via `libproc.proc_pidinfo` + `runner_token` env）；mismatch 永不发信号 |
| 任务 cmd 注入 / shell expansion | 任意代码执行 + shell metachar 展开 | `Popen(argv, shell=False)` 强制；CLI 拒绝 bare string，要求 `--` 或 `--cmd-json`；威胁模型：同用户 arbitrary exec 可接受（Claude 本就有同用户权限） |
| 长任务占 ChatTaskQueue slot | 同 session 后续消息排队 | 合成 turn 完成后立即释放；`bg cancel` 任何时刻可中止 |
| Manifest 写入被截断 | 状态不一致 | atomic rename（`.tmp` → `.done`）+ `os.fsync`；rename 前崩 → orphan，rename 后崩 → pending delivery 重试 |
| SQLite 文件损坏 | 全部 DB 状态丢失 | `PRAGMA integrity_check` + quarantine + 从 `task.json.done` manifest replay；`queued` pre-launch 任务丢失在 CLI --help 明示 |
| 系统睡眠 / NTP 时钟跳变扭曲 timeout | 超时逻辑失效 | 所有 live deadlines 用 `time.monotonic()`；epoch ms 只用于持久化/UI |
| Stale wake.sock 从前次崩溃遗留 | bind EADDRINUSE | 启动时 client connect 探测无响应 → `unlink()` + rebind |
| stdout/stderr tail 截断在 UTF-8 multi-byte 中间 | prompt 乱码 | 4096 字节上限 + 向前回退到 UTF-8 boundary（首字节高位 10xxxxxx 不做截断点）；prepend `...[truncated]\n` |
| `kind='scheduled'` 枚举值预留导致 cron 范围蔓延 | 公开接口承诺 | 本变更 schema `CHECK (kind = 'adhoc')` 硬约束；cron 变更时另外 schema migrate |

## Decision Log

| Date | Decision | Reason |
|------|----------|--------|
| 2026-04-18 | 选定 Approach A | Codex 交叉评审推荐；通用能力投资值得；扩展 cron 成本低 |
| 2026-04-18 | 不开 HTTP ingress | 保持 bridge WebSocket-only 姿态；单用户 macOS 不需要 HTTP |
| 2026-04-18 | CLI 不直接操作 subprocess | bridge 是唯一生命周期 owner；避免两处都能 launch/kill 造成竞态 |
| 2026-04-18 | Completion 用 atomic manifest 而非 pid liveness | pid reuse 风险；manifest 原子 rename 是可靠完成信号 |
| 2026-04-18 | 引入 task-runner wrapper 独立进程 | bridge 崩溃后被收养的 child 无法被新 bridge `wait()`；wrapper 让 manifest 写入不依赖 bridge 存活 |
| 2026-04-18 | queued→running 用 SQL CAS + `launching` 瞬态 | 防 wake listener 与 poller / launchd reload 重叠窗口双启动同一任务 |
| 2026-04-18 | `bg_runs.delivery_state` 作为 crash-safe outbox | manifest 写完 + ChatTaskQueue 入队之间的 crash window 不允许丢失 completion |
| 2026-04-18 | pid 身份验证用三元组 | macOS 无 `/proc`；`ps -o command=` 匹配 argv 不唯一；`ps -o lstart=` 秒精度不够快速 pid reuse；改用 `libproc.proc_pidinfo` μs 精度 `process_start_time_us` + `runner_token` env 提升唯一性 |
| 2026-04-18 | DB 损坏时从 manifest replay | 满足 proposal RISKS 原有承诺；`queued` pre-launch 任务接受丢失 |
| 2026-04-18 | Session resume 失败回退到新 session | 15 min 任务跑完时原 session 可能已 compact / `/new`；不能让 completion 永久卡住 |
| 2026-04-18 | timeout 用 `time.monotonic()` | macOS 笔记本频繁睡眠 + NTP 跳变让 wall clock 不可靠 |
| 2026-04-18 | 命令输入用 argv `--` 或 `--cmd-json` | 永不 shlex/shell=True；断开 meta-expansion 攻击面；Claude 同用户 exec 威胁模型已显式接受 |
| 2026-04-18 | tail 截断 4096 字节 + UTF-8 boundary | 避免 multi-byte 字符被截断导致 prompt 乱码 |
| 2026-04-18 | `kind='scheduled'` 不进本变更 schema | 保持 NOT 列承诺；cron 变更时另外 migrate，不预留未实现的公开状态 |
| 2026-04-18 | 合成 turn overflow 绕过 `MAX_PENDING_PER_SESSION` | completion 丢失 = 用户看不到结果，比人类消息 backpressure 代价高 |
| 2026-04-18 | 保留 7 天 / archive 90 天 / 之后 DELETE | 澄清 "archive 保留 7 天" vs "_archive/" 的语义歧义 |
| 2026-04-18 | Wrapper 生命周期分 P-S-W-C 四阶段（R3/B1） | Phase P 先 INSERT bg_runs（仅 wrapper 身份），Phase S 单事务 UPDATE pid+start_time+state='running'——修正 R2 评审发现的 post-spawn-pre-register 窗口（child 已跑但 DB 无记录→reconciler 无凭据回收） |
| 2026-04-18 | Bridge 主动 reap 孤儿存活子（R3/B2） | wrapper 死但 child 活时，仅标 orphan 不满足 cancel/timeout 语义；bridge reconciler 三元组验证后主动 killpg，记录 `reason='reaped_by_bridge_after_wrapper_death'` |
| 2026-04-18 | Delivery outbox 4 状态（R3/B3） | 原 `delivered` 单状态在 enqueue 成功+send 失败间语义含糊；拆 `enqueued`（入 ChatTaskQueue 成功）和 `sent`（飞书 API ack），stuck `enqueued` > 5min 回滚 `pending` 重试 |
| 2026-04-18 | Completion 单事务更新（R3/B4） | wrapper Phase C 原两个事务（先 bg_runs 后 bg_tasks）中间 crash 会留 `running` 僵尸；改为单事务同时写双表终态 |
| 2026-04-18 | `process_start_time` 改 μs 精度（R3/B5） | 原 `ps -o lstart=` 秒精度不足以区分快速重启 pid；改用 `libproc.proc_pidinfo(PROC_PIDTBSDINFO)` 获取 μs，ctypes 从 `/usr/lib/libproc.dylib` 载入 |
| 2026-04-18 | 持久化 `wrapper_start_time_us`（R3/B6） | 原仅 `wrapper_pid` 无法防 wrapper 自身 pid reuse；reconciler 判活 wrapper 时用同样三元组 |
| 2026-04-18 | Session probe 契约（R3/B7） | 24h 阈值 + 5s `:probe:` sentinel + `sessions_index` in-memory/磁盘持久化；status enum `resumed|fresh_fallback|resume_failed` 写入 `bg_runs.session_resume_status` |
| 2026-04-18 | Archive cleanup 并发保护（R3/B8） | `BEGIN IMMEDIATE` + predicate 排除 `pending|enqueued|delivery_failed<10` 行；防 retry 中途被 cleanup 截断 |
| 2026-04-18 | 合成 turn 截断顺序确定性（R3/B9） | 16KB 预算按 4 步顺序截：tails→1024B / output_paths top 5 / on_done_prompt / 始终保留 `[bg-task:id]` 和 manifest path；避免"各字段都长时哪个被截"模糊 |
| 2026-04-18 | Cancel poll 500ms（R3/B10） | wrapper Phase W 在等待子退出的同时 select/poll 500ms 检查 cancel_requested_at；SIGTERM grace 5s 后 SIGKILL |
| 2026-04-18 | Quarantine 保留 3 份或 30 天（R3/B11） | 原只说"quarantine"无保留策略；多次损坏累积无上限；取更晚的 3 或 30 天 |
| 2026-04-18 | runner_token 定性为非秘密 nonce（R3/B12） | token 随 env + argv 可见于 `ps eww`；作用仅是"同 pid 的多代进程去重"的唯一 ID，不用于权限；删除单独 token 文件，DB+env 双保留即可 |
