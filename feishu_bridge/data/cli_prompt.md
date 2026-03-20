# Feishu CLI

You have access to Feishu (飞书) APIs via the `feishu-cli` tool.
Call it with `feishu-cli <command> [args]`.

## Available Commands

### Documents
- `read-doc --token <doc_token>` — Read document as Markdown
- `create-doc --title <title> [--markdown <content>] [--folder-token <folder>]`
- `update-doc --token <doc_token> --markdown <content> --mode <mode> [--selection <sel>] [--new-title <title>]`
  Update modes (prefer incremental over overwrite):
  - `overwrite` — 全量覆写（慎用：会丢失图片、评论等嵌入内容）
  - `append` — 尾部追加
  - `replace_range` — 替换 selection 匹配的内容
  - `replace_all` — 全文替换所有匹配（`--selection` 可选，用于限定替换范围）
  - `insert_before` — 在 selection 匹配位置前插入
  - `insert_after` — 在 selection 匹配位置后插入
  - `delete_range` — 删除 selection 匹配的内容（`--markdown` 需占位传入）
  Selection (`replace_range`/`insert_before`/`insert_after`/`delete_range` 必填):
  - 范围: `"开头内容...结尾内容"` — 匹配从开头到结尾（含中间），10-20 字符确保唯一
  - 精确: `"完整内容"` — 不含 `...` 时精确匹配
  - 转义: 内容含字面 `...` 时用 `\.\.\.`
  Mode selection guide: append new content → `append`; insert near a heading or anchor text → `insert_after`/`insert_before` with `--selection`; replace a section → `replace_range`; global find-replace → `replace_all`; rewrite entire doc → `overwrite` (only when no images/tables/comments at risk).
- `delete-doc --token <doc_token> --confirm <token_prefix>` ⚠️

### Spreadsheets
- `read-sheet --token <sheet_token> --range <A1_range>`
- `sheet-info --token <sheet_token>` — Get metadata + sheets list
- `write-sheet --token <sheet_token> --range <A1_range> --values '<json_2d_array>'`
- `append-sheet --token <sheet_token> --range <A1_range> --values '<json_2d_array>'`
- `create-sheet --title <title> [--folder-token <folder>]`
- `delete-sheet --token <sheet_token> --confirm <token_prefix>` ⚠️

### Wiki
- `list-wiki-spaces [--page-size N]`
- `list-wiki-nodes --space-id <id> [--parent-node-token <token>]`
- `get-wiki-node --token <node_token>` — Resolve wiki to doc/sheet/bitable
- `create-wiki-node --space-id <id> --title <title> [--obj-type doc|sheet]`
- `delete-wiki-node --space-id <id> --token <node_token> --confirm <token_prefix>` ⚠️

### Comments
- `list-comments --file-token <token> [--file-type docx] [--is-solved true|false]`
- `add-comment --file-token <token> --file-type <type> --content <text>`
- `reply-comment --file-token <token> --file-type <type> --comment-id <id> --content <text>`
- `resolve-comment --file-token <token> --file-type <type> --comment-id <id>`
- `delete-comment --file-token <token> --file-type <type> --comment-id <id> --confirm <id_prefix>` ⚠️

### Calendar
- `list-calendars`
- `list-events --calendar-id <id> --start-time <rfc3339> --end-time <rfc3339>`
- `get-event --calendar-id <id> --event-id <id>`
- `create-event --calendar-id <id> --summary <title> --start-time <rfc3339> --end-time <rfc3339> [--description <text>]`
- `update-event --calendar-id <id> --event-id <id> [--summary <title>] [--start-time <t>] [--end-time <t>]`
- `delete-event --calendar-id <id> --event-id <id> --confirm <id_prefix>` ⚠️
- `reply-event --calendar-id <id> --event-id <id> --status accept|decline|tentative`

### Search
- `search-docs --query <keyword> [--type doc|sheet|bitable]`
- `search-messages --query <keyword> [--chat-id <id>]`
- `list-messages --container-id <chat_id> [--start-time <unix_ts>] [--end-time <unix_ts>]`
- `list-files [--folder-token <token>]`

### Bitable (多维表格)
- `list-bitable-records --app-token <token> --table-id <id> [--filter <expr>]`
- `get-bitable-record --app-token <token> --table-id <id> --record-id <id>`
- `create-bitable-records --app-token <token> --table-id <id> --records '<json_array>'`
- `update-bitable-records --app-token <token> --table-id <id> --records '<json_array>'`
- `delete-bitable-records --app-token <token> --table-id <id> --record-ids '<json_array>' --confirm <id_prefix>` ⚠️
- `create-bitable-app --name <name> [--folder-token <folder>]`
- `create-bitable-table --app-token <token> --name <name>`
- `delete-bitable-table --app-token <token> --table-id <id> --confirm <id_prefix>` ⚠️
- `list-bitable-fields --app-token <token> --table-id <id>`


### Drive (云盘)
- `upload-file --file <local_path> [--folder-token <token>] [--file-name <name>]` — Upload a local file to Drive (max 20MB, default: root folder)
- `upload-url --url <source_url> [--folder-token <token>] [--file-name <name>]` — Download from URL and upload to Drive (max 20MB, default: root folder)

### Tasks (任务)
- `list-tasks [--completed true|false] [--page-size N]` — List tasks visible to user
- `get-task --guid <task_guid>` — Get task details by GUID
- `list-tasklists [--page-size N]` — List task lists
- `get-tasklist --guid <tasklist_guid>` — Get task list details
- `list-tasklist-tasks --guid <tasklist_guid> [--completed true|false]` — List tasks in a task list
- `create-task --summary <text> [--description <text>] [--due <unix_ts>] [--tasklist-guid <guid>]` — Create a new task
- `complete-task --guid <task_guid>` — Mark a task as completed
- `list-subtasks --guid <task_guid> [--page-size N]` — List subtasks of a task
- `create-subtask --parent-guid <guid> --summary <text> [--due <unix_ts>]` — Create a subtask
- `create-tasklist --name <name>` — Create a new task list
- `update-tasklist --guid <tasklist_guid> --name <new_name>` — Rename a task list
- `delete-tasklist --guid <tasklist_guid> --confirm <guid_prefix>` ⚠️
- `add-task-to-tasklist --task-guid <guid> --tasklist-guid <guid>` — Add a task to a list
- `remove-task-from-tasklist --task-guid <guid> --tasklist-guid <guid>` — Remove a task from a list

## Important Notes
- All output is JSON
- ⚠️ Delete commands require `--confirm <prefix>` matching the target token/ID prefix (safety guard)
- For multi-step operations (search → read → update), chain multiple calls
- Use `--help` on any command for full argument details
