---
branch: master
start-sha: 989bd97479e596ad356f6514c7808b1d2b8959b3
status: active
---

# Proposal: pi-runner-ux-v1

## WHY

对照官方 pi-chat（pi↔Discord/Telegram bridge）做了一轮深度调研，确认三项 feishu-bridge 的 pi 支持短板，全部 bridge 内部可修、无需引入 pi-chat 依赖或 Gondolin 沙箱：

1. **pi 工具/任务进程在飞书卡片上展示差**：PiRunner 只把裸工具名字符串塞进 `pending_tool_status`，且同一调用经 `tool_execution_*` 与 `message_update.toolcall_*` 两个事件族重复塞入，pi 的小写工具名对不上 `ui.py` 的 PascalCase 映射表，且从不提取参数 → 卡片显示裸 "read"、无文件/命令目标、还重复。对照 claude/omp/alma 都喂 `{name, hint_data}` dict 显示 "读取文件 README.md"。
2. **`/new` 不中止活跃 turn**：`commands.py` 的 `/new` 只删 session id，正在跑的 pi 进程继续，体验割裂。
3. **pi 缺按会话持久记忆**：只注入 bridge 全局 memory，无 per-session 维度，跨会话不隔离也不各自累积。

## WHAT

- **Item 1 — PiRunner 工具状态增强**：PiRunner emit `{name, hint_data}` dict（对齐 omp），归一化 pi 小写工具名到 PascalCase（照抄 omp `_TOOL_NAME_MAP` 模式），复用共享 `_extract_hint_data`。**唯一权威源 = `message_update.toolcall_*`，按 tool-call id 每调用只 emit 一次**；`tool_execution_*` 对状态完全 no-op（消除两事件族重复与 id-less 歧义 + 避免 ui label-backfill 错配）。补 pi 特有工具的 ui 标签（`Ls`）、`_format_tool_hint` 的 `Ls` basename 分支、`_extract_hint_data` 的 `Ls`/单数 `find` 取值。
- **Item 2 — `/new` abort-then-new**：`/new` 构造 `tag=bot:chat`，调真实接口 `runner.cancel(tag)`（BaseRunner 方法，所有 runner 通用）；**用其 bool 返回值判活跃**（True=有 in-flight 进程且已取消并收尾卡片，False=无，含首个 `--no-session` 无 old_sid 的 turn），随后清 session（若有），按是否活跃发对应文案。空闲（cancel 返回 False）时行为对所有 runner 不变。
- **Item 3 — 按会话持久记忆**：scope = `(bot_id, chat_id, thread_id)`（与 `session_journal._scope_hash` 一致，= 一个"会话"），存 per-session `memory.md`。**所有权模型：pi 独占写、bridge 只读**——bridge 读取（无锁，与 `session_journal.read` 一致）并以**软 tail-cap** 注入 PiRunner `--append-system-prompt`，**绝不写/prune** 该文件；pi 用 read-before-write 协议维护并自控大小。prompt 给定固定 markdown 布局 + 写入协议（见 design.md）。

## NOT

- pi 上下文压缩（已验证 pi 0.78.0 CLI 无压缩入口；SDK `ctx.compact()` 不可用于 `--mode json -p` 子进程）
- 出站文件附件（能力已存在：`feishu-cli send-image/file/audio` + `feishu_send.py`；仅 ergonomic）
- 会话感知系统提示 + 稳定发送者身份（群聊向，优先级低于私聊/基础架构，延后）
- secret 请求流、传输层 checkpoint/resume、Gondolin 沙箱、skills 注入（pi 原生支持）
- 重构 omp/claude/alma 的工具状态逻辑（不碰已有 runner 的实现，仅共享 ui/`_extract_hint_data` 加 Ls 分支）

## Acceptance Criteria

- [ ] pi 调 bash/read/write/edit/grep/ls 时，飞书卡片显示中文工具标签 + 文件名/命令描述/目录（非裸 "read"，Ls 显示目录非全路径）
- [ ] 同一工具调用在卡片只计一次（两事件族不重复，按 id 关联）
- [ ] 活跃 turn（含首个尚无 session id 的 `--no-session` turn）中发 `/new`：当前进程被中止、卡片正常收尾、下条消息确为新会话
- [ ] per-session memory：scope A 写入的事实不出现在 scope B（含不同 thread_id）的注入上下文；agent 能读到本 scope 已存记忆；bridge 从不写该文件
- [ ] **回归**：claude/omp/codex 工具状态展示、空闲时各 runner 的 `/new` 行为、`tests/unit/test_bridge.py` 全绿

## Approaches Considered

### Approach A: PiRunner 自包含（minimal viable，选定）
Summary: pi 名归一化 + hint 提取 + id 关联全在 PiRunner（mirror omp `_normalize_tool_name` + 复用共享 `_extract_hint_data`）；`ui.py`/`runtime.py` 仅补 pi 特有的 `Ls` 标签/basename/取值分支。memory 为 pi 独占写 + bridge 只读。
Effort: M  Risk: Low
Pros: blast radius 小，不碰 omp 实现；对齐既有模式；hint 提取已共享；只读注入彻底回避并发写竞争。
Cons: pi 与 omp 各保留一份 `_TOOL_NAME_MAP`（轻微重复）。

### Approach B: 抽共享 tool-status 模块（ideal architecture）
Summary: 名归一化 + hint 提取抽成 omp+pi 共用模块。
Effort: L  Risk: Med
Pros: 长期无重复。
Cons: 要改 omp（回归面更大），拖慢交付；`_extract_hint_data` 本已共享，去重边际收益有限。

**Selected: A** — 快速补齐 pi UX 短板，小 blast radius；B 待真有第三个需求再做。

## RISKS

- **流式事件 arguments 时序**：pi `toolcall_start` 时 `arguments` 可能为空。缓解：**不依赖 ui label-backfill**，PiRunner 内按 tool-call id 缓存，首次拿到 arguments（start 或 end）才 emit 一次富载条目；已知小代价：个别工具状态可能在其 end 时才出现（pi 调用快，可接受）。
- **never-raises 热路径**：Item 1/3 提取/读取在热路径。缓解：包 try/except，Item 1 失败降级为裸工具名（现状），Item 3 失败降级为不注入 memory 段；绝不冒泡（参考热路径 never-raises 须 catch Exception 的既有约束）。
- **per-session memory 读写错位**：bridge 只读（与 session_journal.read 同为无锁读），pi 独占写且同 scope turn 串行 → 无双写竞争；读到 pi 写一半的文件仅影响当 turn 注入（try/except 兜底），不破坏持久状态。
- **memory 跨实例**：root = `bridge_home()/...`，honors `FEISHU_BRIDGE_BG_HOME` → 多实例天然隔离（与 session_journal 一致）。

## Rollback

三项功能相互隔离，可独立回退（每项一个开关/单点）：
- **Item 2**：`/new` 恢复为 clear-only（移除活跃检测分支），现有文件无影响。
- **Item 1**：PiRunner 恢复 emit 裸工具名字符串（ui 已容错 str，退回现状显示）。
- **Item 3**：关闭 memory 注入（PiRunner `_build_system_prompt` 跳过该段），已存 memory 文件原样保留不删。
- ui/`_extract_hint_data` 新增的 `Ls` 分支对其它 runner 无副作用（纯增量 key），无需回退。

## Decision Log

| Date | Decision | Reason |
|------|----------|--------|
| 2026-06-01 | 选 Approach A（PiRunner 自包含） | 小 blast radius，不碰已 abandon 但代码在跑的 omp；hint 提取已共享 |
| 2026-06-01 | 范围排除 pi 压缩 | pi 0.78.0 CLI 无压缩入口，子进程拿不到 SDK `ctx.compact()` |
| 2026-06-01 | 范围排除出站附件 | 能力已存在（feishu-cli + feishu_send.py），仅 ergonomic |
| 2026-06-01 | memory 所有权：pi 独占写 + bridge 只读 | plan-review CRITICAL：避免 pi 写 + bridge prune 的无主文件竞争；贴合 pure-conduit |
| 2026-06-01 | memory scope 含 thread_id | plan-review HIGH：与 session_journal/cancel key 一致，防跨 thread 串记忆 |
| 2026-06-01 | 工具状态按 tool-call id 关联、单次 emit | plan-review HIGH：消除两事件族重复 + 避免 ui label-backfill 错配 |
| 2026-06-01 | `/new` 活跃检测走 tag-based cancel 路径 | plan-review HIGH：覆盖首个 `--no-session` 无 old_sid 的活跃 turn |
| 2026-06-01 | `/new` 新行为通用于所有支持 cancel 的 runner | plan-review MEDIUM：消除 proposal/design 范围不一致 |
| 2026-06-01 | **Item 2 砍掉**：`/new` 中止活跃 turn 已存在 | 实现中发现 main.py:1575-1581 已 `cancel(tag)`+`drain`；我与 Codex 均漏看（锚定 commands.py，未追 main.py 入队前路径）。Codex #6 一并作废 |
