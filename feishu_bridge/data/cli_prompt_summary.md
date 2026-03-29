# Feishu CLI

You have access to Feishu (飞书) APIs via `feishu-cli <command> [args]`.

## Command Categories
- **Documents** — read, create, update, delete docs (Markdown-based)
- **Spreadsheets** — read, write, append cells; get sheet metadata
- **Wiki** — list spaces/nodes, resolve wiki links to doc/sheet
- **Comments** — list, add, reply, resolve comments on files
- **Calendar** — events CRUD, attendees, free/busy queries
- **Search** — search docs/messages, list messages/files
- **Bitable** — multidimensional tables: apps, tables, records, fields, views
- **Drive** — upload local files or URLs to cloud drive
- **Mail** — send/list/read emails, manage folders and rules
- **Tasks** — task/subtask CRUD, task lists, completion tracking
- **Messaging** — send bot messages to chats

Run `feishu-cli <command> --help` for detailed usage and arguments.
All output is JSON. Delete commands require `--confirm <prefix>` safety guard.
