# Summary: pi-runner-ux-v1

## 概要
对照官方 pi-chat 深度调研后，落地 feishu-bridge 的 pi 支持改进。3 项中 2 项实装、1 项确认已存在。已发布 v2026.06.01（PyPI + GitHub Release），live 验证通过。

## 变更内容
- **Item 1 工具状态卡片增强**（runtime_pi.py / ui.py / runtime.py）：`message_update.toolcall_*` 为唯一权威源、按 tool-call id 单次 emit `{name, hint_data}`、`tool_execution_*` 对状态 no-op；`_TOOL_NAME_MAP` 归一化 pi 小写工具名到 PascalCase，复用共享 `_extract_hint_data`；补 Ls 标签/basename + 单数 find 取值；畸形事件降级裸名（never-raises）。卡片从裸 `read` 重复 → `读取文件 README.md` 单次。
- **Item 3 per-session 持久记忆**（新建 pi_memory.py + worker.py）：scope=`sha1(bot:chat:thread)`，pi 独占写、bridge 只读注入（软 tail-cap，绝不写/prune），worker 经 `fresh_context` 每 turn 注入。
- **Item 2 /new 中止**：确认 main.py:1575-1581 早已 `cancel(tag)`+`drain`，砍掉。

## 关键决策
- Approach A（PiRunner 自包含，不碰 omp）；memory 改 pi 独占写/bridge 只读（plan-review CRITICAL，消除并发写竞争）；工具状态 toolcall_* 单源 id 关联（弃 ui label-backfill）；`/new` 用 `cancel(tag)` 返回值判活跃。
- 范围排除：pi 压缩（CLI 无入口）、出站附件（能力已存在）、群聊向身份提示（延后）。

## 影响范围
- 新增 `feishu_bridge/pi_memory.py`；改 runtime.py / runtime_pi.py / ui.py / worker.py / README.md；+17 单测（test_pi_runner.py / test_bridge.py）。全量 1060 passed。
- 流程：/plan → /plan-review(2轮 BLOCK→pass) → /tdd-workflow → /code-review(2轮 WARNING→Approve) → 3 commit → ship.sh 发布。
- Follow-up（超范围）：card.update 失败 fallback 新建卡时清理卡住的流式卡。
