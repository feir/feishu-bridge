"""
Feishu Task API v2 wrapper with auto-auth via Device Flow OAuth.

All Task API v2 endpoints require user_access_token (UAT).

Usage:
    tasks = FeishuTasks(app_id, app_secret, lark_client)
    result = tasks.list_tasks(chat_id, user_open_id)
    result = tasks.list_tasklists(chat_id, user_open_id)
    result = tasks.get_tasklist_tasks(chat_id, user_open_id, tasklist_guid)
"""

import datetime
import logging
import time

from feishu_bridge.api.client import FeishuAPI, FeishuAPIError

log = logging.getLogger("feishu-tasks")

DEFAULT_PAGE_SIZE = 50


class FeishuTasks(FeishuAPI):
    """Feishu Task API v2 with auto-auth."""

    SCOPES = ["task:task:read", "task:task:write",
              "task:tasklist:read", "task:tasklist:write",
              "task:section:read", "task:section:write"]
    BASE_PATH = "/open-apis/task/v2"

    # -------------------------------------------------------------------
    # Task endpoints
    # -------------------------------------------------------------------

    def list_tasks(self, chat_id: str, user_open_id: str,
                   completed: bool = None,
                   page_size: int = DEFAULT_PAGE_SIZE,
                   page_token: str = None) -> dict:
        """List tasks visible to the user."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return {"items": [], "has_more": False, "page_token": None,
                    "error": "auth_failed"}

        params = {"page_size": page_size, "user_id_type": "open_id"}
        if completed is not None:
            params["completed"] = str(completed).lower()
        if page_token:
            params["page_token"] = page_token

        return self.request("GET", "/tasks", token, params=params)

    def get_task(self, chat_id: str, user_open_id: str,
                 task_guid: str) -> dict:
        """Get a single task by GUID."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return {"error": "auth_failed"}
        return self.request("GET", f"/tasks/{task_guid}", token,
                            params={"user_id_type": "open_id"})

    def list_all_tasks_result(self, chat_id: str, user_open_id: str,
                              completed: bool = None,
                              max_pages: int = 10) -> dict:
        """List all tasks with pagination metadata and explicit error state."""
        all_items = []
        page_token = None

        for _ in range(max_pages):
            data = self.list_tasks(chat_id, user_open_id,
                                   completed=completed,
                                   page_token=page_token)
            if "error" in data:
                return {
                    "items": all_items,
                    "error": data["error"],
                    "truncated": False,
                }

            all_items.extend(data.get("items", []))
            if not data.get("has_more"):
                return {"items": all_items, "error": None, "truncated": False}

            page_token = data.get("page_token")
            if not page_token:
                break

        return {
            "items": all_items,
            "error": None,
            "truncated": bool(page_token),
        }

    def list_all_tasks(self, chat_id: str, user_open_id: str,
                       completed: bool = None,
                       max_pages: int = 10) -> list[dict]:
        """Backward-compatible list-only wrapper."""
        result = self.list_all_tasks_result(
            chat_id, user_open_id, completed=completed, max_pages=max_pages)
        return result["items"]

    def find_task_by_id(self, chat_id: str, user_open_id: str,
                        task_id: str, completed: bool = None,
                        max_pages: int = 50) -> dict:
        """Find a task by task_id or guid with explicit truncation/error state."""
        page_token = None

        for _ in range(max_pages):
            data = self.list_tasks(chat_id, user_open_id,
                                   completed=completed,
                                   page_token=page_token)
            if "error" in data:
                return {"task": None, "error": data["error"], "truncated": False}

            for item in data.get("items", []):
                if item.get("task_id") == task_id or item.get("guid") == task_id:
                    return {"task": item, "error": None, "truncated": False}

            if not data.get("has_more"):
                return {"task": None, "error": None, "truncated": False}

            page_token = data.get("page_token")
            if not page_token:
                break

        return {"task": None, "error": None, "truncated": bool(page_token)}

    # -------------------------------------------------------------------
    # TaskList endpoints
    # -------------------------------------------------------------------

    def list_tasklists(self, chat_id: str, user_open_id: str,
                       page_size: int = DEFAULT_PAGE_SIZE,
                       page_token: str = None) -> dict:
        """List task lists visible to the user."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return {"items": [], "has_more": False, "page_token": None,
                    "error": "auth_failed"}

        params = {"page_size": page_size, "user_id_type": "open_id"}
        if page_token:
            params["page_token"] = page_token

        return self.request("GET", "/tasklists", token, params=params)

    def get_tasklist(self, chat_id: str, user_open_id: str,
                     tasklist_guid: str) -> dict:
        """Get a single task list by GUID."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return {"error": "auth_failed"}
        return self.request("GET", f"/tasklists/{tasklist_guid}", token,
                            params={"user_id_type": "open_id"})

    def get_tasklist_tasks(self, chat_id: str, user_open_id: str,
                           tasklist_guid: str,
                           completed: bool = None,
                           page_size: int = DEFAULT_PAGE_SIZE,
                           page_token: str = None) -> dict:
        """List tasks within a specific task list."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return {"items": [], "has_more": False, "page_token": None,
                    "error": "auth_failed"}

        params = {"page_size": page_size, "user_id_type": "open_id"}
        if completed is not None:
            params["completed"] = str(completed).lower()
        if page_token:
            params["page_token"] = page_token

        return self.request("GET", f"/tasklists/{tasklist_guid}/tasks",
                            token, params=params)

    def complete_task(self, chat_id: str, user_open_id: str,
                      task_guid: str) -> dict:
        """Mark a task as completed (idempotent — already-completed tasks succeed)."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return {"error": "auth_failed"}
        try:
            return self.request("PATCH", f"/tasks/{task_guid}", token,
                                json_body={
                                    "task": {"completed_at": str(int(time.time() * 1000))},
                                    "update_fields": ["completed_at"],
                                })
        except FeishuAPIError as e:
            if e.code == 1470400:
                log.info("Task %s already completed", task_guid)
                return self.request("GET", f"/tasks/{task_guid}", token,
                                    params={"user_id_type": "open_id"})
            raise

    # -------------------------------------------------------------------
    # Subtask endpoints
    # -------------------------------------------------------------------

    def list_subtasks(self, chat_id: str, user_open_id: str,
                      task_guid: str,
                      page_size: int = DEFAULT_PAGE_SIZE,
                      page_token: str = None) -> dict:
        """List subtasks of a given task."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return {"items": [], "has_more": False, "page_token": None,
                    "error": "auth_failed"}

        params = {"page_size": page_size, "user_id_type": "open_id"}
        if page_token:
            params["page_token"] = page_token

        return self.request("GET", f"/tasks/{task_guid}/subtasks",
                            token, params=params)

    def create_subtask(self, chat_id: str, user_open_id: str,
                       parent_guid: str, summary: str,
                       description: str = None,
                       due_timestamp: str = None) -> dict:
        """Create a subtask under the given parent task.

        Args:
            parent_guid: GUID of the parent task.
            summary: Title of the new subtask.
            description: Optional description text.
            due_timestamp: Optional Unix timestamp in **milliseconds** as string.

        Returns:
            API response data (contains the created subtask object).
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return {"error": "auth_failed"}

        body = {"summary": summary}
        if description:
            body["description"] = description
        if due_timestamp:
            body["due"] = {"timestamp": due_timestamp}

        return self.request("POST", f"/tasks/{parent_guid}/subtasks",
                            token, json_body=body)


    def create_task(self, chat_id: str, user_open_id: str,
                    summary: str, description: str = None,
                    due_timestamp: str = None,
                    tasklist_guid: str = None,
                    section_guid: str = None) -> dict:
        """Create a new task.

        Args:
            summary: task title (max 3000 chars)
            description: optional description
            due_timestamp: optional Unix timestamp in **milliseconds** as string
            tasklist_guid: optional tasklist to add the task to
            section_guid: optional section within the tasklist

        Returns:
            API response (contains the created task object).
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return {"error": "auth_failed"}

        body = {"summary": summary}
        if description:
            body["description"] = description
        if due_timestamp:
            body["due"] = {"timestamp": due_timestamp}
        if tasklist_guid:
            tl_entry = {"tasklist_guid": tasklist_guid}
            if section_guid:
                tl_entry["section_guid"] = section_guid
            body["tasklists"] = [tl_entry]

        return self.request("POST", "/tasks", token,
                            params={"user_id_type": "open_id"},
                            json_body=body)

    def update_task(self, chat_id: str, user_open_id: str,
                    task_guid: str, *,
                    summary: str = None,
                    description: str = None,
                    due_timestamp: str = None,
                    completed_at: str = None) -> dict:
        """Update a task via PATCH.

        Args:
            task_guid: GUID of the task to update.
            summary: New title (optional).
            description: New description (optional).
            due_timestamp: New due date as Unix ms timestamp string (optional).
            completed_at: Completion timestamp in ms, or "0" to uncomplete (optional).

        Returns:
            API response (contains the updated task object).
        """
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return {"error": "auth_failed"}

        task_data = {}
        if summary is not None:
            task_data["summary"] = summary
        if description is not None:
            task_data["description"] = description
        if due_timestamp is not None:
            task_data["due"] = {"timestamp": due_timestamp}
        if completed_at is not None:
            task_data["completed_at"] = completed_at

        if not task_data:
            return {"error": "no fields to update"}

        # Split completed_at from other fields to avoid silent-drop on 1470400:
        # If completed_at is mixed with other fields and the task is already
        # completed, the API rejects the whole PATCH — losing the other updates.
        other_data = {k: v for k, v in task_data.items() if k != "completed_at"}

        # Step 1: apply non-completion fields first (if any)
        result = None
        if other_data:
            result = self.request("PATCH", f"/tasks/{task_guid}", token,
                                  params={"user_id_type": "open_id"},
                                  json_body={
                                      "task": other_data,
                                      "update_fields": list(other_data.keys()),
                                  })

        # Step 2: apply completed_at separately (idempotent)
        if completed_at is not None:
            try:
                result = self.request(
                    "PATCH", f"/tasks/{task_guid}", token,
                    params={"user_id_type": "open_id"},
                    json_body={
                        "task": {"completed_at": completed_at},
                        "update_fields": ["completed_at"],
                    })
            except FeishuAPIError as e:
                # 1470400: task already completed — treat as success
                if e.code == 1470400 and completed_at != "0":
                    log.info("Task %s already completed, treating as success",
                             task_guid)
                    if not result:
                        result = self.request(
                            "GET", f"/tasks/{task_guid}", token,
                            params={"user_id_type": "open_id"})
                else:
                    raise

        return result


    # -------------------------------------------------------------------
    # TaskList CRUD endpoints
    # -------------------------------------------------------------------

    def create_tasklist(self, chat_id: str, user_open_id: str,
                        name: str) -> dict:
        """Create a new task list."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return {"error": "auth_failed"}
        return self.request("POST", "/tasklists", token,
                            params={"user_id_type": "open_id"},
                            json_body={"name": name})

    def update_tasklist(self, chat_id: str, user_open_id: str,
                        tasklist_guid: str, name: str) -> dict:
        """Rename a task list."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return {"error": "auth_failed"}
        return self.request("PATCH", f"/tasklists/{tasklist_guid}", token,
                            params={"user_id_type": "open_id"},
                            json_body={
                                "tasklist": {"name": name},
                                "update_fields": ["name"],
                            })

    def delete_tasklist(self, chat_id: str, user_open_id: str,
                        tasklist_guid: str) -> dict:
        """Delete a task list."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return {"error": "auth_failed"}
        return self.request("DELETE", f"/tasklists/{tasklist_guid}", token,
                            params={"user_id_type": "open_id"})

    def add_task_to_tasklist(self, chat_id: str, user_open_id: str,
                             task_guid: str, tasklist_guid: str) -> dict:
        """Add an existing task to a task list."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return {"error": "auth_failed"}
        return self.request("POST", f"/tasks/{task_guid}/add_tasklist", token,
                            params={"user_id_type": "open_id"},
                            json_body={"tasklist_guid": tasklist_guid})

    def remove_task_from_tasklist(self, chat_id: str, user_open_id: str,
                                  task_guid: str, tasklist_guid: str) -> dict:
        """Remove a task from a task list."""
        token = self.get_token(chat_id, user_open_id)
        if not token:
            return {"error": "auth_failed"}
        return self.request("POST", f"/tasks/{task_guid}/remove_tasklist", token,
                            params={"user_id_type": "open_id"},
                            json_body={"tasklist_guid": tasklist_guid})

    # -------------------------------------------------------------------
    # Convenience
    # -------------------------------------------------------------------

    def summary(self, chat_id: str, user_open_id: str) -> str:
        """Human-readable summary of all tasks and lists."""
        lines = []

        tasklists = self.list_tasklists(chat_id, user_open_id)
        if "error" in tasklists:
            return self._auth_failed_message()

        tl_items = tasklists.get("items", [])
        if tl_items:
            lines.append(f"📋 **任务清单** ({len(tl_items)} 个)")
            for tl in tl_items:
                name = tl.get("name", "未命名清单")
                guid = tl.get("guid", "")
                lines.append(f"  • {name} (`{guid[:8]}…`)")
        else:
            lines.append("📋 没有找到任务清单")

        lines.append("")

        tasks_result = self.list_all_tasks_result(
            chat_id, user_open_id, completed=False)
        if tasks_result["error"]:
            return self._auth_failed_message()

        tasks = tasks_result["items"]
        if tasks:
            lines.append(f"📌 **待办任务** ({len(tasks)} 个)")
            for t in tasks[:20]:
                summary_text = t.get("summary", "无标题")
                due = t.get("due")
                due_str = ""
                if due and due.get("timestamp"):
                    ts_raw = int(due["timestamp"])
                    # Feishu Task API stores timestamps in milliseconds
                    ts = ts_raw // 1000 if ts_raw > 9_999_999_999 else ts_raw
                    due_str = f" — 截止 {datetime.datetime.fromtimestamp(ts).strftime('%m/%d')}"
                lines.append(f"  • {summary_text}{due_str}")
            if len(tasks) > 20:
                lines.append(f"  …还有 {len(tasks) - 20} 个任务")
            if tasks_result["truncated"]:
                lines.append("  …任务结果过多，仅显示搜索上限内的部分任务")
        else:
            lines.append("📌 没有待办任务")

        return "\n".join(lines)
