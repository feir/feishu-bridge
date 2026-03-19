"""Worker pipeline for Feishu bridge message processing."""

import json
import logging
import os
import uuid

from feishu_bridge.parsers import download_image, fetch_quoted_message
from feishu_bridge.runtime import ClaudeRunner, SessionMap
from feishu_bridge.ui import ResponseHandle, remove_typing_indicator

log = logging.getLogger("feishu-bridge")


def format_task_detail_bridge(task: dict) -> str:
    """Format task detail for bridge-level display (completed tasks)."""
    import datetime as _dt

    lines = []
    lines.append(f"**{task.get('summary', '未命名任务')}**")
    lines.append("状态: ✅ 已完成")
    guid = task.get("guid", "?")
    lines.append(f"GUID: `{guid}`")
    desc = task.get("description", "")
    if desc:
        lines.append(f"描述: {desc[:200]}")
    due = task.get("due")
    if due and due.get("timestamp"):
        ts = int(str(due["timestamp"])[:10])
        due_dt = _dt.datetime.fromtimestamp(ts)
        lines.append(f"截止: {due_dt.strftime('%Y-%m-%d %H:%M')}")
    return "\n".join(lines)



def _write_auth_file(chat_id: str, sender_id: str, user_token: str) -> str:
    """Write a temporary auth file for feishu_cli.py.

    Returns the file path. Caller must clean up in finally block.
    """
    auth_path = f"/tmp/feishu_auth_{uuid.uuid4()}.json"
    fd = os.open(auth_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({
                "user_access_token": user_token,
                "chat_id": chat_id,
                "sender_id": sender_id,
            }, f)
    except BaseException:
        try:
            os.unlink(auth_path)
        except OSError:
            pass
        raise
    return auth_path


def process_message(
    item: dict,
    bot_config: dict,
    lark_client,
    session_map: SessionMap,
    runner: ClaudeRunner,
    feishu_tasks=None,
    feishu_docs=None,
    feishu_sheets=None,
    feishu_api_error_cls=None,
    response_handle_cls=ResponseHandle,
    download_image_fn=download_image,
    fetch_quoted_message_fn=fetch_quoted_message,
    remove_typing_indicator_fn=remove_typing_indicator,
    session_not_found_signatures=None,
):
    """Process a single message from the work queue."""
    bot_id = item["bot_id"]
    chat_id = item["chat_id"]
    thread_id = item.get("thread_id")
    parent_id = item.get("parent_id")
    text = item.get("text", "")
    image_key = item.get("image_key")
    message_id = item.get("message_id")

    key = (bot_id, chat_id, thread_id)
    tag = SessionMap._key_str(key)
    handle = response_handle_cls(lark_client, chat_id, thread_id, message_id)
    image_path = None
    auth_file_path = None
    session_not_found_signatures = session_not_found_signatures or []

    try:
        handle._runner = runner
        handle._runner_tag = tag

        if image_key and message_id:
            image_path = download_image_fn(
                lark_client, message_id, image_key, bot_config["workspace"]
            )
            if image_path:
                text = (
                    f"{text}\n\n[用户发送了一张图片，"
                    f"已保存到 {image_path}，请查看并回复]"
                )
            else:
                text = f"{text}\n\n[用户发送了一张图片，但下载失败]"

        should_fetch_quote = (
            (parent_id and not thread_id)
            or (parent_id and thread_id and not session_map.get(key))
        )
        if should_fetch_quote:
            quoted = fetch_quoted_message_fn(lark_client, parent_id)
            if quoted:
                st = quoted["sender_type"]
                sender_label = "Bot" if st == "app" else ("User" if st == "user" else "Unknown")
                quote_header = f"[引用消息 message_id={parent_id} from={sender_label}]"
                text = f"{quote_header}\n{quoted['content']}\n[/引用消息]\n\n{text}"

        # --- Auto-fetch Feishu doc/wiki/sheet content from URLs in message ---
        feishu_urls = item.get("_feishu_urls") or []
        if feishu_urls and (feishu_docs or feishu_sheets) and feishu_api_error_cls:
            import requests as _requests
            sender_id = item.get("sender_id", "")
            fetched_parts = []
            for url_type, url_token in feishu_urls:
                try:
                    if url_type == "wiki":
                        # Wiki links need node resolution first
                        if not feishu_docs:
                            continue
                        wiki_token = feishu_docs.get_token(chat_id, sender_id)
                        if not wiki_token:
                            fetched_parts.append(
                                f"[飞书wiki {url_token}: 需要授权，已发送授权卡片，请完成授权后重试]"
                            )
                            continue
                        try:
                            node_data = feishu_docs.request(
                                "GET", f"/open-apis/wiki/v2/spaces/get_node",
                                wiki_token, params={"token": url_token})
                            node = node_data.get("node", {})
                            obj_type = node.get("obj_type", "doc")
                            obj_token = node.get("obj_token", url_token)
                        except feishu_api_error_cls:
                            # Fallback: treat as doc
                            obj_type = "doc"
                            obj_token = url_token

                        if obj_type not in ("doc", "docx", "wiki", "sheet"):
                            fetched_parts.append(
                                f"[飞书wiki {url_token}: 不支持的节点类型 {obj_type}，请直接在飞书中打开]"
                            )
                            continue
                        if obj_type == "sheet":
                            # Redirect to sheets handler
                            if not feishu_sheets:
                                fetched_parts.append(f"[飞书表格 {url_token}: sheets API 不可用]")
                                continue
                            result = feishu_sheets.info(chat_id, sender_id, obj_token)
                            if result is None:
                                fetched_parts.append(
                                    f"[飞书表格 {url_token}: 需要授权，已发送授权卡片，请完成授权后重试]"
                                )
                                continue
                            spreadsheet = result.get("spreadsheet", result)
                            title = spreadsheet.get("title", url_token)
                            sheets_list = result.get("sheets", [])
                            sheet_names = [s.get("title", "?") for s in sheets_list[:10]]
                            fetched_parts.append(
                                f"[飞书表格: {title}]\n"
                                f"工作表: {', '.join(sheet_names)}\n"
                                f"(使用 `/feishu-sheet read {obj_token} <sheet>!<range>` 读取具体数据)\n"
                                f"[/飞书表格]"
                            )
                            continue
                        # doc/docx/wiki doc — fetch via MCP
                        result = feishu_docs.fetch(chat_id, sender_id, doc_id=obj_token)
                        if result is None:
                            fetched_parts.append(
                                f"[飞书文档 {url_token}: 需要授权，已发送授权卡片，请完成授权后重试]"
                            )
                            continue
                        title = result.get("title", "")
                        md = result.get("markdown", "")
                        if not md:
                            md = result.get("text", str(result))
                        if len(md) > 8000:
                            md = md[:8000] + "\n\n... (内容过长，已截断)"
                        header = f"[飞书文档: {title}]" if title else f"[飞书文档 {url_token}]"
                        fetched_parts.append(f"{header}\n{md}\n[/飞书文档]")
                        continue
                    if url_type in ("doc", "docx"):
                        if not feishu_docs:
                            continue
                        result = feishu_docs.fetch(chat_id, sender_id, doc_id=url_token)
                        if result is None:
                            fetched_parts.append(
                                f"[飞书文档 {url_token}: 需要授权，已发送授权卡片，请完成授权后重试]"
                            )
                            continue
                        title = result.get("title", "")
                        md = result.get("markdown", "")
                        if not md:
                            md = result.get("text", str(result))
                        # Truncate very large docs
                        if len(md) > 8000:
                            md = md[:8000] + "\n\n... (内容过长，已截断)"
                        header = f"[飞书文档: {title}]" if title else f"[飞书文档 {url_token}]"
                        fetched_parts.append(f"{header}\n{md}\n[/飞书文档]")
                    elif url_type == "sheets":
                        if not feishu_sheets:
                            continue
                        result = feishu_sheets.info(chat_id, sender_id, url_token)
                        if result is None:
                            fetched_parts.append(
                                f"[飞书表格 {url_token}: 需要授权，已发送授权卡片，请完成授权后重试]"
                            )
                            continue
                        spreadsheet = result.get("spreadsheet", result)
                        title = spreadsheet.get("title", url_token)
                        sheets_list = result.get("sheets", [])
                        sheet_names = [s.get("title", "?") for s in sheets_list[:10]]
                        fetched_parts.append(
                            f"[飞书表格: {title}]\n"
                            f"工作表: {', '.join(sheet_names)}\n"
                            f"(使用 `/feishu-sheet read {url_token} <sheet>!<range>` 读取具体数据)\n"
                            f"[/飞书表格]"
                        )
                    elif url_type == "base":
                        fetched_parts.append(
                            f"[飞书多维表格 {url_token}]\n"
                            f"(使用 `/feishu-bitable` 命令操作多维表格)\n"
                            f"[/飞书多维表格]"
                        )
                except feishu_api_error_cls as e:
                    log.warning("Auto-fetch feishu %s/%s failed: %s", url_type, url_token, e)
                    if e.code == 403 or "permission" in str(e.msg).lower():
                        fetched_parts.append(
                            f"[飞书{url_type} {url_token}: 无访问权限，请确认文档已对你开放权限]"
                        )
                    elif "no permission" in str(e.msg).lower() or "not found" in str(e.msg).lower():
                        fetched_parts.append(
                            f"[飞书{url_type} {url_token}: 文档不存在或无权限访问]"
                        )
                    else:
                        fetched_parts.append(
                            f"[飞书{url_type} {url_token}: 获取失败 — {e.msg}]"
                        )
                except (_requests.exceptions.Timeout, _requests.exceptions.ConnectionError):
                    fetched_parts.append(
                        f"[飞书{url_type} {url_token}: 网络超时，请稍后重试]"
                    )
                except Exception:
                    log.exception("Auto-fetch feishu %s/%s unexpected error", url_type, url_token)
                    fetched_parts.append(
                        f"[飞书{url_type} {url_token}: 获取失败]"
                    )
            if fetched_parts:
                context = "\n\n".join(fetched_parts)
                text = f"{context}\n\n{text}"
                log.info("Auto-fetched %d feishu URL(s) for chat %s", len(fetched_parts), chat_id)

        if not handle.send_processing_indicator():
            log.info("Skipping Claude invocation: message recalled (mid=%s)", message_id)
            return handle

        def on_stream(text_so_far):
            handle.stream_update(text_so_far)

        todo_task_id = item.get("_todo_task_id")
        if todo_task_id and not feishu_api_error_cls:
            log.debug(
                "todo auto-drive disabled: feishu_api_error_cls not provided (chat=%s)",
                chat_id,
            )
        if todo_task_id and feishu_tasks and feishu_api_error_cls:
            import requests as _requests

            sender_id = item.get("sender_id", "")
            try:
                data = feishu_tasks.get_task(chat_id, sender_id, todo_task_id)
            except feishu_api_error_cls as e:
                if e.code == 404 or "not found" in str(e.msg).lower():
                    fallback = feishu_tasks.find_task_by_id(
                        chat_id, sender_id, todo_task_id, completed=None
                    )
                    if fallback.get("error") == "auth_failed":
                        handle.deliver(feishu_tasks._auth_failed_message())
                        return handle
                    matched = fallback.get("task")
                    if not matched:
                        if fallback.get("truncated"):
                            handle.deliver(
                                "未能在搜索上限内定位此任务，请稍后重试，"
                                "或改用 `/feishu-tasks get <guid>` 直接查询。"
                            )
                            return handle
                        handle.deliver("无法找到此任务，请确认任务 ID 是否正确。")
                        return handle
                    data = {"task": matched}
                elif e.code == 403:
                    handle.deliver("无权访问此任务，请确认任务权限。")
                    return handle
                elif e.code == 429:
                    handle.deliver("飞书 API 请求频繁，请稍后重试。")
                    return handle
                else:
                    handle.deliver("获取任务详情失败，请稍后重试。")
                    return handle
            except (_requests.exceptions.Timeout, _requests.exceptions.ConnectionError):
                handle.deliver("获取任务详情失败（网络超时），请稍后重试。")
                return handle
            except Exception:
                log.exception("Unexpected error fetching task %s", todo_task_id)
                handle.deliver("获取任务详情失败，请稍后重试。")
                return handle

            if isinstance(data, dict) and "error" in data:
                handle.deliver(feishu_tasks._auth_failed_message())
                return handle

            task = data.get("task", data)
            completed_at = task.get("completed_at")
            if completed_at and str(completed_at) != "0":
                detail = format_task_detail_bridge(task)
                handle.deliver(f"该任务已完成：\n\n{detail}")
                return handle

            t_summary = task.get("summary", "未命名任务")
            t_guid = task.get("guid", todo_task_id)
            t_due = task.get("due")
            if t_due and t_due.get("timestamp"):
                import datetime as _dt

                ts = int(str(t_due["timestamp"])[:10])
                due_str = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            else:
                due_str = "无"
            t_desc = task.get("description", "") or ""
            if len(t_desc) > 500:
                t_desc = t_desc[:500] + "\u2026"
            t_desc = t_desc or "无"

            subtask_count = task.get("subtask_count", 0)
            subtask_lines = ""
            if subtask_count:
                try:
                    st_data = feishu_tasks.list_subtasks(chat_id, sender_id, t_guid)
                    if "error" not in st_data:
                        st_items = st_data.get("items", [])
                        parts = []
                        for st in st_items:
                            st_status = "\u2705" if st.get("completed_at") else "\u2b1c"
                            parts.append(f"  {st_status} {st.get('summary', '?')}")
                        subtask_lines = "\n".join(parts)
                except Exception:
                    log.warning("list_subtasks failed for %s", t_guid, exc_info=True)
                if not subtask_lines:
                    subtask_lines = f"  (共 {subtask_count} 个，获取失败)"

            prompt_parts = [
                "[飞书任务]",
                f"标题: {t_summary}",
                f"GUID: {t_guid}",
                f"截止: {due_str}",
                f"描述: {t_desc}",
            ]
            if subtask_count:
                prompt_parts.append(f"子任务 ({subtask_count} 个):")
                prompt_parts.append(subtask_lines)
            prompt_parts.append("")
            prompt_parts.append(
                "用户转发了上述飞书任务，请分析任务内容，"
                "告知用户你的推进计划，等用户确认后开始执行。"
                f"\n完成后提醒用户可执行 `/feishu-tasks complete {t_guid}` "
                "标记任务完成。"
            )
            text = "\n".join(prompt_parts)

        # --- Auth file for feishu_cli.py ---
        env_extra = None
        sender_id = item.get("sender_id", "")
        try:
            # Try to get user token for CLI auth
            if feishu_docs:
                cli_token = feishu_docs.get_token(chat_id, sender_id)
                if cli_token:
                    auth_file_path = _write_auth_file(chat_id, sender_id, cli_token)
                    env_extra = {
                        "FEISHU_AUTH_FILE": auth_file_path,
                        "FEISHU_BOT_NAME": bot_config.get("name", ""),
                    }
        except Exception:
            log.warning("Failed to create auth file for CLI", exc_info=True)

        existing_sid = session_map.get(key)
        if existing_sid:
            result = runner.run(
                text, session_id=existing_sid, resume=True, tag=tag,
                on_output=on_stream, env_extra=env_extra
            )
        else:
            new_sid = str(uuid.uuid4())
            result = runner.run(
                text, session_id=new_sid, resume=False, tag=tag,
                on_output=on_stream, env_extra=env_extra
            )

        if result.get("cancelled"):
            handle.deliver(result["result"])
            return handle

        effective_sid = None
        if result["is_error"] and existing_sid:
            err_text = (result.get("result") or "").lower()
            if any(sig in err_text for sig in session_not_found_signatures):
                log.warning("Session %s not found, auto-healing", existing_sid[:8])
                session_map.delete(key)
                retry_sid = str(uuid.uuid4())
                result = runner.run(
                    text, session_id=retry_sid, resume=False, tag=tag,
                    on_output=on_stream, env_extra=env_extra
                )
                if result.get("cancelled"):
                    handle.deliver(result["result"])
                    return handle
                effective_sid = (
                    result.get("session_id", retry_sid) if not result["is_error"] else None
                )
                if not result["is_error"]:
                    result["result"] = (
                        result.get("result", "") + "\n\n⚠️ 会话已重建，上下文已清除。"
                    )
                else:
                    result["result"] = (
                        result.get("result", "") + "\n\n⚠️ 会话重建失败，请稍后重试。"
                    )
            else:
                effective_sid = existing_sid
        elif result["is_error"]:
            effective_sid = None
        else:
            effective_sid = result.get("session_id") or existing_sid

        if effective_sid:
            session_map.put(key, effective_sid)
            cost_store = item.get("_cost_store")
            if cost_store is not None and (
                result.get("usage") or result.get("total_cost_usd")
            ):
                cost_store[effective_sid] = {
                    "usage": result.get("usage", {}),
                    "model_usage": result.get("modelUsage", {}),
                    "total_cost_usd": result.get("total_cost_usd", 0),
                }

        if not handle._terminated:
            handle.deliver(result["result"], is_error=result["is_error"])

        return handle

    except Exception as e:
        log.exception("Worker error for chat %s: %s", chat_id, e)
        if not handle._terminated:
            try:
                handle.deliver("内部错误，请稍后重试。如持续出现请联系管理员。", is_error=True)
            except Exception:
                log.exception("Failed to deliver error message")

    finally:
        if getattr(handle, "_card_fallback_timer", None):
            handle._card_fallback_timer.cancel()
            handle._card_fallback_timer = None
        if getattr(handle, "_typing_reaction_id", None) and handle.source_message_id:
            remove_typing_indicator_fn(
                handle.client, handle.source_message_id, handle._typing_reaction_id
            )
            handle._typing_reaction_id = None
        if auth_file_path:
            try:
                os.unlink(auth_file_path)
            except OSError:
                pass
        if image_path:
            try:
                os.unlink(image_path)
            except OSError:
                pass

    return handle
