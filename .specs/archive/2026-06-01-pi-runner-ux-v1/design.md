# Design: pi-runner-ux-v1

> Round 2 — addresses plan-review findings (CRITICAL memory ownership; HIGH scope/precedence/backfill/new-detection; MEDIUM contracts/validate/rollback; LOW anchors).

## Item 1 — PiRunner 工具状态数据流（id 关联，单次 emit）

现状（坏）：裸字符串、两事件族各塞、小写名 miss 映射、无参数。

```
现状:
  tool_execution_start{toolName:"read"}            ─► append("read")   ← 裸名
  message_update.toolcall_start{toolCall.name:"read"} ─► append("read") ← 同一调用第二次
  tool_execution_end / toolcall_end                ─► 再各 append 一次  ← 重复 x4
  ui: label="read"(miss映射), hint=""(无目标)  →  卡片 "read read ..."

目标（Approach A，唯一权威源 = toolcall_*，按 id 单次 emit）:
  唯一权威源是 message_update.toolcall_*（runtime_pi.py:228-246），携带
  toolCall{id, name, arguments}。top-level tool_execution_*（runtime_pi.py:87-97，
  仅 toolName、无 arguments、可能无 id）改为**对 pending_tool_status 完全 no-op**
  （删除其现有 append）→ 从根上消除两事件族重复与 id-less 兜底歧义（finding#4）。
  PiRunner 在 StreamState 维护 _tool_seen: set[id]。规则（仅在 toolcall_* 路径）：
    on toolcall_start/end for id X:
       if X not in _tool_seen and arguments_available(X):
            name = _normalize_pi_tool(toolCall.name)   # bash→Bash, read→Read, ls→Ls
            hint = _extract_hint_data(name, toolCall.arguments)  # 共享函数, 兼容 pi arg key
            pending_tool_status.append({"name":name,"hint_data":hint})
            _tool_seen.add(X)
       elif X not in _tool_seen and is_start and no args yet:
            pass   # 不 emit 空 hint（避免 ui label-backfill 错配 finding#5）；同 id 后续事件拿 args 再 emit
  ui.tool_status_update([{name:"Read",hint_data:"/a/README.md"}])
       label=_TOOL_STATUS_MAP["Read"]="读取文件"; hint=_format_tool_hint→"README.md"
  卡片: "读取文件 README.md"   ← 每调用一次，正确
```

**关键变更 vs Round 1**：(a) 放弃"start emit 空 + end backfill"（ui backfill 按 label 匹配易错配，finding#5）；(b) **唯一权威源定为 `toolcall_*`，`tool_execution_*` 对状态 no-op**（finding#4）—— 不再需要"两族优先级/id-less 兜底"逻辑，因为根本不从 id-less 族 emit。**已验证假设**（来自真实 pi session：每个工具调用的 message content 均为 `toolCall{id,name,arguments}`）：pi 每次工具调用都产生 toolcall_*，故 no-op tool_execution_* 不会丢状态。退化情形（仅 tool_execution_*、无 toolcall_*）→ 该工具不显示状态，记为已知限制并加验证用例。代价：极少数状态在其 toolcall_end 才出现（pi 调用快，可接受）。

### pi 工具名映射（authoritative，来自真实 pi session 数据）

| pi tool | arguments | → canonical | ui label | hint 来源 |
|---------|-----------|-------------|----------|-----------|
| `bash`  | `command`(+`timeout`) | Bash | 执行命令 | 共享：basename(executable)（安全，不暴露全命令/参数） |
| `read`  | `path`(+`limit`/`offset`) | Read | 读取文件 | 共享：path → ui basename |
| `write` | `path`,`content` | Write | 写入文件 | 共享：path → ui basename |
| `edit`  | `path`,`edits[]` | Edit | 编辑文件 | 共享：path → ui basename |
| `grep`  | `pattern`(+`path`/`glob`/`ignoreCase`) | Grep | 搜索代码 | 共享：pattern[:30] |
| `ls`    | `path` | **Ls（新增）** | **列出目录（ui 新增标签）** | **`_extract_hint_data` 新增 Ls 分支：path**；**`_format_tool_hint` 新增 Ls→basename**（finding#3） |
| `find`  | `path`,`pattern` | Find | 查找文件 | **`_extract_hint_data` Find 现读 list `paths`；补单数 `path`/`pattern` 取值** |

`_extract_hint_data`（runtime.py:455-530）已支持 Bash`command`/Read·Write·Edit`path`/Grep`pattern`，与 pi arg key 一致 → 只需补 `Ls` 与单数 `find`。

## Item 2 — `/new` abort-then-new 时序（活跃检测走 cancel 路径）

```
/new ──► commands.cmd_new
  tag = f"{bot_name}:{chat_id}"          # 与 main.py cancel 路径同款 in-flight key
  was_active = runner.cancel(tag)        # 真实接口 runtime.py:942-958, 返回 bool
                                          # cancel(tag) 本身即活跃检测器：
                                          #   True  = 该 tag 有 in-flight 进程, 已 set cancelled + SIGTERM (现链路收尾卡片)
                                          #   False = 无活跃进程 (含首个 --no-session 无 old_sid 的 turn, finding#6)
                                          # cancel 是 BaseRunner 方法 → 所有 runner 通用, 无需 supports_cancel (finding#12)
  clear session id if present            # 现状不变
  if was_active:
        deliver("已中止当前任务，下条消息将开新会话")
  else:
        deliver("会话已重置，下一条消息将开始新对话。")   # 与现有 /new 文案一致
```
关键：用 `cancel(tag)` 的返回值判活跃（不靠 old_sid 是否存在，finding#6）；`cancel` 内部已是 set cancelled 标志 → SIGTERM（runtime.py:946-951，区分用户取消 vs 超时的既有语义）；先 cancel 后 clear（防进程悬挂，premise 3）。`cancel` 对无活跃 tag 安全返回 False → 空闲行为对所有 runner 不变（finding#7/#12）。

## Item 3 — per-session memory（pi 独占写 + bridge 只读，解 CRITICAL）

```
所有权模型（finding#1）:
  WRITER: 仅 pi（用 write/edit 工具）。同 (bot,chat,thread) scope 的 turn 串行 → 单写者无并发。
  READER: 仅 bridge（PiRunner._build_system_prompt）。无锁读（与 session_journal.read:319 一致）。
  bridge 绝不写、绝不 prune 该文件 → 无双写竞争，无需 scope lock。

存储 scope = (bot_id, chat_id, thread_id)（finding#2，照抄 _scope_hash:159-162）:
  root = bridge_home()/feishu-bridge/pi-memory/   # honors FEISHU_BRIDGE_BG_HOME → 多实例隔离
  file = root / f"{sha1(bot|chat|thread)}.md"

注入（PiRunner build_args → _build_system_prompt 调用点 runtime_pi.py:67）:
  raw = safe_read(file)                 # try/except, 缺失/不可读=空
  body = soft_tail_cap(raw, MAX_INJECT_BYTES)   # 仅截断"注入副本"的头部, 文件不动
  append 到 --append-system-prompt:
    "## Persistent memory (this chat)\n<body>\n
     <写入协议见下>"

写入协议（注入到 prompt，指示 pi 维护，finding#9）:
  - 文件: <abs_file_path>（绝对路径）
  - 布局: markdown，每条事实一行 `- [YYYY-MM-DD] <fact>` 或分节
  - 更新: 先用 read 读现有内容 → 保留旧事实 → 用 edit 增改（优先 edit 而非 write 整体覆盖）
  - 预算: 保持 < SOFT_BUDGET_BYTES（如 8KB）；超了由 pi 合并/删旧（bridge 不替 pi prune）
```

`safe_read` + `soft_tail_cap` 仅作用于注入，文件生命周期完全归 pi。这与 session_journal 的"bridge 独占写 + 锁 + prune"是**相反但自洽**的所有权选择：因为 memory 必须由 agent 写，故反过来让 bridge 完全只读，消除多写者（Round 1 的错误是两者混用）。

## 关键决策

1. **工具名归一化在 PiRunner**：`ui.py` 框架保持 runner-agnostic（claude/omp/alma 已证可用）；仅补 `Ls` 标签 + `_format_tool_hint` 的 Ls basename 分支 + `_extract_hint_data` 的 Ls/单数 find 取值。
2. **按 tool-call id 单次 emit**：取代 start-空+end-backfill，消除两事件族重复与 ui backfill 错配。
3. **memory pi 独占写 / bridge 只读 + 软注入 cap**：消除并发写竞争，贴合 pure-conduit。
4. **memory scope 含 thread_id**：与 session/cancel key 一致。
5. **`/new` 活跃检测走 tag-based cancel 路径**：覆盖无 session id 的活跃 turn；通用于支持 cancel 的 runner。
6. **never-raises**：Item 1 提取失败→裸名；Item 3 读取失败→不注入；均 try/except 不冒泡。

## 影响范围

- `[MOD] feishu_bridge/runtime_pi.py` — 加 `_TOOL_NAME_MAP`+`_normalize_pi_tool`；`parse_streaming_line`/`_handle_message_update` 改为 id 关联单次 emit dict（事件载荷优先级）；StreamState 加 `_tool_seen`；`build_args` 调用 `_build_system_prompt` 处（runtime_pi.py:67 为**调用点**，方法在 BaseRunner）注入 per-session memory 段。
- `[MOD] feishu_bridge/ui.py` — `_TOOL_STATUS_MAP` 加 `"Ls":"列出目录"`；`_format_tool_hint` 加 `Ls` → basename 分支（finding#3）。
- `[MOD] feishu_bridge/runtime.py` — `_extract_hint_data`（455-530）加 `Ls` 分支 + `Find` 兼容单数 `path`/`pattern`。
- `[NEW] feishu_bridge/pi_memory.py` — per-session memory: 路径解析（mirror `_scope_hash`）+ `safe_read` + `soft_tail_cap`（**只读，无写/prune**）。
- `[MOD] feishu_bridge/commands.py` — `/new` 加 tag-based 活跃检测 → cancel-then-clear（通用 runner）。
- `[MOD] tests/unit/`（见 tasks.md，新增 `test_pi_runner.py`）。

> Anchor 校正（finding#11）：`runtime_pi.py:67` 是 `_build_system_prompt` 的**调用点**（方法定义在 BaseRunner）；session_journal 的 prune 助手是 `_maybe_prune_locked`（本设计 bridge 不 prune，故不复用它，仅借 `_scope_hash` 命名方案）。
