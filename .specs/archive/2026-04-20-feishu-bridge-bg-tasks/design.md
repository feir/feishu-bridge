# Design: feishu-bridge-bg-tasks

## 技术方案

### 整体架构（task-runner wrapper 模型）

核心原则：**subprocess 生命周期的耐用性不依赖 bridge**。bridge spawn 一个独立的 `task-runner` wrapper 二进制，由 wrapper 负责 `wait()` child、写 manifest、更新 SQLite；bridge 崩溃/重启不影响 wrapper，因此 manifest 永远由存活的父进程完成写入。

```
┌─────────────────┐                ┌──────────────────────────────────────┐
│  Claude turn    │                │         feishu-bridge (supervisor)    │
│                 │                │                                       │
│  CLI enqueue ───┼── INSERT ─────>│  ┌────────────────────────────────┐   │
│  (feishu-bridge │   WAL row      │  │  SQLite bg_tasks / bg_runs     │   │
│   -cli bg …)    │                │  └────────────────────────────────┘   │
│                 │   UDS nudge    │  ┌────────────────────────────────┐   │
│                 │───────────────>│  │  Wake listener + 1s poller     │   │
│                 │                │  └──────────┬─────────────────────┘   │
└─────────────────┘                │             │                         │
                                   │             v                         │
                                   │  ┌────────────────────────────────┐   │
                                   │  │ CAS claim: queued → launching   │   │
                                   │  │ spawn task-runner (detached)    │   │
                                   │  └──────────┬─────────────────────┘   │
                                   │             │ fork + exec              │
                                   └─────────────┼─────────────────────────┘
                                                 v
                             ┌──────────────────────────────────────────┐
                             │  task-runner wrapper (独立进程)          │
                             │  • setsid / setpgid                      │
                             │  • 注入 BG_TASK_TOKEN env                │
                             │  • spawn user command (Popen)            │
                             │  • UPDATE state=running, record          │
                             │    pid/pgid/process_start_time/token     │
                             │  • wait() + 流式 stdout/stderr           │
                             │  • 监听 timeout（monotonic）             │
                             │  • 退出后：写 task.json.tmp → rename     │
                             │  • UPDATE bg_runs (终态 + exit_code)     │
                             │  • UPDATE bg_tasks.state (终态)          │
                             │  • UDS nudge bridge（delivery outbox）   │
                             └──────────────────────────────────────────┘
                                                 │
                                                 v
                             ┌──────────────────────────────────────────┐
                             │  bridge delivery watcher (poller + wake) │
                             │  • 扫 bg_runs.delivery_state='pending'   │
                             │  • 读 manifest → build synthetic prompt  │
                             │  • enqueue_turn(…, kind=                 │
                             │    'bg_task_completion')                 │
                             │  • pending→enqueued（入 ChatTaskQueue） │
                             │  • enqueued→sent（飞书 API 成功）       │
                             │  • 失败 → 'delivery_failed' + retry      │
                             └──────────────────────────────────────────┘
```

两层分工：
- **bridge supervisor**：launch wrapper，不直接拥有 user-command 进程；崩溃也不影响 wrapper 继续跑完。
- **task-runner wrapper**：拥有 user-command 进程组，是 manifest 的唯一写入者，是 exit_code 的唯一捕获者。
- **bridge delivery watcher**：独立于 supervisor，专门扫描 `delivery_state='pending'` 把 completion 送进 ChatTaskQueue；crash-safe outbox pattern。

### 数据模型（SQLite）

路径：`~/.feishu-bridge/bg_tasks.db`（首次启动时由 bridge 或 CLI 初始化）

```sql
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS bg_tasks (
    id                    TEXT PRIMARY KEY,           -- uuid4 hex (32 chars)
    chat_id               TEXT NOT NULL,
    session_id            TEXT NOT NULL,
    requester_open_id     TEXT,
    kind                  TEXT NOT NULL DEFAULT 'adhoc',  -- 本变更仅允许 'adhoc'
    command_argv          TEXT NOT NULL,              -- JSON list[str]; shell=False 强制
    cwd                   TEXT,
    env_overlay           TEXT,                        -- JSON dict
    timeout_seconds       INTEGER NOT NULL DEFAULT 1800,  -- 默认 30 min（任务上限 15 min + 2× 安全系数）
    on_done_prompt        TEXT NOT NULL,
    output_paths          TEXT,                        -- JSON list
    state                 TEXT NOT NULL DEFAULT 'queued',
    reason                TEXT,                        -- orphan_cause / launch_interrupted / etc
    signal                TEXT,                        -- SIGTERM / SIGKILL（若以信号结束）
    error_message         TEXT,                        -- 人可读错误
    cancel_requested_at   INTEGER,                     -- unix epoch ms
    claimed_by            TEXT,                        -- bridge instance uuid（防 launchd 重叠）
    claimed_at            INTEGER,                     -- unix epoch ms
    created_at            INTEGER NOT NULL,
    updated_at            INTEGER NOT NULL,
    CHECK (state IN ('queued','launching','running','completed','failed','cancelled','timeout','orphan')),
    CHECK (kind = 'adhoc')
);

CREATE INDEX IF NOT EXISTS idx_bg_tasks_state       ON bg_tasks(state);
CREATE INDEX IF NOT EXISTS idx_bg_tasks_chat        ON bg_tasks(chat_id, state);
CREATE INDEX IF NOT EXISTS idx_bg_tasks_updated     ON bg_tasks(updated_at);
CREATE INDEX IF NOT EXISTS idx_bg_tasks_launching   ON bg_tasks(state, claimed_at)
    WHERE state = 'launching';

CREATE TABLE IF NOT EXISTS bg_runs (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id                   TEXT NOT NULL REFERENCES bg_tasks(id) ON DELETE CASCADE,
    runner_token              TEXT NOT NULL,          -- uuid4 nonce；注入 wrapper env，非 secret（同用户 ps eww 可见），仅用于 pid reuse 防御
    pid                       INTEGER,                -- user command pid（wrapper 的 child）
    pgid                      INTEGER,
    process_start_time_us     INTEGER,                -- 从 libproc.proc_pidinfo(PROC_PIDTBSDINFO).pbi_start_tvsec/usec 计算；μs 精度
    wrapper_pid               INTEGER NOT NULL,       -- task-runner wrapper 自身 pid；**row 在 Popen 之前 insert**，见 §原子写入约定
    wrapper_start_time_us     INTEGER NOT NULL,       -- wrapper 自身 start time；wrapper identity triple 的一部分
    started_at                INTEGER NOT NULL,
    finished_at               INTEGER,
    exit_code                 INTEGER,
    signal                    TEXT,
    manifest_path             TEXT,
    stdout_tail               BLOB,                   -- 最多 4096 字节，UTF-8 安全截断
    stderr_tail               BLOB,                   -- 最多 4096 字节，UTF-8 安全截断
    delivery_state            TEXT NOT NULL DEFAULT 'not_ready',
                                                      -- not_ready | pending | enqueued | sent | delivery_failed
    delivery_error            TEXT,
    delivery_attempt_count    INTEGER NOT NULL DEFAULT 0,
    completion_detected_at    INTEGER,                -- bridge watcher 首次看到 manifest 的时刻
    enqueued_at               INTEGER,                -- 合成 turn 入 ChatTaskQueue 的时刻
    sent_at                   INTEGER,                -- Feishu send ack 成功时刻（真正 delivered 给用户的时刻）
    session_resume_status     TEXT,                   -- resumed | fresh_fallback | resume_failed
    CHECK (delivery_state IN ('not_ready','pending','enqueued','sent','delivery_failed'))
);

CREATE INDEX IF NOT EXISTS idx_bg_runs_task      ON bg_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_bg_runs_delivery  ON bg_runs(delivery_state);
```

**状态机（含 launching 瞬态）**：

```
(none)──INSERT──> queued ──CAS claim──> launching ──wrapper up──> running
                    │                      │                        │
                    │                      │ wrapper 崩在           │ exit==0
                    │                      │ spawn 之前             └────> completed
                    │                      │                        │ exit!=0
                    │                      │ (startup reconciler    ├────> failed
                    │                      │  → failed, reason=     │ SIGKILL by cancel
                    │                      │  'launch_interrupted') ├────> cancelled
                    │                      │                        │ SIGKILL by timeout
                    │                      ▼                        ├────> timeout
                    │                 (terminal)                    │ wrapper 死 + 无 manifest
                    │                                               └────> orphan
                    │ cancel_requested_at 在 launch 前
                    └──────────────────────────────────────> cancelled
```

规则：
- **CLI 永远不改 state**（除了 INSERT=queued 和 cancel_requested_at flag）
- state 单向流动；bridge/wrapper 在 repo 层做 `validate_transition()` 拒绝非法跳转
- `launching` 是瞬态：supervisor claim 后、wrapper `UPDATE state='running'` 前的窗口，startup 时若发现 claimed_at > 30s 前，视为 launch 失败 → `failed, reason='launch_interrupted'`

### Launch claim（原子 CAS）

Supervisor（wake listener + poller）用 CAS 防止双启动：

```sql
-- 仅第一个成功 UPDATE 的进程拿到 row
UPDATE bg_tasks
SET state='launching', claimed_by=:bridge_instance_id, claimed_at=:now_ms, updated_at=:now_ms
WHERE id=:task_id AND state='queued' AND cancel_requested_at IS NULL;
-- RETURNING * on SQLite 3.35+（bridge 已依赖的版本）
```

成功时才 spawn wrapper。不成功说明另一个 bridge 实例（launchd reload 重叠窗口）已认领；本轮跳过。

### 文件系统布局

```
~/.feishu-bridge/
├── bg_tasks.db                 # SQLite 主库 + -shm / -wal 旁文件
├── bg_tasks.db.quarantine.<ts> # integrity_check 失败后的隔离副本
├── wake.sock                   # UDS（parent dir 0700，socket 0600）
├── bridge.instance             # 当前 bridge instance uuid（启动时写）
├── tasks/
│   ├── active/<task_id>/
│   │   ├── stdout.log
│   │   ├── stderr.log
│   │   ├── wrapper.pid         # wrapper 自身 pid（hard link 到 /proc 不可用，存文件）
│   │   ├── task.json.tmp       # manifest 写入中
│   │   # 注：不再单独落盘 runner_token 文件——DB + env 是唯一来源
│   ├── completed/<task_id>/
│   │   ├── stdout.log
│   │   ├── stderr.log
│   │   └── task.json.done      # atomic rename 完成后的 manifest
│   └── _archive/               # 终态 > 7 天的 task（compress + keep，并非删除）
└── logs/
    ├── supervisor.log          # bridge supervisor 日志
    └── task-runner/<task_id>.log  # wrapper 自身日志（非 user command 的 stdout）
```

**保留策略**（澄清）：
- `tasks/completed/<task_id>/` 保留 7 天；之后打包 gzip 到 `_archive/<yyyy-mm>/<task_id>.tar.gz`
- `_archive/` 保留 90 天；之后删除
- `bg_tasks` 行 cascade 删除对应 `bg_runs`；archive cleanup 同时 DELETE DB 行
- **Cleanup 竞态保护**：DELETE 前 `BEGIN IMMEDIATE` + 检查 `delivery_state IN ('sent') OR (delivery_state='delivery_failed' AND delivery_attempt_count >= 10)`；否则跳过（保留待重试）。防止 delivery watcher 正在 retry 时行被删

### Manifest Schema (`task.json.done`)

```json
{
  "schema_version": 2,
  "task_id": "abc123...",
  "state": "completed",
  "reason": null,
  "signal": null,
  "exit_code": 0,
  "runner_token": "uuid4-...",
  "pid": 12345,
  "pgid": 12345,
  "process_start_time_us": 1713398400123456,
  "wrapper_pid": 12300,
  "wrapper_start_time_us": 1713398399800000,
  "started_at_ms": 1713398400000,
  "finished_at_ms": 1713398760000,
  "duration_seconds": 360,
  "command_argv": ["python3", "/path/to/script.py", "--flag"],
  "cwd": "/Users/feir/models",
  "stdout_tail_b64": "base64-4096-bytes-max",
  "stderr_tail_b64": "base64-4096-bytes-max",
  "output_paths": ["/Users/feir/models/bench_result.json"],
  "on_done_prompt": "...",
  "chat_id": "oc_xxx",
  "session_id": "oc_xxx:thread_y"
}
```

**原子写入约定**（wrapper 职责）：

**Phase P（Pre-spawn identity registration）** —— 在 `Popen()` 之前：
- P1. wrapper 自己取 `wrapper_start_time_us`（`libproc.proc_pidinfo`）
- P2. 单事务 INSERT `bg_runs` 行（runner_token, wrapper_pid, wrapper_start_time_us, started_at=now, delivery_state='not_ready'；pid/pgid/process_start_time_us 暂空）
- P3. 若该 INSERT 失败 → wrapper 立即退出非零 + 写 `wrapper.log`；不 Popen child

**Phase S（Spawn + register child）**：
- S1. `subprocess.Popen(argv, shell=False, start_new_session=True, env={...,'BG_TASK_TOKEN':token})`
- S2. 拿到 child pid 立刻 `libproc.proc_pidinfo(pid, PROC_PIDTBSDINFO)` 读 `process_start_time_us`
- S3. **单事务**：UPDATE bg_runs SET pid/pgid/process_start_time_us；UPDATE bg_tasks SET state='running'。失败 → SIGTERM child + rollback + orphan

**Phase W（wait + stream）**：wrapper 流式写 stdout/stderr.log，轮询 cancel_requested_at（500ms 周期），`time.monotonic()` deadline

**Phase C（Completion）** —— child 退出后：
- C1. 写 `tasks/active/<id>/task.json.tmp`（`os.write` + `os.fsync`）
- C2. `os.rename()` 到 `tasks/active/<id>/task.json.done`
- C3. `os.rename()` 整个目录到 `tasks/completed/<id>/`
- C4. **单事务**：UPDATE bg_runs (manifest_path, finished_at, exit_code, signal, stdout_tail, stderr_tail, delivery_state='pending') + UPDATE bg_tasks (state=<terminal>, signal, reason?, updated_at)。事务失败 → 重试 3 次；仍失败 → wrapper 退出，reconciler 从 manifest replay
- C5. UDS nudge bridge（delivery watcher 立即处理）

Crash window 识别（配合 reconciler）：
- Phase P 后 / S 前崩 → bg_runs 有行但 pid IS NULL → reconciler 识别为 `bg_tasks.state='launching'` + 孤立 bg_runs；标 `failed, reason='spawn_not_attempted'`；**child 不可能存在**
- Phase S1 完成 + S3 前崩（child 已 Popen，但 pid 未回写 DB）→ child 孤立运行。reconciler 用 runner_token 扫 `ps eww` argv+env 找对应进程，找到 → 通过 pgid 发送 SIGKILL（wrapper 已死不可救）→ 标 `orphan, reason='wrapper_died_pre_register'`。即使扫描失败（极少数）进程最终被用户/launchd 清理
- Phase S3 完成 + C 前崩 → bg_tasks.state='running' + wrapper 死 → reconciler 走 "wrapper dead + child 判活" 分支，见 §Startup Reconciler
- C1-C3 任一步崩 → active 目录无 .done → orphan
- C4 事务未 commit → bg_runs.delivery_state='not_ready' → reconciler 从 manifest replay；幂等
- C4 commit 后 + C5 前崩 → poller 1s 内扫到 `delivery_state='pending'` → 补投

### UDS Wake Protocol

- Parent dir `~/.feishu-bridge/` 权限 0700；`wake.sock` 0600
- 协议：
  - `b'\x01'` ：通用 ping → 扫 `queued` + `delivery_state='pending'`
  - `b'\x02' + 16 bytes`（`uuid.UUID(hex=task_id).bytes`）：指定 task_id
  - `b'\x03' + 16 bytes`：wrapper 通知 delivery ready
- CLI / wrapper nudge 后立即 `close()`，不等响应
- Listener 收到 → 触发 scan event；fallback poller 1s 周期
- 启动 bind 处理：
  - `EADDRINUSE` → client 侧 connect 探测；无 listener 响应 → `unlink()` + rebind
  - 仍失败 → WARN 但不阻止启动；poller 保底

### Startup Reconciler

bridge 启动时（`main.py` 主循环 before `ws_client.start()`）执行，顺序：

1. **Integrity check**：`PRAGMA integrity_check;` 失败 → quarantine DB + 从 manifest 重建（见 §DB 恢复）
2. **Stale launching**：`UPDATE bg_tasks SET state='failed', reason='launch_interrupted' WHERE state='launching' AND claimed_at < now - 30_000`
3. **Running 活性判断**（每个 `state='running'` 行）：

   ```
   wrapper alive（三元组：wrapper_pid 存活 +
                         wrapper_start_time_us 匹配 libproc 读到的值 +
                         task-runner 进程 argv 包含 runner_token）？
     └─ yes → 已由 wrapper 自己管，跳过
     └─ no → user command alive（三元组：pid 存活 +
                                         process_start_time_us 匹配 +
                                         env BG_TASK_TOKEN=runner_token）？
             ├─ yes → "orphan 孤儿活着"场景。wrapper 死，child 还在：
             │         bridge 直接接管生命周期：
             │         (a) 若 cancel_requested_at IS NOT NULL → killpg(pgid, SIGTERM)；
             │             500ms 轮询 5s grace，超时 SIGKILL；
             │             标 state='cancelled', reason='reaped_by_bridge_after_wrapper_death'
             │         (b) 若 time.monotonic() 相对 started_at 超过 timeout_seconds →
             │             killpg + 标 state='timeout' 同上
             │         (c) 否则起新 reaper 线程继续轮询 child 退出；
             │             退出后若无 manifest → orphan, reason='wrapper_dead_child_died_no_manifest'；
             │             manifest 存在 → 从 manifest 更新（罕见，wrapper 临终前写完了 manifest）
             └─ no  → manifest 存在？
                       ├─ yes → 从 manifest UPDATE state, exit_code, signal, stdout/stderr_tail
                       │         + delivery_state='pending'（单事务）
                       └─ no  → state='orphan', reason='wrapper_and_child_both_died'
   ```

   关键：所有 `killpg` 之前必须通过三元组验证目标进程身份，mismatch 则 **绝不发信号**，直接 orphan 化记录。

4. **Queued 续推**：`state='queued' AND cancel_requested_at IS NULL` → 再次 claim + launch
5. **Delivery outbox**：`bg_runs WHERE delivery_state IN ('pending','delivery_failed') AND delivery_attempt_count < 10` → 重新 enqueue 合成 turn
6. **Manifest-only 行**（DB 无对应 bg_runs）：从 `task.json.done` 回填 bg_runs + bg_tasks；`delivery_state='pending'`

**pid reuse 防御**（关键）：
- `process_start_time_us` / `wrapper_start_time_us`：macOS 用 `libproc.proc_pidinfo(pid, PROC_PIDTBSDINFO)`（通过 `ctypes` 调用 `/usr/lib/libproc.dylib`）读 `pbi_start_tvsec` + `pbi_start_tvusec`，合并为 μs 精度 epoch。**不使用** `ps -o lstart=`（仅秒精度）。退出监听另外用 `kqueue(EVFILT_PROC, NOTE_EXIT)` 注册
- `runner_token`：uuid4 nonce（非 secret，同用户 `ps eww` 可见）。注入 `env={'BG_TASK_TOKEN': token}` + 命令行 argv 末尾。验证：macOS `ps -E -o command= -p <pid>` 读到完整 argv+env；匹配 `BG_TASK_TOKEN=<expected>`
- **三元组匹配**（pid 存活 + start_time μs 级完全一致 + runner_token 在 env 中）任一不一致 → 视为 pid reuse，不发任何信号，直接 orphan
- 宁可误判 orphan，不可向无关进程 SIGKILL

### DB 损坏恢复

```
1. PRAGMA integrity_check 返回非 'ok'
2. mv bg_tasks.db bg_tasks.db.quarantine.<ts>
3. 新建空 schema
4. 扫描 tasks/completed/*/task.json.done 全部 manifest：
     - INSERT bg_tasks（state=manifest.state, reason='recovered_from_manifest'）
     - INSERT bg_runs（delivery_state='pending'）
5. 扫 tasks/active/*：
     - task.json.done 存在 → 移到 completed/ 再按 4 处理
     - 只有 task.json.tmp / 无 manifest → state='orphan'
6. queued pre-launch 任务丢失 —— 这个限制在 CLI --help 中明确声明
7. 日志 WARN + 通过 bridge 自身 Feishu 渠道通知管理员（best-effort）
```

**Quarantine retention**：`bg_tasks.db.quarantine.<ts>` 文件保留最近 3 份或 30 天（取更长）；超出由 §archive cleanup 作业一并删除。

### Cancel SLO

proposal AC 要求 "cancel ≤10s 进入终态"。实现细节：
- CLI 侧 SQL UPDATE + UDS nudge ~ 50ms
- wrapper 收到 cancel 信号（SIGTERM）→ grace 5s（收紧，原 10s 越界）→ SIGKILL
- manifest 写入 ~ 100ms
- bridge delivery 入队 ~ 100ms
- **SLO 口径**：CLI 返回后 ≤10s 内 `bg_tasks.state` 转为 `cancelled`。用 `time.monotonic()` 计算 deadline，不用 wall clock。

### Timeout 实现

- 所有 live deadlines 用 `time.monotonic()`
- 持久化时转 `unix epoch ms`（用于 UI / 跨进程传递）
- 默认 `timeout_seconds = 1800`（30 min，任务上限 15 min + 安全系数）
- `--timeout-seconds` 允许覆盖；CLI 校验 1 ≤ t ≤ 86400
- wrapper 内实现：`select()` / `poll()` 带 timeout，避免 sleep 被系统睡眠扭曲

### Command Argv 规范

严格禁止 shell 解析：
- CLI 接收 argv 用 `--` 分隔符：`feishu-bridge-cli bg enqueue [flags] -- python3 script.py --arg val`
- 或显式 JSON：`--cmd-json '["python3","script.py","--arg"]'`
- DB 列 `command_argv` 存 JSON list[str]
- wrapper spawn 用 `subprocess.Popen(argv, shell=False)`，永不 `shell=True`
- 若有人传了含 shell metachar 的 bare string，CLI 返回非零 + 错误提示，不自动 shlex-split

威胁模型（显式声明）：Claude 在单用户 macOS 上已有同用户权限，`bg enqueue` = 同用户任意代码执行；接受该模型，仅通过 `shell=False` 断开 meta-expansion 的额外攻击面。

### Synthetic Turn Format

```
[bg-task:{task_id}] Background task {state_verb}.

State: {state}
Reason: {reason_or_null}
Duration: {duration_seconds}s
Exit code: {exit_code}
Signal: {signal_or_null}
Output files:
  - {output_path_1}
  - {output_path_2}

Stdout tail (last {N} bytes, UTF-8-safe):
{stdout_tail}

Stderr tail (last {N} bytes, UTF-8-safe):
{stderr_tail}

Original intent:
{on_done_prompt}
```

- 入队：`enqueue_turn(chat_id, session_id, prompt, kind='bg_task_completion')`
- **Delivery outbox 三态**：`pending`（manifest 写完，未入队）→ `enqueued`（ChatTaskQueue.put 成功 + `bg_runs.enqueued_at` 记录时刻）→ `sent`（Feishu send API 返回 ack + `sent_at` 记录）。只有 `sent` 才算真正 delivered。
- **Bridge send 路径**：真实 send 成功后回调 `BgTaskRepo.mark_sent(task_id)`；失败回调 `mark_delivery_failed(task_id, error)` + `delivery_attempt_count += 1`。
- **Stuck `enqueued` 扫描**：startup + poller 每次扫描 `delivery_state='enqueued' AND enqueued_at < now - 5min` → 回滚为 `pending` 重投（防 ChatTaskQueue 进了但 Feishu send 前 bridge 崩）。
- **ChatTaskQueue 满策略**：`kind='bg_task_completion'` 不受 `MAX_PENDING_PER_SESSION` 限制；人类消息仍受限。理由：completion 丢失 = 用户永远看不到结果。
- **Tail 截断**：4096 **字节**上限；截断点向前回退到 UTF-8 boundary（首字节高位 `10xxxxxx` 不能做截断点）；prepend `...[truncated]\n`
- **Prompt 总长上限 16KB**：确定性 clamp 顺序：
  1. stdout_tail / stderr_tail 各砍到 1024 字节（UTF-8 safe）
  2. output_paths 列表保留前 5 条，之后追加 `... (N more omitted)` 标记
  3. 仍超 → `on_done_prompt` 截断，后缀 `...[truncated from original {N} chars]`
  4. **永远保留** `[bg-task:{task_id}]` 开头行 + `manifest: {path}` 行（调试兜底）

### Session Resume Fallback

15 min 任务跑完时，原 session 可能已被 compact / `/new` / bridge 重启。

**可 resume 探测契约**（确定性）：
- bridge 维护 `sessions_index`（in-memory dict + 持久化到 `~/.feishu-bridge/sessions.json`，每次 `_on_message` 结束后更新）：`{session_id: {last_seen_at_ms, last_turn_cost_usd, compact_fingerprint}}`
- enqueue_turn 前执行：
  ```
  if session_id in sessions_index AND now - last_seen_at_ms < 24h:
      status = 'resumed'
  elif 在 sessions_index 但超过 24h:
      # 不可靠，做一次 5s sentinel probe：claude -p --resume <id> -p ":probe:"（立即返回的 no-op 前缀）
      if 探测成功: status = 'resumed'
      elif 返回 session-not-found / compacted / timeout: status = 'fresh_fallback'
      else: status = 'resume_failed'
  else:
      status = 'fresh_fallback'
  ```
- `fresh_fallback`：用 chat_id 起新 session；prompt 开头 prepend
  `[NOTE: original session no longer resumable (reason: {reason}); this is a fresh-context bg-task completion. Previously-discussed context is NOT available — read output files for ground truth.]`
- 状态写入 `bg_runs.session_resume_status`（新增列）

### CLI Command Surface

```bash
# 注册：argv 用 -- 分隔
feishu-cli bg enqueue \
    --chat-id oc_xxx \
    [--session-id oc_xxx:thread_y] \
    [--cwd /path] \
    [--env KEY=VAL]... \
    [--timeout-seconds 1800] \
    --on-done-prompt "analyze result" \
    [--output-path /abs/path.json]... \
    -- python3 /path/to/script.py --arg1 val

# 或 JSON 格式
feishu-cli bg enqueue [...] --cmd-json '["python3","/path/to/script.py","--arg1","val"]'

# stdout: {"task_id": "abc123...", "state": "queued", "enqueue_latency_ms": 12}
# bridge not running: 仍成功 INSERT；UDS nudge 失败静默跳过；task_id 返回
# DB locked / corrupted: 非零退出 + stderr 错误描述

feishu-cli bg status <task_id>
feishu-cli bg list [--chat-id X] [--state STATE] [--limit 20]
feishu-cli bg cancel <task_id>   # 仅设 cancel_requested_at + UDS nudge
```

### 回归保护

1. `_on_message` 对真人消息的外部行为不变（内部抽出 `enqueue_turn`，两条路径共用）
2. `MAX_PENDING_PER_SESSION=10` 对人类消息同样生效；**合成 turn 例外**（上文已说明）
3. Reaction / card action 事件处理路径不动
4. 现有 `test_*.py` 全部通过
5. `enqueue_turn(chat_id, session_id, prompt, kind)` 对 `kind in {'human','bg_task_completion'}` 语义等价（除上述 overflow 规则）

## 依赖关系

- 新引入 Python 标准库：`sqlite3`, `socket`, `selectors`, `threading`, `uuid`, `base64`
- 新引入二进制：`task-runner`（作为 bridge 包内的 console_script；独立 entry，便于 launchd 追踪）
- `task-runner` 复用 `BgTaskRepo`（同一 Python 包内 import）
- 无新第三方依赖

## 开放设计决策（已敲定）

| 问题 | 决策 |
|---|---|
| 子进程生命周期所有权 | task-runner wrapper 独立进程；bridge 只是 launcher + delivery watcher |
| Wrapper 生命周期阶段 | P (pre-register) → S (spawn+link) → W (wait+stream) → C (commit+deliver)，每阶段 crash 窗口在 reconciler 覆盖 |
| queued→running 原子化 | `launching` 瞬态 + SQL CAS claim；wrapper Phase P 先 INSERT bg_runs，Phase S 单事务 UPDATE pid+pgid+state='running' |
| completion delivery 耐用性 | bg_runs.delivery_state 4-state outbox（pending→enqueued→sent）；startup + 1s poller 重放，stuck `enqueued`>5min 回滚 pending |
| pid/pgid 身份验证 | `libproc.proc_pidinfo`（μs 精度）+ runner_token（env 非秘密 nonce）三元组；wrapper 自身用 wrapper_pid + wrapper_start_time_us 验证 |
| wrapper 死但子活 | bridge reconciler 三元组验证后主动接管：尊重 cancel_requested_at/timeout_seconds，500ms poll + 5s grace + SIGKILL |
| DB 损坏恢复 | PRAGMA integrity_check + manifest replay；queued 丢失接受；quarantine 保留最近 3 份或 30 天 |
| archive cleanup 并发 | `BEGIN IMMEDIATE` + predicate（delivery_state='sent' OR (='delivery_failed' AND attempt_count>=10)）防止 retry 中途被删 |
| session resume 失败 | sessions_index（in-memory + `~/.feishu-bridge/sessions.json`）记录；>24h 发 5s `:probe:` sentinel；失败 fallback 到 chat_id 起新 session + NOTE prefix，记录 session_resume_status |
| timeout 时钟 | monotonic for deadlines；epoch μs for persistence（macOS sleep+NTP 跳变免疫） |
| shell 执行 | shell=False + `--`/`--cmd-json`；拒绝 bare string |
| tail 截断单位 | 4096 bytes + UTF-8 boundary safe |
| 合成 turn 截断 | 16KB 预算，4 步确定性顺序：tails→1024B → output_paths top 5 → on_done_prompt → 始终保留 `[bg-task:id]` 和 manifest path |
| kind 枚举 | 本变更仅 'adhoc'（schema CHECK）；cron 变更另拓展 |
| 保留策略 | completed 7 天，archive 90 天后删除 |
| 合成 turn overflow | kind='bg_task_completion' 绕过 MAX_PENDING_PER_SESSION |
| cancel 响应延迟 | wrapper Phase W 500ms poll cancel_requested_at；grace 5s 后 SIGKILL |

## 测试策略

### 单元测试
- SQLite schema 迁移 + 所有状态 CHECK 违反拒绝
- CAS claim 并发（两 fiber 同时 UPDATE 同一 queued 行，只一人赢）
- State transition validator（非法跳转拒绝）
- Manifest 原子 rename（mock partial-write crash）
- UTF-8 safe truncation（构造 multi-byte 字符跨 4096 边界）
- runner_token 验证（伪造 pid + mismatch token → reject kill）
- `libproc.proc_pidinfo` μs 精度验证（快速启动两进程，start_time 可区分）
- 合成 turn 16KB 确定性截断顺序（构造各字段均超长的输入，断言截断顺序）
- Delivery outbox 状态机（pending→enqueued→sent 合法；sent→pending 非法拒绝）

### 集成测试（含 crash 窗口）
- 启动 bridge fixture + 真实 task-runner 二进制
- CLI enqueue → `sleep 1` → completion → 合成 turn 入队 < 5s
- **Crash barriers**（使用 fault injection hook）：
  - `post_claim`：claim 后 crash → 重启 → `launching` 超时回收为 failed
  - `post_spawn_pre_register`：Phase S 中 INSERT bg_runs 成功但 UPDATE bg_tasks 未完成前 crash → 重启后用 runner_token 在 `ps eww` 扫描孤儿子进程并回收（**R3 新增**）
  - `post_spawn`：spawn 后 wrapper crash → wrapper 继续跑完子，重启后 delivery 补投
  - `pre_rename`：manifest rename 前 crash → orphan，exit_code 从 kqueue 捕获
  - `post_rename_pre_db`：manifest 写成后 DB update 前 crash → 重启扫 `.json.done` 补 DB + delivery
  - `post_db_pre_enqueue`：DB update 成但 enqueue 前 crash → 重启后 delivery_state=pending 被补投
  - `post_enqueue_send_fail`：已 enqueue 但 send 失败 → delivery_state=delivery_failed，attempt_count++，retry
  - `stuck_enqueued`：模拟 send 中途 bridge crash，delivery_state 卡 enqueued → 5min 后扫描回滚 pending + 重试（**R3 新增**）
  - `orphan_alive_bridge_reap`：kill wrapper 但保留子存活 → bridge reconciler 三元组验证后主动 killpg，记录 reason='reaped_by_bridge_after_wrapper_death'（**R3 新增**）
- **Cancel 路径**：queued cancel、running cancel（SIGTERM→5s grace→SIGKILL 真路径，非 mock）；wrapper 500ms poll 延迟断言
- **Timeout 路径**：wall-clock skew 模拟（monotonic mock）；macOS sleep wake 不导致误判（epoch μs vs monotonic 分工验证）
- **pid reuse 场景**：kill wrapper + 快速 fork 同 pid 的无关进程 → reconciler 通过 process_start_time_us 识别 mismatch → orphan
- **DB 损坏**：truncate bg_tasks.db 到部分字节 → 启动 → quarantine（保留 3 份或 30 天）+ manifest rebuild
- **SQLite BUSY**：CLI 开 transaction 持有 writer lock，bridge 并发 enqueue → 5s busy_timeout 后成功
- **Archive cleanup race**：delivery retry 进行中 cleanup 触发 → BEGIN IMMEDIATE + predicate 保护不删除进行中行
- **Session resume fallback**：>24h 会话 sentinel probe 返回错误 → 记录 session_resume_status='resume_failed' + 起新 session + NOTE prefix

### 回归测试
- 运行现有 `tests/test_*.py` 全套
- 特别验证 `_on_message` 真人消息路径 / Reaction / card action 行为不变
