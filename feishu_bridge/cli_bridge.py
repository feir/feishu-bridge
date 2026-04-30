#!/usr/bin/env python3
"""
Bridge infrastructure CLI — background task management.

Separated from feishu-cli (Feishu API operations) to maintain clear
responsibility boundaries. This CLI manages bridge-internal concerns:
task queuing, status tracking, cancellation.

Usage:
    bridge-cli bg enqueue --chat-id oc_xxx --on-done-prompt "done" --cmd-json '["bash","-c","echo hi"]'
    bridge-cli bg status <task_id>
    bridge-cli bg list
    bridge-cli bg cancel <task_id>
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _output(result):
    """Print result as JSON."""
    print(json.dumps(result, ensure_ascii=False, default=str))


# ---- bg-task helpers ------------------------------------------------------

def _bg_home() -> Path:
    from feishu_bridge.bg_paths import bg_home
    return bg_home()


def _bg_db_path() -> Path:
    return _bg_home() / "bg_tasks.db"


def _bg_sock_path() -> Path:
    return _bg_home() / "wake.sock"


def _bg_ensure_home() -> None:
    home = _bg_home()
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass


def _bg_nudge(sock_path: Path, payload: bytes) -> None:
    """Send UDS nudge; fail-open when bridge isn't listening."""
    import socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            s.connect(str(sock_path))
            s.sendall(payload)
    except (FileNotFoundError, ConnectionRefusedError,
            OSError, socket.timeout):
        pass


def _parse_env_kv(s: str) -> tuple[str, str]:
    if "=" not in s:
        raise ValueError(
            f"--env value must be KEY=VAL, got value of length {len(s)}"
        )
    k, v = s.split("=", 1)
    if not k:
        raise ValueError("--env key empty (expected KEY=VAL)")
    return k, v


def _bg_task_to_dict(row) -> dict:
    from dataclasses import asdict
    return asdict(row)


def _positive_int(max_value: int):
    """argparse type that rejects non-positive and above-cap values."""
    def _parse(s: str) -> int:
        try:
            n = int(s)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(
                f"expected an integer, got {s!r}"
            )
        if n <= 0:
            raise argparse.ArgumentTypeError(
                f"must be > 0, got {n}"
            )
        if n > max_value:
            raise argparse.ArgumentTypeError(
                f"must be <= {max_value}, got {n}"
            )
        return n
    return _parse


def _bg_db_json_error(label: str, exc: Exception) -> None:
    """Emit a consistent stderr JSON error for DB failures and exit 1."""
    print(json.dumps({"error": f"{label}: {exc}"}), file=sys.stderr)
    sys.exit(1)


def _run_bg_command(args) -> None:
    """Dispatch `bg <subcommand>`. Exits with nonzero on validation/DB errors."""
    import sqlite3
    import uuid
    from feishu_bridge.bg_tasks_db import (
        BgTaskRepo, TaskState, connect, init_db,
    )

    sub = args.bg_command
    db_path = _bg_db_path()
    sock_path = _bg_sock_path()

    def _open_repo() -> tuple[sqlite3.Connection, BgTaskRepo]:
        conn = connect(db_path)
        return conn, BgTaskRepo(conn)

    def _ensure_db() -> None:
        _bg_ensure_home()
        try:
            init_db(db_path).close()
        except sqlite3.Error as e:
            _bg_db_json_error("DB init failed", e)

    if sub == "enqueue":
        has_pos = bool(args.cmd_argv)
        has_json = args.cmd_json is not None
        if has_pos and has_json:
            print(json.dumps({
                "error": "Use either `-- <argv>` or --cmd-json, not both",
            }), file=sys.stderr)
            sys.exit(2)
        if not has_pos and not has_json:
            print(json.dumps({
                "error": "Command argv required: pass `-- cmd arg...` "
                         "or --cmd-json '[\"cmd\",\"arg\"]'",
            }), file=sys.stderr)
            sys.exit(2)

        if has_json:
            try:
                argv = json.loads(args.cmd_json)
            except json.JSONDecodeError as e:
                print(json.dumps({"error": f"--cmd-json parse: {e}"}),
                      file=sys.stderr)
                sys.exit(2)
            if (not isinstance(argv, list) or not argv
                    or not all(isinstance(x, str) for x in argv)):
                print(json.dumps({
                    "error": "--cmd-json must be a non-empty array of strings",
                }), file=sys.stderr)
                sys.exit(2)
        else:
            argv = list(args.cmd_argv)

        env_overlay: dict[str, str] = {}
        for kv in args.env:
            try:
                k, v = _parse_env_kv(kv)
            except ValueError as e:
                print(json.dumps({"error": str(e)}), file=sys.stderr)
                sys.exit(2)
            env_overlay[k] = v

        cwd_abs: str | None = None
        if args.cwd:
            cwd_abs = str(Path(args.cwd).expanduser().resolve())

        output_abs: list[str] = [
            str(Path(p).expanduser().resolve()) for p in args.output_path
        ]

        session_id = args.session_id or args.chat_id
        timeout_s = args.timeout_seconds

        _ensure_db()

        started = time.monotonic()
        conn, repo = _open_repo()
        try:
            task_id = repo.insert_task(
                chat_id=args.chat_id,
                session_id=session_id,
                thread_id=args.thread_id,
                command_argv=argv,
                on_done_prompt=args.on_done_prompt,
                requester_open_id=args.requester_open_id,
                cwd=cwd_abs,
                env_overlay=env_overlay or None,
                timeout_seconds=timeout_s,
                output_paths=output_abs or None,
            )
        except sqlite3.OperationalError as e:
            print(json.dumps({"error": f"DB operational error: {e}"}),
                  file=sys.stderr)
            sys.exit(1)
        except sqlite3.Error as e:
            print(json.dumps({"error": f"DB error: {e}"}),
                  file=sys.stderr)
            sys.exit(1)
        finally:
            conn.close()

        _bg_nudge(sock_path, b"\x01")
        latency_ms = int((time.monotonic() - started) * 1000)
        _output({
            "task_id": task_id,
            "state": "queued",
            "enqueue_latency_ms": latency_ms,
        })
        return

    if sub in ("status", "list", "cancel") and not db_path.exists():
        print(json.dumps({
            "error": "bg_tasks.db not found; no tasks have been enqueued yet",
        }), file=sys.stderr)
        sys.exit(1)

    if sub == "status":
        try:
            conn, repo = _open_repo()
        except sqlite3.Error as e:
            _bg_db_json_error("DB open failed", e)
        try:
            try:
                row = repo.get(args.task_id)
            except sqlite3.Error as e:
                _bg_db_json_error("DB error", e)
        finally:
            conn.close()
        if row is None:
            print(json.dumps({"error": f"task {args.task_id} not found"}),
                  file=sys.stderr)
            sys.exit(1)
        _output(_bg_task_to_dict(row))
        return

    if sub == "list":
        try:
            conn, repo = _open_repo()
        except sqlite3.Error as e:
            _bg_db_json_error("DB open failed", e)
        try:
            try:
                rows = repo.list(
                    chat_id=args.chat_id,
                    state=args.state,
                    limit=args.limit,
                )
            except sqlite3.Error as e:
                _bg_db_json_error("DB error", e)
        finally:
            conn.close()
        _output({"tasks": [_bg_task_to_dict(r) for r in rows]})
        return

    if sub == "cancel":
        try:
            conn, repo = _open_repo()
        except sqlite3.Error as e:
            _bg_db_json_error("DB open failed", e)
        try:
            try:
                row = repo.get(args.task_id)
                if row is None:
                    print(json.dumps({"error": f"task {args.task_id} not found"}),
                          file=sys.stderr)
                    sys.exit(1)
                if TaskState.is_terminal(row.state):
                    print(json.dumps({
                        "error": (f"task {args.task_id} already terminal "
                                  f"(state={row.state})"),
                    }), file=sys.stderr)
                    sys.exit(1)
                updated = repo.set_cancel_requested(args.task_id)
            except sqlite3.Error as e:
                _bg_db_json_error("DB error", e)
        finally:
            conn.close()
        if not updated:
            print(json.dumps({
                "error": (f"task {args.task_id} transitioned to terminal "
                          f"state during cancel; no flag set"),
            }), file=sys.stderr)
            sys.exit(1)
        try:
            uuid_bytes = uuid.UUID(hex=args.task_id).bytes
            _bg_nudge(sock_path, b"\x02" + uuid_bytes)
        except ValueError:
            _bg_nudge(sock_path, b"\x01")
        _output({"task_id": args.task_id, "cancel_requested": True})
        return

    print(json.dumps({"error": f"Unknown bg subcommand: {sub}"}),
          file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Bridge CLI — infrastructure commands for feishu-bridge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- Background tasks ---
    p_bg = sub.add_parser("bg", help="Background task management")
    bg_sub = p_bg.add_subparsers(dest="bg_command", required=True)

    p = bg_sub.add_parser(
        "enqueue",
        help="Enqueue a bg task; argv after -- or via --cmd-json",
    )
    p.add_argument("--chat-id", required=True,
                   help="Feishu chat_id (e.g. oc_xxx)")
    p.add_argument("--thread-id",
                   help="Feishu thread_id if launched inside a threaded reply; "
                        "delivery watcher reuses this to land the synthetic "
                        "completion turn in the originating thread")
    p.add_argument("--session-id",
                   help="Session identifier; defaults to chat_id")
    p.add_argument("--cwd", help="Working directory (absolute path)")
    p.add_argument("--env", action="append", default=[],
                   metavar="KEY=VAL",
                   help="Environment overlay entry (repeatable)")
    p.add_argument("--timeout-seconds", type=_positive_int(86400),
                   default=1800,
                   help="Hard timeout in seconds (1..86400, default 1800)")
    p.add_argument("--on-done-prompt", required=True,
                   help="Prompt delivered to the resumed session on completion")
    p.add_argument("--output-path", action="append", default=[],
                   metavar="PATH",
                   help="Declared output artifact path (repeatable)")
    p.add_argument("--cmd-json",
                   help='JSON array of argv, e.g. \'["python3","x.py"]\'')
    p.add_argument("--requester-open-id",
                   help="Originating user open_id (optional)")
    p.add_argument("cmd_argv", nargs="*",
                   help="Command argv (must appear after --)")

    p = bg_sub.add_parser("status", help="Show a single task's state")
    p.add_argument("task_id")

    p = bg_sub.add_parser("list", help="List tasks (newest first)")
    p.add_argument("--chat-id")
    p.add_argument("--state")
    p.add_argument("--limit", type=_positive_int(200), default=20,
                   help="Max rows to return (1..200, default 20)")

    p = bg_sub.add_parser(
        "cancel",
        help="Request cancel; only sets cancel_requested_at + UDS nudge",
    )
    p.add_argument("task_id")

    args = parser.parse_args()

    if args.command == "bg":
        _run_bg_command(args)
        return

    print(json.dumps({"error": f"Unknown command: {args.command}"}),
          file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
