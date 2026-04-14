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
  - `delete_range` — 删除 selection 匹配的内容（无需 `--markdown`）
  Selection (`replace_range`/`insert_before`/`insert_after`/`delete_range` 必填):
  - 范围: `"开头内容...结尾内容"` — 匹配从开头到结尾（含中间），10-20 字符确保唯一
  - 精确: `"完整内容"` — 不含 `...` 时精确匹配
  - 转义: 内容含字面 `...` 时用 `\.\.\.`
  - `--selection-by-title "## 章节标题"` — 标题定位，自动选中整个章节
    （从标题到下一个同级或更高级标题），与 `--selection` 二选一
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
- `list-events --calendar-id <id> --start-time <rfc3339> --end-time <rfc3339> [--timezone <iana_tz>]`
- `get-event --calendar-id <id> --event-id <id>`
- `create-event --calendar-id <id> --summary <title> --start-time <rfc3339> --end-time <rfc3339> [--description <text>] [--timezone <iana_tz>]`
- `update-event --calendar-id <id> --event-id <id> [--summary <title>] [--start-time <t>] [--end-time <t>] [--timezone <iana_tz>]`
- `delete-event --calendar-id <id> --event-id <id> --confirm <id_prefix>` ⚠️
- `reply-event --calendar-id <id> --event-id <id> --status accept|decline|tentative`
- `list-event-instances --calendar-id <id> --event-id <id> --start-time <rfc3339> --end-time <rfc3339> [--timezone <iana_tz>]` — Expand recurring event instances (max 40-day window)
- `list-attendees --calendar-id <id> --event-id <id>` — List event attendees
- `create-attendees --calendar-id <id> --event-id <id> --attendees '<json_array>'` — Add attendees (user/resource/third_party)
- `delete-attendees --calendar-id <id> --event-id <id> --attendee-ids '<json_array>' --confirm <id_prefix>` ⚠️
- `list-freebusy --user-ids '<json_array>' --start-time <rfc3339> --end-time <rfc3339> [--timezone <iana_tz>]` — Query free/busy for 1-10 users
- Note: `--timezone` defaults to `Asia/Shanghai`. Only needed when time inputs lack timezone info (e.g. "2026-03-21 10:30"). RFC3339 with offset (e.g. "+08:00") ignores this flag.

### Search
- `search-docs --query <keyword> [--type doc|sheet|bitable]`
- `search-messages --query <keyword> [--chat-id <id>]`
- `list-messages --container-id <chat_id> [--start-time <unix_ts>] [--end-time <unix_ts>]`
- `read-message --message-id <id>` — Read a single message by ID
- `list-files [--folder-token <token>]`

### Bitable (多维表格)

**App**
- `get-bitable-app --app-token <token>` — Get app metadata
- `create-bitable-app --name <name> [--folder-token <folder>]`
- `copy-bitable-app --app-token <token> [--name <name>] [--folder-token <folder>]` — Copy a bitable

**Table**
- `list-bitable-tables --app-token <token>` — List tables in a bitable
- `create-bitable-table --app-token <token> --name <name>`
- `patch-bitable-table --app-token <token> --table-id <id> --name <new_name>` — Rename table
- `delete-bitable-table --app-token <token> --table-id <id> --confirm <id_prefix>` ⚠️

**Record**
- `list-bitable-records --app-token <token> --table-id <id> [--filter <expr>] [--sort '<json>'] [--field-names '<json>']`
  - `--filter`: JSON filter object. Operators: `is`, `isNot`, `contains`, `doesNotContain`, `isEmpty`, `isNotEmpty`, `isGreater`, `isLess`
    - Single: `'{"conjunction":"and","conditions":[{"field_name":"Status","operator":"is","value":["Done"]}]}'`
    - Multi: `'{"conjunction":"and","conditions":[{"field_name":"Priority","operator":"is","value":["High"]},{"field_name":"Status","operator":"isNot","value":["Done"]}]}'`
  - `--sort`: e.g. `'[{"field_name":"Created","desc":true}]'`
  - `--field-names`: e.g. `'["Name","Status"]'` — only return listed fields (reduces payload)
- `get-bitable-record --app-token <token> --table-id <id> --record-id <id>`
- `create-bitable-records --app-token <token> --table-id <id> --records '<json_array>'`
- `update-bitable-records --app-token <token> --table-id <id> --records '<json_array>'`
- `delete-bitable-records --app-token <token> --table-id <id> --record-ids '<json_array>' --confirm <id_prefix>` ⚠️
- Field value formats for records: Text→`"plain string"`, Number→`123`, SingleSelect→`"option name"`, MultiSelect→`["opt1","opt2"]`, Checkbox→`true/false`, DateTime→`unix_ms`, URL→`{"link":"...","text":"..."}`, User→`[{"id":"ou_xxx"}]`

**Field**
- `list-bitable-fields --app-token <token> --table-id <id>`
- `create-bitable-field --app-token <token> --table-id <id> --field-name <name> --field-type <int> [--property '<json>']` — Type codes: 1=Text 2=Number 3=SingleSelect 4=MultiSelect 5=DateTime 7=Checkbox 11=User 15=URL 17=Attachment 20=Formula 21=DuplexLink
- `update-bitable-field --app-token <token> --table-id <id> --field-id <id> [--field-name <name>] [--field-type <int>] [--property '<json>']`
- `delete-bitable-field --app-token <token> --table-id <id> --field-id <id> --confirm <id_prefix>` ⚠️

**View**
- `list-bitable-views --app-token <token> --table-id <id>`
- `get-bitable-view --app-token <token> --table-id <id> --view-id <id>`
- `create-bitable-view --app-token <token> --table-id <id> --view-name <name> [--view-type grid|kanban|gallery|gantt|form]`
- `patch-bitable-view --app-token <token> --table-id <id> --view-id <id> --view-name <new_name>` — Rename view
- `delete-bitable-view --app-token <token> --table-id <id> --view-id <id> --confirm <id_prefix>` ⚠️


### Drive (云盘)
- `upload-file --file <local_path> [--folder-token <token>] [--file-name <name>]` — Upload a local file to Drive (max 20MB, default: root folder)
- `upload-url --url <source_url> [--folder-token <token>] [--file-name <name>]` — Download from URL and upload to Drive (max 20MB, default: root folder)

### Mail (邮件)
- `send-mail --to <email> --subject <title> --body-html <html> [--body-plain <text>] [--cc <email>] [--bcc <email>] [--from-address <alias>] [--from-name <name>] [--attachment <path>]` — Send an email
  - `--to`, `--cc`, `--bcc`, `--attachment` are repeatable for multiple values
  - At least one of `--body-html` or `--body-plain` is required
  - `--from-address`: send from an alias email (e.g. jerry@xiao-llc.com)
  - `--attachment`: local file path, max 25MB/file, 50MB total
- `list-mail [--folder <name_or_id>] [--unread] [--page-size N] [--page-token <token>]` — List emails
  - `--folder` accepts folder name (e.g. "INBOX", case-insensitive) or folder_id string
- `read-mail --message-id <id>` — Read full email content
- `list-mail-folders [--folder-type 1|2]` — List mail folders (1=system, 2=user)
- `create-mail-folder --name <name> [--parent-folder-id <int>]` — Create a mail folder
- `list-mail-rules` — List mail rules
- `create-mail-rule --name <name> --condition '<json>' --action '<json>' [--disabled] [--stop-after-match]` — Create a mail rule
  - Condition: `'{"match_type": 1, "items": [{"type": 6, "operator": 1, "input": "invoice"}]}'`
    - match_type: 1=all, 2=any
    - type: 1=from, 2=to, 6=subject, 7=body
    - operator: 1=contains, 5=equals
  - Action: `'{"items": [{"type": 11, "input": "folder_id"}]}'`
    - type: 1=archive, 3=mark_read, 9=flag, 11=move_to_folder
- `delete-mail-rule --rule-id <int> --confirm <id_prefix>` ⚠️

### Tasks (任务)
- `list-tasks [--completed true|false] [--page-size N]` — List tasks visible to user
- `get-task --guid <task_guid>` — Get task details by GUID
- `list-tasklists [--page-size N]` — List task lists
- `get-tasklist --guid <tasklist_guid>` — Get task list details
- `list-tasklist-tasks --guid <tasklist_guid> [--completed true|false]` — List tasks in a task list
- `create-task --summary <text> [--description <text>] [--due <unix_ts>] [--tasklist-guid <guid>]` — Create a new task
- `complete-task --guid <task_guid>` — Mark a task as completed
- `update-task --guid <task_guid> [--summary <text>] [--description <text>] [--due <date>] [--completed-at <ts|now|0>]` — Update task fields (use `--completed-at 0` to uncomplete)
- `list-subtasks --guid <task_guid> [--page-size N]` — List subtasks of a task
- `create-subtask --parent-guid <guid> --summary <text> [--due <unix_ts>]` — Create a subtask
- `create-tasklist --name <name>` — Create a new task list
- `update-tasklist --guid <tasklist_guid> --name <new_name>` — Rename a task list
- `delete-tasklist --guid <tasklist_guid> --confirm <guid_prefix>` ⚠️
- `add-task-to-tasklist --task-guid <guid> --tasklist-guid <guid>` — Add a task to a list
- `remove-task-from-tasklist --task-guid <guid> --tasklist-guid <guid>` — Remove a task from a list

### Messaging (Bot)
- `send-message --chat-id <id> --text <text>` — Send a bot text message to a chat (no user auth needed)
- `send-message --chat-id <id> --msg-type interactive --content '<json>'` — Send a card/post message (raw JSON content)
- `send-image --chat-id <id> --file <path>` — Upload and send an image (png, jpg, etc.)

## Important Notes
- All output is JSON
- ⚠️ Delete commands require `--confirm <prefix>` matching the target token/ID prefix (safety guard)
- For multi-step operations (search → read → update), chain multiple calls
- Use `--help` on any command for full argument details
