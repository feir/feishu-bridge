"""Bridge command handlers for Feishu bridge."""

import datetime
import importlib.metadata
import json
import logging
import platform as _platform
import threading

from feishu_bridge.api.client import FeishuAPIError
from feishu_bridge.quota import WINDOW_LABELS, fetch_codex_quota
from feishu_bridge.ui import ResponseHandle, add_queued_reaction
from feishu_bridge.runtime import ClaudeRunner, SessionMap, feishu_cli_activated


def _get_install_info() -> tuple[str, str, str | None]:
    """Detect installation mode and platform.

    Returns (mode, platform, source_path):
        mode: "pypi" or "git"
        platform: "linux" or "macos"
        source_path: local source directory (git mode only)
    """
    plat = "macos" if _platform.system() == "Darwin" else "linux"
    try:
        dist = importlib.metadata.distribution("feishu-bridge")
        raw = dist.read_text("direct_url.json")
        if raw:
            info = json.loads(raw)
            if info.get("dir_info", {}).get("editable"):
                url = info.get("url", "")
                path = url.removeprefix("file://") if url.startswith("file://") else url
                return "git", plat, path
    except Exception:
        pass
    return "pypi", plat, None

log = logging.getLogger("feishu-bridge")


def _context_window_for_model(model: str) -> int:
    """Infer context window size from model name."""
    m = model.lower()
    if "opus" in m:
        return 1_000_000
    return 200_000


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
            feishu_cli_activated.pop(key, None)
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
            from feishu_bridge import __version__
            lines = [
                "**Bridge 命令**",
                "`/new` `/clear` `/reset` — 重置会话（清除上下文）",
                "`/stop` `/cancel` — 取消当前任务（排队消息继续处理）",
                "`/stop all` — 取消当前任务并清空所有排队消息",
            ]
            if self.bot.runner.supports_compact():
                lines.append("`/compact [指示]` — 压缩当前会话上下文")
            lines.extend([
                "`/btw <问题>` — 快速提问（不中断当前任务，基于当前上下文）",
                "`/model [模型名]` — 查看或切换模型",
                "`/status` — 查看会话状态（context / 费用 / 配额）",
                "`/update` — 检查并拉取最新版本（不重启）",
                "`/restart` — 重启当前 Bot 实例",
                "`/restart-all` — 重启所有 Bot 实例",
                "`/help` — 显示本帮助",
            ])
            # Version & upgrade info
            mode, plat, src_path = _get_install_info()
            bot_id = self.bot.bot_id
            if mode == "git":
                upgrade_cmd = f"cd {src_path} && git pull"
            else:
                upgrade_cmd = "pipx upgrade feishu-bridge"
            if plat == "macos":
                restart_hint = "或 `launchctl kickstart -k` 重启服务"
            else:
                restart_hint = (
                    f"或 `systemctl --user restart feishu-bridge@{bot_id}`"
                )
            lines.extend([
                "",
                f"**v{__version__}** ({mode}{'・macOS' if plat == 'macos' else ''})",
                f"升级: `{upgrade_cmd}` → `/restart`",
                restart_hint,
            ])
            handle.deliver("\n".join(lines))

        elif cmd == "btw":
            self._handle_btw(item, arg, handle)

        elif cmd == "update":
            self._handle_update(handle)

        elif cmd == "compact":
            if not self.bot.runner.supports_compact():
                handle.deliver("此 Agent 不支持 /compact 命令。")
                return
            key = (item["bot_id"], item["chat_id"], item.get("thread_id"))
            tag = SessionMap._key_str(key)
            prompt = f"/compact {arg}" if arg else "/compact"
            sid = self.bot.session_map.get(key)
            if not sid:
                handle.deliver("当前没有活跃会话，无需压缩。")
                return
            handle.send_processing_indicator()

            def on_stream(text_so_far):
                handle.stream_update(text_so_far)

            result = self.bot.runner.run(
                prompt, session_id=sid, resume=True, tag=tag,
                on_output=on_stream,
            )
            if not result["is_error"]:
                new_sid = result.get("session_id") or sid
                if new_sid != sid:
                    self.bot.session_map.put(key, new_sid)
            if result["is_error"]:
                handle.deliver(result["result"], is_error=True)
            else:
                handle.deliver("上下文已压缩。")

        elif cmd == "model":
            aliases = self.bot.runner.get_model_aliases()
            if not arg:
                alias_list = " / ".join(f"`{a}`" for a in aliases)
                handle.deliver(
                    f"当前模型: `{self.bot.runner.model}`\n可选: {alias_list}"
                )
            elif arg in aliases:
                self.bot.runner.model = aliases[arg]
                handle.deliver(f"模型已切换为 `{aliases[arg]}`")
            elif arg in aliases.values():
                self.bot.runner.model = arg
                handle.deliver(f"模型已切换为 `{arg}`")
            else:
                # Passthrough unknown model name (allows new models)
                self.bot.runner.model = arg
                handle.deliver(f"模型已设置为 `{arg}`（未识别的名称，将直接传递给 CLI）")

        elif cmd == "status":
            self._handle_status(item, handle)

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

    def _handle_update(self, handle):
        """Handle /update — check for new version and pull if available."""
        from feishu_bridge import __version__
        from feishu_bridge.updater import check_and_update, get_pending_version

        pv = get_pending_version()
        if pv:
            handle.deliver(
                f"v{pv} 已就绪（当前运行 v{__version__}），`/restart` 部署。")
            return

        handle.send_processing_indicator()
        result = check_and_update()
        status = result.get("status")
        if status == "updated":
            handle.deliver(
                f"已拉取新版本 v{result['version']}（当前运行 v{__version__}），"
                f"`/restart` 部署。")
        elif status == "up_to_date":
            handle.deliver(f"已是最新版本 v{__version__}。")
        else:
            handle.deliver(
                f"检查更新失败: {result.get('message', '未知错误')}", is_error=True)

    def _handle_btw(self, item: dict, arg: str, handle):
        """Handle /btw — side question via fork-session, no tools."""
        if not arg.strip():
            handle.deliver("用法: `/btw <问题>` — 在当前上下文中快速提问（不中断正在执行的任务）")
            return

        if not isinstance(self.bot.runner, ClaudeRunner):
            handle.deliver("当前 Agent 不支持 /btw 命令。")
            return

        key = (item["bot_id"], item["chat_id"], item.get("thread_id"))
        sid = self.bot.session_map.get(key)
        if not sid:
            handle.deliver("当前无活跃会话，请直接发送消息。")
            return

        try:
            result = self.bot.runner.run(
                arg.strip(), session_id=sid, resume=True,
                fork_session=True,
            )
        except Exception:
            log.exception("/btw runner.run failed")
            handle.deliver("[/btw] 调用失败，请稍后重试。", is_error=True)
            return

        if result.get("is_error"):
            handle.deliver(f"[/btw] {result['result']}", is_error=True)
        else:
            answer = result.get("result", "").strip()
            if not answer:
                answer = "（无回复）"
            handle.deliver(f"[/btw] {answer}")

    def _handle_status(self, item: dict, handle):
        """Unified /status: context + cost + quota in one view."""
        key = (item["bot_id"], item["chat_id"], item.get("thread_id"))
        sid = self.bot.session_map.get(key)
        if not sid:
            handle.deliver("当前没有活跃会话。")
            return
        cost_info = self.bot._session_cost.get(sid)
        if not cost_info:
            handle.deliver("暂无数据（首次消息后可用）。")
            return

        lines: list[str] = []

        # --- Section 1: Context ---
        usage = cost_info.get("last_call_usage") or cost_info.get("usage") or {}
        model_usage = cost_info.get("model_usage", {})
        inp = usage.get("input_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_create = usage.get("cache_creation_input_tokens", 0)
        total_ctx = inp + cache_read + cache_create

        max_ctx = self.bot.runner.get_default_context_window()
        model_name = self.bot.runner.model or ""
        for m, mu in model_usage.items():
            model_name = m
            # API value preferred, model inference as fallback
            cw = mu.get("contextWindow", 0)
            max_ctx = cw if cw > 0 else _context_window_for_model(m)
            break

        pct = total_ctx / max_ctx * 100 if max_ctx else 0
        filled = int(pct / 5)
        bar = "\u2593" * filled + "\u2591" * (20 - filled)

        # Context header with bar
        lines.append(f"**Context** `{bar}` **{pct:.0f}%**")
        meta_parts = [f"{total_ctx:,} / {max_ctx:,} tokens"]
        if model_name:
            meta_parts.append(model_name)
        lines.append(" · ".join(meta_parts))
        if cache_read and total_ctx:
            cache_pct = cache_read / total_ctx * 100
            lines.append(f"cache hit: {cache_read:,} ({cache_pct:.0f}%)")

        # Context warning
        compact_hint = " 或 `/compact`" if self.bot.runner.supports_compact() else ""
        if pct >= 85:
            lines.append(f"\U0001f534 建议 `/new`{compact_hint}")
        elif pct >= 70:
            lines.append(f"\U0001f7e1 接近上限{compact_hint}")

        # Determine subscription vs API mode early (needed for cost section)
        quota_poller = getattr(self.bot, "_quota_poller", None)
        snap = quota_poller.snapshot if quota_poller else None
        is_subscription = snap and snap.available

        # --- Section 2: Cost (API mode only, skip for subscription) ---
        if not is_subscription:
            turn_cost = cost_info.get("total_cost_usd")
            accumulated = cost_info.get("accumulated_cost_usd", turn_cost or 0)
            out_tokens = usage.get("output_tokens", 0)

            if turn_cost and turn_cost > 0:
                lines.append("")
                cost_parts = [f"本次 ${turn_cost:.4f}"]
                if accumulated and accumulated > 0:
                    cost_parts.append(f"累计 **${accumulated:.4f}**")
                lines.append("**费用** " + " · ".join(cost_parts))
                lines.append(f"in: {inp + cache_read + cache_create:,} · out: {out_tokens:,}")

        # --- Section 3: Claude quota ---
        import time as _time

        if snap and snap.available and not snap.stale:
            lines.append("")
            any_exhausted = any(
                w.utilization >= 100 for w in snap.windows.values()
            )
            status_icon = "\U0001f6ab" if any_exhausted else "\U0001f7e2"
            lines.append(f"**Claude** {status_icon}")
            for wkey, label in WINDOW_LABELS.items():
                w = snap.windows.get(wkey)
                if w is None:
                    continue
                remaining = max(0, w.resets_at_epoch - _time.time())
                hours, mins = divmod(int(remaining) // 60, 60)
                reset_str = f" 重置 {hours}h{mins:02d}m" if remaining > 0 else ""
                lines.append(f"- {label}: {w.utilization:.0f}%{reset_str}")
        else:
            # Fallback: stream event rate_limit_info
            rli = cost_info.get("rate_limit_info")
            if rli:
                status = rli.get("status", "")
                limit_type = rli.get("rateLimitType", "")
                label = "7d" if "seven_day" in limit_type else "5h"
                if status == "rejected":
                    resets_at = rli.get("resetsAt", 0)
                    remaining = max(0, resets_at - _time.time()) if resets_at else 0
                    hours, mins = divmod(int(remaining) // 60, 60)
                    reset_str = f" 重置 {hours}h{mins:02d}m" if remaining > 0 else ""
                    lines.append("")
                    lines.append(f"**Claude** \U0001f6ab")
                    lines.append(f"- {label}: 已用尽{reset_str}")
                else:
                    lines.append("")
                    lines.append(f"**Claude** \U0001f7e2")
                    lines.append(f"- {label}: {status}")

        # Cookie expiry warning
        if snap:
            cookie_warn = snap.cookie_expiry_warning
            if cookie_warn:
                lines.append(cookie_warn)

        # --- Section 4: Codex quota ---
        codex_snap = fetch_codex_quota()
        if codex_snap.available:
            lines.append("")
            plan = f" ({codex_snap.plan_type.capitalize()})" if codex_snap.plan_type else ""
            status_icon = "\U0001f7e2" if codex_snap.allowed else "\U0001f6ab"
            lines.append(f"**Codex{plan}** {status_icon}")
            # 5h window
            pu = codex_snap.primary_used_pct
            pr = max(0, codex_snap.primary_resets_at - _time.time())
            ph, pm = divmod(int(pr) // 60, 60)
            p_reset = f" 重置 {ph}h{pm:02d}m" if pr > 0 else ""
            lines.append(f"- 5h: {pu:.0f}%{p_reset}")
            # 7d window
            su = codex_snap.secondary_used_pct
            sr = max(0, codex_snap.secondary_resets_at - _time.time())
            sh, sm = divmod(int(sr) // 60, 60)
            s_reset = f" 重置 {sh}h{sm:02d}m" if sr > 0 else ""
            lines.append(f"- 7d: {su:.0f}%{s_reset}")

        handle.deliver("\n".join(lines))

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
                ts_raw = int(due["timestamp"])
                ts = ts_raw // 1000 if ts_raw > 9_999_999_999 else ts_raw
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
                ts_raw = int(due["timestamp"])
                ts = ts_raw // 1000 if ts_raw > 9_999_999_999 else ts_raw
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
            ts_raw = int(due["timestamp"])
            ts = ts_raw // 1000 if ts_raw > 9_999_999_999 else ts_raw
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
            return "表格写入需要结构化数据，请通过对话描述你要写入的内容，我会调用 Sheets API 执行。"

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
