"""Bridge command handlers for Feishu bridge."""

import datetime
import logging
import threading

from feishu_bridge.api.client import FeishuAPIError

from feishu_bridge.ui import ResponseHandle, add_queued_reaction
from feishu_bridge.runtime import SessionMap

log = logging.getLogger("feishu-bridge")


class BridgeCommandHandler:
    """Handle bridge-level and Feishu service commands for a bot instance."""

    def __init__(self, bot):
        self.bot = bot

    def handle_bridge_command(self, item: dict):
        """Handle bridge-level commands (not sent to Claude)."""
        cmd = item["_bridge_command"]
        arg = item.get("_cmd_arg", "")
        handle = ResponseHandle(
            self.bot.lark_client, item["chat_id"],
            item.get("thread_id"), item.get("message_id"),
        )

        if cmd == "new":
            key = (item["bot_id"], item["chat_id"], item.get("thread_id"))
            old_sid = self.bot.session_map.get(key)
            if old_sid:
                self.bot.session_map.delete(key)
                log.info("Session cleared: %s", old_sid[:8])
            handle.deliver("会话已重置，下一条消息将开始新对话。")

        elif cmd == "stop":
            parts = arg.split("|")
            cancelled = parts[0] == "1"
            drained_count = int(parts[1]) if len(parts) > 1 else 0
            if drained_count > 0:
                handle.deliver(f"{drained_count} 条排队消息已清除。")
            elif not cancelled:
                handle.deliver("当前没有正在执行的任务。")

        elif cmd == "help":
            handle.deliver(
                "**Bridge 命令**\n"
                "`/new` `/clear` `/reset` — 重置会话（清除上下文）\n"
                "`/stop` `/cancel` — 取消当前任务（排队消息继续处理）\n"
                "`/stop all` — 取消当前任务并清空所有排队消息\n"
                "`/compact [指示]` — 压缩当前会话上下文\n"
                "`/model [模型名]` — 查看或切换模型\n"
                "`/cost` — 查看当前会话 token 用量\n"
                "`/context` — 查看当前 context 使用率\n"
                "`/feishu-tasks [命令]` — 飞书任务管理（list/get/subtasks/add-subtask）\n"
                "`/feishu-doc` — 云文档读写（Markdown）\n"
                "`/feishu-sheet` — 电子表格读写\n"
                "`/feishu-bitable` — 多维表格操作\n"
                "`/restart` — 重启 Bridge 进程\n"
                "`/help` — 显示本帮助\n\n"
                "飞书命令首次使用需授权（自动弹出授权卡片）\n\n"
                "**Skill 命令**（透传给 Claude）\n"
                "`/plan` `/done` `/save` `/social-feed` 等已注册 skill 正常使用"
            )

        elif cmd == "compact":
            key = (item["bot_id"], item["chat_id"], item.get("thread_id"))
            tag = SessionMap._key_str(key)
            prompt = f"/compact {arg}" if arg else "/compact"
            sid = self.bot.session_map.get(key)
            if not sid:
                handle.deliver("当前没有活跃会话，无需压缩。")
                return
            result = self.bot.runner.run(prompt, session_id=sid, resume=True, tag=tag)
            if not result["is_error"]:
                new_sid = result.get("session_id") or sid
                if new_sid != sid:
                    self.bot.session_map.put(key, new_sid)
            if result["is_error"]:
                handle.deliver(result["result"], is_error=True)
            else:
                handle.deliver("上下文已压缩。")

        elif cmd == "model":
            if not arg:
                handle.deliver(f"当前模型: `{self.bot.runner.model}`")
            elif arg in ("opus", "claude-opus-4-6"):
                self.bot.runner.model = "claude-opus-4-6"
                handle.deliver("模型已切换为 `claude-opus-4-6`")
            elif arg in ("sonnet", "claude-sonnet-4-6"):
                self.bot.runner.model = "claude-sonnet-4-6"
                handle.deliver("模型已切换为 `claude-sonnet-4-6`")
            elif arg in ("haiku", "claude-haiku-4-5"):
                self.bot.runner.model = "claude-haiku-4-5"
                handle.deliver("模型已切换为 `claude-haiku-4-5`")
            else:
                handle.deliver(f"未知模型: `{arg}`\n\n可选: `opus` / `sonnet` / `haiku`")

        elif cmd == "cost":
            key = (item["bot_id"], item["chat_id"], item.get("thread_id"))
            sid = self.bot.session_map.get(key)
            if not sid:
                handle.deliver("当前没有活跃会话。")
                return
            cost_info = self.bot._session_cost.get(sid)
            if cost_info:
                usage = cost_info.get("usage", {})
                model_usage = cost_info.get("model_usage", {})
                total_cost = cost_info.get("total_cost_usd", 0)
                inp = usage.get("input_tokens", 0)
                out = usage.get("output_tokens", 0)
                cache_read = usage.get("cache_read_input_tokens", 0)
                cache_create = usage.get("cache_creation_input_tokens", 0)
                lines = [
                    "**会话用量（最近一次调用）**",
                    f"输入: {inp:,} tokens",
                    f"  cache read: {cache_read:,} / cache create: {cache_create:,}",
                    f"输出: {out:,} tokens",
                    f"费用: ${total_cost:.4f}",
                ]
                if model_usage:
                    lines.append("")
                    for model, mu in model_usage.items():
                        lines.append(
                            f"  {model}: in={mu.get('inputTokens', 0):,} out={mu.get('outputTokens', 0):,}"
                        )
                handle.deliver("\n".join(lines))
            else:
                handle.deliver("暂无用量数据（首次消息后可用）。")


        elif cmd == "context":
            key = (item["bot_id"], item["chat_id"], item.get("thread_id"))
            sid = self.bot.session_map.get(key)
            if not sid:
                handle.deliver("当前没有活跃会话。")
                return
            cost_info = self.bot._session_cost.get(sid)
            if not cost_info or not cost_info.get("usage"):
                handle.deliver("暂无 context 数据（首次消息后可用）。")
                return
            usage = cost_info["usage"]
            inp = usage.get("input_tokens", 0)
            cache_read = usage.get("cache_read_input_tokens", 0)
            max_ctx = 1_000_000
            compact_pct = 85
            pct = inp / max_ctx * 100 if max_ctx else 0
            filled = int(pct / 5)
            bar = "\u2593" * filled + "\u2591" * (20 - filled)
            lines = [
                "**Context 使用率**",
                f"`{bar}` {pct:.1f}%",
                f"{inp:,} / {max_ctx:,} tokens",
            ]
            if cache_read:
                cache_pct = cache_read / inp * 100 if inp else 0
                lines.append(f"cache hit: {cache_read:,} ({cache_pct:.0f}%)")
            if pct >= compact_pct:
                lines.append(f"\u26a0\ufe0f 已超过 {compact_pct}% 阈值，即将自动压缩")
            elif pct >= compact_pct * 0.8:
                lines.append(f"\U0001f4a1 接近 {compact_pct}% 自动压缩线，可考虑 `/compact`")
            handle.deliver("\n".join(lines))

        elif cmd == "feishu-tasks":
            self._handle_feishu_tasks(item, handle)

        elif cmd == "feishu-doc":
            self._handle_feishu_service(item, handle, "doc")

        elif cmd == "feishu-sheet":
            self._handle_feishu_service(item, handle, "sheet")

        elif cmd == "feishu-bitable":
            self._handle_feishu_service(item, handle, "bitable")

    def dispatch_task_command(self, arg: str, chat_id: str, sender_id: str) -> str:
        """Parse and dispatch /feishu-tasks sub-commands."""
        parts = arg.split(None, 1)
        action = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if action == "list":
            return self._task_list(rest, chat_id, sender_id)
        if action == "get":
            return self._task_get(rest, chat_id, sender_id)
        if action in ("subtasks", "subtask"):
            return self._task_subtasks(rest, chat_id, sender_id)
        if action in ("add-subtask", "create-subtask"):
            return self._task_add_subtask(rest, chat_id, sender_id)
        if action == "complete":
            return self._task_complete(rest, chat_id, sender_id)
        if action == "help":
            return self._task_help()
        return self._task_help()

    def _handle_feishu_tasks(self, item: dict, handle):
        if not self.bot.feishu_tasks:
            handle.deliver("飞书 API 服务不可用（缺少依赖模块）。")
            return
        sender_id = item.get("sender_id")
        if not sender_id:
            handle.deliver("无法获取用户身份，请重试。")
            return
        task_arg = item.get("_cmd_arg", "").strip()
        chat_id = item["chat_id"]

        def _do_tasks():
            try:
                if not task_arg:
                    result = self.bot.feishu_tasks.summary(chat_id, sender_id)
                else:
                    result = self.dispatch_task_command(task_arg, chat_id, sender_id)
                handle.deliver(result)
            except Exception as e:
                log.exception("Task API error")
                if isinstance(e, FeishuAPIError):
                    handle.deliver(f"飞书 API 错误 ({e.code}): {e.msg}", is_error=True)
                else:
                    handle.deliver("任务操作失败，请稍后重试。", is_error=True)

        threading.Thread(target=_do_tasks, daemon=True, name="feishu-tasks-handler").start()

    def _task_list(self, rest: str, chat_id: str, sender_id: str) -> str:
        completed = None
        if rest.lower() in ("completed", "done"):
            completed = True
        elif rest.lower() in ("active", "pending", ""):
            completed = False if rest else None

        tasks_result = self.bot.feishu_tasks.list_all_tasks_result(
            chat_id, sender_id, completed=completed)
        error = tasks_result.get("error")
        if error == "auth_failed":
            return self.bot.feishu_tasks._auth_failed_message()
        if error:
            return "获取任务列表失败，请稍后重试。"

        tasks = tasks_result["items"]
        if not tasks:
            return "📌 没有找到任务。"

        lines = [f"📌 **任务列表** ({len(tasks)} 个)\n"]
        for t in tasks[:30]:
            summary_text = t.get("summary", "无标题")
            guid = t.get("guid", "")
            status = "✅" if t.get("completed_at") else "📌"
            due = t.get("due")
            due_str = ""
            if due and due.get("timestamp"):
                ts = int(str(due["timestamp"])[:10])
                due_str = f" — 截止 {datetime.datetime.fromtimestamp(ts).strftime('%m/%d')}"
            members = t.get("members", [])
            assignee_str = ""
            if members:
                names = [m.get("name", "?") for m in members if m.get("role") == "assignee"]
                if names:
                    assignee_str = f" [{', '.join(names)}]"
            lines.append(f"{status} {summary_text}{due_str}{assignee_str}\n   `{guid}`")
        if len(tasks) > 30:
            lines.append(f"…还有 {len(tasks) - 30} 个任务")
        if tasks_result.get("truncated"):
            lines.append("…任务结果过多，仅显示搜索上限内的部分任务")
        return "\n".join(lines)

    def _task_get(self, rest: str, chat_id: str, sender_id: str) -> str:
        guid = rest.strip()
        if not guid:
            return "用法: `/feishu-tasks get <task_guid>`"
        data = self.bot.feishu_tasks.get_task(chat_id, sender_id, guid)
        if "error" in data:
            return self.bot.feishu_tasks._auth_failed_message()
        task = data.get("task", data)
        return self._format_task_detail(task)

    def _task_subtasks(self, rest: str, chat_id: str, sender_id: str) -> str:
        parts = rest.split(None, 1)
        if parts and parts[0].lower() == "list":
            guid = parts[1].strip() if len(parts) > 1 else ""
        elif parts and parts[0].lower() == "create":
            return self._task_add_subtask(parts[1].strip() if len(parts) > 1 else "",
                                          chat_id, sender_id)
        else:
            guid = rest.strip()

        if not guid:
            return "用法: `/feishu-tasks subtasks <parent_task_guid>`"

        data = self.bot.feishu_tasks.list_subtasks(chat_id, sender_id, guid)
        if "error" in data:
            return self.bot.feishu_tasks._auth_failed_message()
        items = data.get("items", [])
        if not items:
            return f"该任务没有子任务。\n父任务: `{guid}`"

        lines = [f"📋 **子任务** ({len(items)} 个)\n"]
        for t in items:
            summary_text = t.get("summary", "无标题")
            sub_guid = t.get("guid", "")
            status = "✅" if t.get("completed_at") else "📌"
            due = t.get("due")
            due_str = ""
            if due and due.get("timestamp"):
                ts = int(str(due["timestamp"])[:10])
                due_str = f" — 截止 {datetime.datetime.fromtimestamp(ts).strftime('%m/%d')}"
            members = t.get("members", [])
            assignee_str = ""
            if members:
                names = [m.get("name", "?") for m in members if m.get("role") == "assignee"]
                if names:
                    assignee_str = f" [{', '.join(names)}]"
            lines.append(f"{status} {summary_text}{due_str}{assignee_str}\n   `{sub_guid}`")
        return "\n".join(lines)

    def _task_add_subtask(self, rest: str, chat_id: str, sender_id: str) -> str:
        parts = rest.split(None, 1)
        if len(parts) < 2:
            return "用法: `/feishu-tasks add-subtask <parent_guid> <标题>`"
        parent_guid, title = parts
        data = self.bot.feishu_tasks.create_subtask(chat_id, sender_id, parent_guid, title)
        if "error" in data:
            return self.bot.feishu_tasks._auth_failed_message()
        task_obj = data.get("task", data)
        return f"✅ 子任务已创建: {title}\n   `{task_obj.get('guid', '?')}`"

    def _format_task_detail(self, task: dict) -> str:
        lines = []
        summary_text = task.get("summary", "无标题")
        guid = task.get("guid", "?")
        status = "✅ 已完成" if task.get("completed_at") else "📌 进行中"
        lines.append(f"**{summary_text}**")
        lines.append(f"状态: {status}")
        lines.append(f"GUID: `{guid}`")

        desc = task.get("description", "")
        if desc:
            lines.append(f"描述: {desc[:200]}")

        due = task.get("due")
        if due and due.get("timestamp"):
            ts = int(str(due["timestamp"])[:10])
            due_dt = datetime.datetime.fromtimestamp(ts)
            lines.append(f"截止: {due_dt.strftime('%Y-%m-%d %H:%M')}")

        members = task.get("members", [])
        if members:
            for m in members:
                lines.append(f"  {m.get('role', '?')}: {m.get('name', m.get('id', '?'))}")

        subtask_count = task.get("subtask_count", 0)
        if subtask_count:
            lines.append(f"子任务: {subtask_count} 个 (`/feishu-tasks subtasks {guid}`)")
        return "\n".join(lines)

    def _task_complete(self, rest: str, chat_id: str, sender_id: str) -> str:
        guid = rest.strip()
        if not guid:
            return "用法: `/feishu-tasks complete <task_guid>`"
        try:
            data = self.bot.feishu_tasks.get_task(chat_id, sender_id, guid)
        except FeishuAPIError as e:
            if e.code == 404 or "not found" in str(e.msg).lower():
                return "未找到此任务，请检查 GUID 是否正确。"
            if e.code == 403:
                return "无权访问此任务，请确认任务权限。"
            if e.code == 429:
                return "飞书 API 请求频繁，请稍后重试。"
            log.exception("get_task failed in complete for %s", guid)
            return "获取任务状态失败，请稍后重试。"
        if "error" in data:
            return self.bot.feishu_tasks._auth_failed_message()
        task = data.get("task", data)
        if task.get("completed_at"):
            return f"✅ 任务已完成: **{task.get('summary', '?')}**"
        try:
            result = self.bot.feishu_tasks.complete_task(chat_id, sender_id, guid)
            if "error" in result:
                return self.bot.feishu_tasks._auth_failed_message()
            return f"✅ 任务已标记完成: **{task.get('summary', '?')}**\n   `{guid}`"
        except FeishuAPIError as e:
            log.exception("complete_task failed for %s", guid)
            if e.code == 403:
                return "无权完成此任务，请确认任务权限。"
            if e.code == 429:
                return "飞书 API 请求频繁，请稍后重试。"
            return "标记任务完成失败，请稍后重试。"
        except Exception:
            log.exception("complete_task unexpected error for %s", guid)
            return "标记任务完成失败，请稍后重试。"

    def _task_help(self) -> str:
        return (
            "**任务命令**\n"
            "`/feishu-tasks` — 任务概览\n"
            "`/feishu-tasks list [active|completed]` — 列出任务（含 GUID）\n"
            "`/feishu-tasks get <guid>` — 查看任务详情\n"
            "`/feishu-tasks subtasks <guid>` — 列出子任务\n"
            "`/feishu-tasks add-subtask <guid> <标题>` — 创建子任务\n"
            "`/feishu-tasks complete <guid>` — 标记任务完成\n"
            "`/feishu-tasks help` — 显示本帮助"
        )

    def _handle_feishu_service(self, item: dict, handle, service: str):
        service_attr = {
            "doc": "feishu_docs",
            "sheet": "feishu_sheets",
            "bitable": "feishu_bitable",
        }.get(service)
        if not service_attr or not getattr(self.bot, service_attr, None):
            handle.deliver("飞书 API 服务不可用（缺少依赖模块）。")
            return
        sender_id = item.get("sender_id")
        if not sender_id:
            handle.deliver("无法获取用户身份，请重试。")
            return

        arg = item.get("_cmd_arg", "").strip()
        chat_id = item["chat_id"]

        def _do():
            try:
                result = self.dispatch_feishu_service(service, arg, chat_id, sender_id)
                handle.deliver(result)
            except Exception as e:
                log.exception("Feishu %s error", service)
                if isinstance(e, FeishuAPIError):
                    handle.deliver(f"飞书 API 错误 ({e.code}): {e.msg}", is_error=True)
                else:
                    handle.deliver(f"飞书{service}操作失败，请稍后重试。", is_error=True)

        threading.Thread(target=_do, daemon=True, name=f"feishu-{service}").start()

    def dispatch_feishu_service(self, service: str, arg: str,
                                chat_id: str, sender_id: str) -> str:
        if not arg:
            return self.feishu_service_help(service)

        parts = arg.split(None, 1)
        action = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if service == "doc":
            return self._handle_doc(action, rest, chat_id, sender_id)
        if service == "sheet":
            return self._handle_sheet(action, rest, chat_id, sender_id)
        if service == "bitable":
            return self._handle_bitable(action, rest, chat_id, sender_id)
        return f"未知服务: {service}"

    def _handle_doc(self, action: str, rest: str, chat_id: str, sender_id: str) -> str:
        if action in ("read", "fetch", "get"):
            if not rest:
                return "用法: `/feishu-doc read <doc_id或URL>`"
            result = self.bot.feishu_docs.fetch(chat_id, sender_id, doc_id=rest)
            if result is None:
                return self.bot.feishu_docs._auth_failed_message()
            title = result.get("title", "")
            md = result.get("markdown", result.get("text", str(result)))
            header = f"**{title}**\n\n" if title else ""
            max_chars = 4000
            if len(md) > max_chars:
                md = md[:max_chars] + f"\n\n…（已截断，共 {len(md)} 字符。使用 `/feishu-doc read <id>` 的 offset/limit 参数获取完整内容）"
            return f"{header}{md}"

        if action in ("write", "update"):
            sub_parts = rest.split(None, 1)
            if len(sub_parts) < 2:
                return "用法: `/feishu-doc write <doc_id> <markdown内容>`"
            doc_id, markdown = sub_parts
            result = self.bot.feishu_docs.update(chat_id, sender_id, doc_id=doc_id, markdown=markdown)
            if result is None:
                return self.bot.feishu_docs._auth_failed_message()
            return "文档已更新。"

        if action == "create":
            sub_parts = rest.split(None, 1)
            if not sub_parts:
                return "用法: `/feishu-doc create <标题> [内容]`"
            title = sub_parts[0]
            markdown = sub_parts[1] if len(sub_parts) > 1 else ""
            result = self.bot.feishu_docs.create(chat_id, sender_id, title=title, markdown=markdown)
            if result is None:
                return self.bot.feishu_docs._auth_failed_message()
            return f"文档已创建: {result}"

        return self.feishu_service_help("doc")

    def _handle_sheet(self, action: str, rest: str, chat_id: str, sender_id: str) -> str:
        if action == "info":
            if not rest:
                return "用法: `/feishu-sheet info <spreadsheet_token>`"
            result = self.bot.feishu_sheets.info(chat_id, sender_id, rest.strip())
            if result is None:
                return self.bot.feishu_sheets._auth_failed_message()
            ss = result.get("spreadsheet", {})
            sheets = result.get("sheets", [])
            lines = [f"**{ss.get('title', '未知')}**", ""]
            for s in sheets:
                lines.append(f"  • {s.get('title', '?')} ({s.get('grid_properties', {}).get('row_count', '?')} 行)")
            return "\n".join(lines)

        if action == "read":
            sub_parts = rest.split(None, 1)
            if len(sub_parts) < 2:
                return "用法: `/feishu-sheet read <spreadsheet_token> <范围>`\n例: `/sheet read shtcnXXX Sheet1!A1:D10`"
            token_str, range_ = sub_parts
            result = self.bot.feishu_sheets.read(chat_id, sender_id, token_str, range_)
            if result is None:
                return self.bot.feishu_sheets._auth_failed_message()
            values = result.get("valueRange", {}).get("values", [])
            if not values:
                return "（空数据）"
            lines = [" | ".join(str(c) if c is not None else "" for c in row) for row in values[:50]]
            if len(values) > 50:
                lines.append(f"…还有 {len(values) - 50} 行")
            return "```\n" + "\n".join(lines) + "\n```"

        if action == "write":
            return "表格写入需要结构化数据，请通过 Claude 对话描述你要写入的内容，我会调用 Sheets API 执行。"

        return self.feishu_service_help("sheet")

    def _handle_bitable(self, action: str, rest: str, chat_id: str, sender_id: str) -> str:
        if action == "info":
            if not rest:
                return "用法: `/feishu-bitable info <app_token>`"
            result = self.bot.feishu_bitable.get_app(chat_id, sender_id, rest.strip())
            if result is None:
                return self.bot.feishu_bitable._auth_failed_message()
            app = result.get("app", result)
            lines = [f"**{app.get('name', '未知')}**"]
            try:
                tables = self.bot.feishu_bitable.list_tables(chat_id, sender_id, rest.strip())
            except Exception:
                tables = None
            if tables:
                for t in tables.get("items", []):
                    lines.append(f"  • {t.get('name', '?')} (`{t.get('table_id', '')[:8]}…`)")
            return "\n".join(lines)

        if action == "records":
            sub_parts = rest.split(None, 1)
            if len(sub_parts) < 2:
                return "用法: `/feishu-bitable records <app_token> <table_id>`"
            app_token, table_id = sub_parts[0], sub_parts[1].strip()
            result = self.bot.feishu_bitable.list_records(chat_id, sender_id, app_token, table_id)
            if result is None:
                return self.bot.feishu_bitable._auth_failed_message()
            items = result.get("items", [])
            total = result.get("total", len(items))
            if not items:
                return "（无记录）"
            lines = [f"共 {total} 条记录（显示前 {min(len(items), 10)} 条）：", ""]
            for r in items[:10]:
                fields = r.get("fields", {})
                lines.append(f"  • {' | '.join(f'{k}: {v}' for k, v in list(fields.items())[:5])}")
            return "\n".join(lines)

        if action == "fields":
            sub_parts = rest.split(None, 1)
            if len(sub_parts) < 2:
                return "用法: `/feishu-bitable fields <app_token> <table_id>`"
            app_token, table_id = sub_parts[0], sub_parts[1].strip()
            result = self.bot.feishu_bitable.list_fields(chat_id, sender_id, app_token, table_id)
            if result is None:
                return self.bot.feishu_bitable._auth_failed_message()
            lines = [f"共 {len(result.get('items', []))} 个字段："]
            for f in result.get("items", []):
                lines.append(f"  • {f.get('field_name', '?')} (type={f.get('type', '?')})")
            return "\n".join(lines)

        return self.feishu_service_help("bitable")

    @staticmethod
    def feishu_service_help(service: str) -> str:
        if service == "doc":
            return (
                "**云文档命令**\n"
                "`/feishu-doc read <doc_id或URL>` — 读取文档内容（Markdown）\n"
                "`/feishu-doc write <doc_id> <markdown>` — 覆写文档内容\n"
                "`/feishu-doc create <标题> [内容]` — 创建新文档"
            )
        if service == "sheet":
            return (
                "**电子表格命令**\n"
                "`/feishu-sheet info <token>` — 查看表格信息\n"
                "`/feishu-sheet read <token> <范围>` — 读取数据\n"
                "例: `/feishu-sheet read shtcnXXX Sheet1!A1:D10`"
            )
        if service == "bitable":
            return (
                "**多维表格命令**\n"
                "`/feishu-bitable info <app_token>` — 查看应用信息和表格列表\n"
                "`/feishu-bitable records <app_token> <table_id>` — 查看记录\n"
                "`/feishu-bitable fields <app_token> <table_id>` — 查看字段定义"
            )
        return "未知服务"

    def add_queued_reaction_to_item(self, item: dict, message_id: str):
        item["_queued_reaction_id"] = add_queued_reaction(self.bot.lark_client, message_id)

    def reply_queue_full(self, chat_id: str, thread_id, message_id: str):
        handle = ResponseHandle(self.bot.lark_client, chat_id, thread_id, message_id)
        handle.deliver("消息过多，请稍后再试。")
