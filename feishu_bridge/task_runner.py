"""task-runner wrapper binary.

Long-lived wrapper that owns the user-command subprocess lifecycle. Spawned
detached from bridge so a bridge crash/restart cannot orphan the child or its
manifest.

Phases (see .specs/changes/feishu-bridge-bg-tasks/design.md):

    Phase P  —  read wrapper self start_time; INSERT bg_runs (pid NULL)
    Phase S  —  Popen(start_new_session=True) + single-tx attach (pid/pgid
                /process_start_time_us on bg_runs, state=running on bg_tasks)
    Phase W  —  wait + stream stdout/stderr; 500ms poll cancel + monotonic deadline
    Phase C  —  write task.json.tmp + fsync + rename to .done; mv active→completed;
                single-tx finish_run; best-effort UDS nudge

The wrapper reads BG_TASK_TOKEN injected into the user command's env for
identity verification by the startup reconciler; the wrapper's own argv
carries ``--runner-token`` so ``ps eww`` can locate live wrappers.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from ctypes import c_char, c_int32, c_uint32, c_uint64
from pathlib import Path
from typing import Optional

from feishu_bridge.bg_tasks_db import (
    BgTaskRepo,
    TaskState,
    _AttachRace,
    _FinishRace,
    init_db,
)

log = logging.getLogger("feishu-bridge.task-runner")

_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Manifest shape; bump in lock-step with bg_tasks_db._MAX_MANIFEST_SCHEMA_VERSION.
MANIFEST_SCHEMA_VERSION = 2

# Tail retention: keep enough bytes to cover a full 4096B UTF-8 safe slice even
# when the trailing code point straddles the window.
_TAIL_WINDOW_BYTES = 8192
_TAIL_OUTPUT_BYTES = 4096

_CANCEL_POLL_INTERVAL = 0.5   # seconds
_TERMINATE_GRACE_SECONDS = 5.0
_FINISH_RETRY_ATTEMPTS = 3
_FINISH_RETRY_BACKOFF = 0.5


# ---------------------------------------------------------------------------
# libproc.proc_pidinfo — μs-precision start_time on macOS
# ---------------------------------------------------------------------------
#
# Struct layout mirrors ``struct proc_bsdinfo`` from xnu ``sys/proc_info.h``.
# Offset of ``pbi_start_tvsec`` is 120; ``pbi_start_tvusec`` is 128; total
# struct size is 136. The pre-mortem self-check validates layout before use.

PROC_PIDTBSDINFO = 3
PROC_PIDTBSDINFO_SIZE = 136


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags",         c_uint32),
        ("pbi_status",        c_uint32),
        ("pbi_xstatus",       c_uint32),
        ("pbi_pid",           c_uint32),
        ("pbi_ppid",          c_uint32),
        ("pbi_uid",           c_uint32),
        ("pbi_gid",           c_uint32),
        ("pbi_ruid",          c_uint32),
        ("pbi_rgid",          c_uint32),
        ("pbi_svuid",         c_uint32),
        ("pbi_svgid",         c_uint32),
        ("rfu_1",             c_uint32),
        ("pbi_comm",          c_char * 16),
        ("pbi_name",          c_char * 32),
        ("pbi_nfiles",        c_uint32),
        ("pbi_pgid",          c_uint32),
        ("pbi_pjobc",         c_uint32),
        ("e_tdev",            c_uint32),
        ("e_tpgid",           c_uint32),
        ("pbi_nice",          c_int32),
        ("pbi_start_tvsec",   c_uint64),
        ("pbi_start_tvusec",  c_uint64),
    ]


_libproc: Optional[ctypes.CDLL] = None


def _libproc_once() -> ctypes.CDLL:
    global _libproc
    if _libproc is None:
        if sys.platform != "darwin":
            raise RuntimeError(
                "task_runner requires macOS libproc.proc_pidinfo (μs-precision start_time)"
            )
        _libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        _libproc.proc_pidinfo.argtypes = [
            c_int32, c_int32, c_uint64,
            ctypes.c_void_p, c_int32,
        ]
        _libproc.proc_pidinfo.restype = c_int32
        # sanity: struct size must match ABI
        if ctypes.sizeof(_ProcBsdInfo) != PROC_PIDTBSDINFO_SIZE:
            raise RuntimeError(
                f"proc_bsdinfo layout mismatch: sizeof={ctypes.sizeof(_ProcBsdInfo)} "
                f"want={PROC_PIDTBSDINFO_SIZE}"
            )
    return _libproc


def read_proc_start_time_us(pid: int) -> int:
    """Return ``pid``'s start time in μs since epoch.

    Raises ``OSError`` if the pid is gone or libproc refuses (EPERM for other
    users' processes — shouldn't happen for wrapper's own children).
    """
    lp = _libproc_once()
    info = _ProcBsdInfo()
    ret = lp.proc_pidinfo(
        pid, PROC_PIDTBSDINFO, 0,
        ctypes.byref(info), ctypes.sizeof(info),
    )
    if ret != ctypes.sizeof(info):
        errno = ctypes.get_errno()
        raise OSError(
            errno, f"proc_pidinfo(pid={pid}) returned {ret}, expected "
            f"{ctypes.sizeof(info)} (errno={errno})"
        )
    return int(info.pbi_start_tvsec) * 1_000_000 + int(info.pbi_start_tvusec)


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without spawning subprocesses)
# ---------------------------------------------------------------------------

def utf8_safe_tail(buf: bytes, limit: int = _TAIL_OUTPUT_BYTES) -> bytes:
    """Return the last ``limit`` bytes of ``buf`` aligned to a UTF-8 boundary.

    If the trailing slice begins mid-codepoint, drop leading continuation bytes
    (0b10xxxxxx) until the first legal start byte, so decoders don't choke on
    partial code points spanning the boundary.
    """
    if len(buf) <= limit:
        return bytes(buf)
    tail = bytes(buf[-limit:])
    # Skip up to 3 leading continuation bytes so we land on a UTF-8 start byte.
    i = 0
    while i < min(4, len(tail)) and (tail[i] & 0b11000000) == 0b10000000:
        i += 1
    return tail[i:]


def build_manifest_dict(
    *,
    task_id: str,
    state: str,
    exit_code: Optional[int],
    signal_name: Optional[str],
    reason: Optional[str],
    runner_token: str,
    pid: Optional[int],
    pgid: Optional[int],
    process_start_time_us: Optional[int],
    wrapper_pid: int,
    wrapper_start_time_us: int,
    started_at_ms: int,
    finished_at_ms: int,
    command_argv: list[str],
    cwd: Optional[str],
    stdout_tail: bytes,
    stderr_tail: bytes,
    output_paths: list[str],
    on_done_prompt: str,
    chat_id: str,
    session_id: str,
) -> dict:
    duration_s = max(0.0, (finished_at_ms - started_at_ms) / 1000.0)
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "task_id": task_id,
        "state": state,
        "reason": reason,
        "signal": signal_name,
        "exit_code": exit_code,
        "runner_token": runner_token,
        "pid": pid,
        "pgid": pgid,
        "process_start_time_us": process_start_time_us,
        "wrapper_pid": wrapper_pid,
        "wrapper_start_time_us": wrapper_start_time_us,
        "started_at_ms": started_at_ms,
        "finished_at_ms": finished_at_ms,
        "duration_seconds": round(duration_s, 3),
        "command_argv": command_argv,
        "cwd": cwd,
        "stdout_tail_b64": base64.b64encode(stdout_tail).decode("ascii"),
        "stderr_tail_b64": base64.b64encode(stderr_tail).decode("ascii"),
        "output_paths": output_paths,
        "on_done_prompt": on_done_prompt,
        "chat_id": chat_id,
        "session_id": session_id,
    }


def write_manifest_atomically(manifest_path: Path, data: dict) -> None:
    """os.write + fsync to .tmp then rename to target path. Crash-safe."""
    tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        written = os.write(fd, payload)
        if written != len(payload):
            raise IOError(f"short write: {written}/{len(payload)}")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(str(tmp), str(manifest_path))


def _cleanup_child_io(
    proc: Optional[subprocess.Popen],
    stdout_collector: Optional["_StreamCollector"],
    stderr_collector: Optional["_StreamCollector"],
    wait_timeout_s: float = 2.0,
) -> None:
    """Close pipes, join collectors, reap proc. Tolerates partial state."""
    if proc is not None:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
    for c in (stdout_collector, stderr_collector):
        if c is not None:
            try:
                c.join(timeout=wait_timeout_s)
            except Exception:  # noqa: BLE001
                pass
    if proc is not None and proc.poll() is None:
        try:
            proc.wait(timeout=wait_timeout_s)
        except subprocess.TimeoutExpired:
            # Best-effort reap — pgid was already signalled by caller. Leaving
            # the zombie for init is acceptable vs. blocking wrapper exit.
            pass


def terminate_pgid(pgid: int, grace_s: float = _TERMINATE_GRACE_SECONDS) -> None:
    """SIGTERM to the process group; SIGKILL after ``grace_s`` if still alive.

    Does not wait for final reap — caller still owns the Popen.
    """
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return

    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)  # probe
        except ProcessLookupError:
            return
        except PermissionError:
            # macOS returns EPERM once the group leader has been reaped or the
            # pgid slot is recycled; treat as "gone" rather than retrying.
            return
        time.sleep(0.1)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# ---------------------------------------------------------------------------
# Stream collector — one thread per pipe
# ---------------------------------------------------------------------------

class _StreamCollector(threading.Thread):
    """Read from ``src`` to a log file while retaining a trailing window in RAM."""

    def __init__(self, src, log_path: Path, label: str) -> None:
        super().__init__(name=f"task-runner-{label}", daemon=True)
        self._src = src
        self._log_path = log_path
        self._window = bytearray()
        self._lock = threading.Lock()

    def run(self) -> None:
        try:
            with open(self._log_path, "ab", buffering=0) as fp:
                while True:
                    chunk = self._src.read(4096)
                    if not chunk:
                        return
                    fp.write(chunk)
                    with self._lock:
                        self._window.extend(chunk)
                        over = len(self._window) - _TAIL_WINDOW_BYTES
                        if over > 0:
                            del self._window[:over]
        except Exception as exc:
            log.warning("stream collector %s failed: %s", self._log_path.name, exc)

    def tail_bytes(self, limit: int = _TAIL_OUTPUT_BYTES) -> bytes:
        with self._lock:
            return utf8_safe_tail(bytes(self._window), limit)


# ---------------------------------------------------------------------------
# Phase orchestration
# ---------------------------------------------------------------------------

class _WrapperState:
    """Container for values that flow P → S → W → C."""

    def __init__(
        self,
        *,
        task_id: str,
        runner_token: str,
        db_path: Path,
        tasks_dir: Path,
        bridge_home: Path,
    ) -> None:
        self.task_id = task_id
        self.runner_token = runner_token
        self.db_path = db_path
        self.tasks_dir = tasks_dir
        self.bridge_home = bridge_home

        # connection is per-thread per bg_tasks_db contract; wrapper is single-threaded
        self.conn = init_db(str(db_path))
        self.repo = BgTaskRepo(self.conn)

        self.wrapper_pid = os.getpid()
        self.wrapper_start_time_us = 0  # filled in Phase P
        self.run_id = 0

        self.task_row = None  # loaded in Phase P
        self.active_dir = tasks_dir / "active" / task_id
        self.completed_dir = tasks_dir / "completed" / task_id

        self.started_at_ms = 0
        self.child_proc: Optional[subprocess.Popen] = None
        self.child_pgid: Optional[int] = None
        self.child_start_time_us: Optional[int] = None

        self.stdout_collector: Optional[_StreamCollector] = None
        self.stderr_collector: Optional[_StreamCollector] = None


def phase_p(state: _WrapperState) -> None:
    """Pre-register identity. Insert bg_runs row before any Popen."""
    state.wrapper_start_time_us = read_proc_start_time_us(state.wrapper_pid)

    state.task_row = state.repo.get(state.task_id)
    if state.task_row is None:
        raise RuntimeError(f"task_id {state.task_id} has no bg_tasks row")
    if state.task_row.state != TaskState.LAUNCHING.value:
        raise RuntimeError(
            f"task {state.task_id} state={state.task_row.state!r}, "
            f"expected 'launching' — bridge supervisor must CAS-claim before spawning wrapper"
        )

    state.active_dir.mkdir(parents=True, exist_ok=True)

    state.started_at_ms = int(time.time() * 1000)
    state.run_id = state.repo.start_run(
        task_id=state.task_id,
        runner_token=state.runner_token,
        wrapper_pid=state.wrapper_pid,
        wrapper_start_time_us=state.wrapper_start_time_us,
    )
    log.info(
        "phase_p: run_id=%d wrapper_pid=%d wrapper_start_us=%d",
        state.run_id, state.wrapper_pid, state.wrapper_start_time_us,
    )


def phase_s(state: _WrapperState) -> None:
    """Spawn child, record identity, flip launching → running in one tx."""
    argv = state.task_row.command_argv
    if not isinstance(argv, list) or not argv:
        raise RuntimeError(f"task {state.task_id} has invalid command_argv")

    env = os.environ.copy()
    overlay = state.task_row.env_overlay or {}
    if isinstance(overlay, dict):
        env.update({str(k): str(v) for k, v in overlay.items()})
    env["BG_TASK_TOKEN"] = state.runner_token

    stdout_path = state.active_dir / "stdout.log"
    stderr_path = state.active_dir / "stderr.log"
    stdout_path.touch()
    stderr_path.touch()

    state.child_proc = subprocess.Popen(
        argv,
        shell=False,
        start_new_session=True,
        cwd=state.task_row.cwd or None,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    state.child_pgid = os.getpgid(state.child_proc.pid)

    # S2: read child's start_time_us immediately
    try:
        state.child_start_time_us = read_proc_start_time_us(state.child_proc.pid)
    except OSError as exc:
        # Child died before we could read start time — still have to record
        # something or reconciler can't locate the orphan.
        log.error(
            "phase_s: proc_pidinfo(child=%d) failed: %s — killing + aborting",
            state.child_proc.pid, exc,
        )
        terminate_pgid(state.child_pgid, grace_s=1.0)
        _cleanup_child_io(state.child_proc, None, None)
        raise

    # Stream collectors owned by wrapper from here on.
    state.stdout_collector = _StreamCollector(
        state.child_proc.stdout, stdout_path, "stdout",
    )
    state.stderr_collector = _StreamCollector(
        state.child_proc.stderr, stderr_path, "stderr",
    )
    state.stdout_collector.start()
    state.stderr_collector.start()

    # S3: single-tx attach
    try:
        state.repo.attach_child(
            run_id=state.run_id,
            task_id=state.task_id,
            pid=state.child_proc.pid,
            pgid=state.child_pgid,
            process_start_time_us=state.child_start_time_us,
        )
    except _AttachRace:
        log.error(
            "phase_s: attach_child race — killing child pgid=%d and aborting",
            state.child_pgid,
        )
        terminate_pgid(state.child_pgid, grace_s=1.0)
        _cleanup_child_io(
            state.child_proc, state.stdout_collector, state.stderr_collector,
        )
        raise

    # Redact argv at INFO — it may carry secrets (tokens, paths). Debug level
    # gets the full vector for diagnosis; tail logs capture post-hoc detail.
    log.info(
        "phase_s: child pid=%d pgid=%d start_us=%d argv0=%s argv_len=%d",
        state.child_proc.pid, state.child_pgid, state.child_start_time_us,
        argv[0], len(argv),
    )
    log.debug("phase_s: full argv=%r", argv)


def phase_w(state: _WrapperState) -> tuple[str, Optional[str]]:
    """Wait for child exit while polling cancel_requested_at and monotonic deadline.

    Returns ``(terminal_state, reason)`` — reason is ``cancelled`` / ``timeout``
    or None when the child exits on its own.
    """
    proc = state.child_proc
    assert proc is not None and state.child_pgid is not None

    timeout_s = int(state.task_row.timeout_seconds or 1800)
    deadline = time.monotonic() + timeout_s

    killed_for: Optional[str] = None

    while True:
        try:
            proc.wait(timeout=_CANCEL_POLL_INTERVAL)
            break
        except subprocess.TimeoutExpired:
            pass

        if killed_for is None:
            if _cancel_requested(state):
                log.info("phase_w: cancel_requested — terminating pgid=%d", state.child_pgid)
                terminate_pgid(state.child_pgid)
                killed_for = "cancelled"
                continue
            if time.monotonic() >= deadline:
                log.info(
                    "phase_w: timeout after %ds — terminating pgid=%d",
                    timeout_s, state.child_pgid,
                )
                terminate_pgid(state.child_pgid)
                killed_for = "timeout"
                continue

    # Join stream collectors so the tail is complete.
    if state.stdout_collector is not None:
        state.stdout_collector.join(timeout=2.0)
    if state.stderr_collector is not None:
        state.stderr_collector.join(timeout=2.0)

    # Decide terminal state.
    if killed_for == "cancelled":
        return TaskState.CANCELLED.value, "cancelled"
    if killed_for == "timeout":
        return TaskState.TIMEOUT.value, "timeout"

    returncode = proc.returncode
    if returncode == 0:
        return TaskState.COMPLETED.value, None
    return TaskState.FAILED.value, f"exit_code={returncode}"


def _cancel_requested(state: _WrapperState) -> bool:
    row = state.repo.get(state.task_id)
    if row is None:
        return False
    # `BgTaskRow` dataclass exposes cancel_requested_at; None means not cancelled.
    return getattr(row, "cancel_requested_at", None) is not None


def phase_c(
    state: _WrapperState,
    *,
    terminal_state: str,
    reason: Optional[str],
) -> None:
    """Write manifest, rename active→completed, single-tx finish, UDS nudge."""
    proc = state.child_proc
    assert proc is not None

    exit_code = proc.returncode
    signal_name = None
    if exit_code is not None and exit_code < 0:
        signal_name = _signal_name_from_exit_code(exit_code)

    stdout_tail = (
        state.stdout_collector.tail_bytes() if state.stdout_collector else b""
    )
    stderr_tail = (
        state.stderr_collector.tail_bytes() if state.stderr_collector else b""
    )

    finished_at_ms = int(time.time() * 1000)
    raw_outputs = state.task_row.output_paths or []
    output_paths: list[str] = (
        [str(p) for p in raw_outputs] if isinstance(raw_outputs, list) else []
    )

    manifest = build_manifest_dict(
        task_id=state.task_id,
        state=terminal_state,
        exit_code=exit_code,
        signal_name=signal_name,
        reason=reason,
        runner_token=state.runner_token,
        pid=proc.pid,
        pgid=state.child_pgid,
        process_start_time_us=state.child_start_time_us,
        wrapper_pid=state.wrapper_pid,
        wrapper_start_time_us=state.wrapper_start_time_us,
        started_at_ms=state.started_at_ms,
        finished_at_ms=finished_at_ms,
        command_argv=list(state.task_row.command_argv),
        cwd=state.task_row.cwd,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        output_paths=output_paths,
        on_done_prompt=state.task_row.on_done_prompt,
        chat_id=state.task_row.chat_id,
        session_id=state.task_row.session_id,
    )

    # C1 + C2: atomic rename to .done (manifest already in active/).
    active_manifest = state.active_dir / "task.json.done"
    write_manifest_atomically(active_manifest, manifest)

    # C3: rename active/<id> → completed/<id>. If this fails we leave the .done
    # manifest in active/<id>; reconciler §6.5 replay covers the gap.
    state.completed_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        state.active_dir.rename(state.completed_dir)
        manifest_path = state.completed_dir / "task.json.done"
    except OSError as exc:
        log.error(
            "phase_c: rename active→completed failed: %s — leaving in active/",
            exc,
        )
        manifest_path = active_manifest

    # C4: single-tx finish_run. Retry transient OperationalError; _FinishRace
    # means state changed under us — leave it to reconciler via manifest replay.
    last_err: Optional[Exception] = None
    for attempt in range(1, _FINISH_RETRY_ATTEMPTS + 1):
        try:
            state.repo.finish_run(
                run_id=state.run_id,
                task_id=state.task_id,
                terminal_state=terminal_state,
                exit_code=exit_code,
                signal=signal_name,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                manifest_path=str(manifest_path),
                reason=reason,
            )
            break
        except _FinishRace as exc:
            log.error(
                "phase_c: finish_run race — state diverged; reconciler will replay: %s",
                exc,
            )
            last_err = exc
            break
        except Exception as exc:
            last_err = exc
            log.warning(
                "phase_c: finish_run attempt %d/%d failed: %s",
                attempt, _FINISH_RETRY_ATTEMPTS, exc,
            )
            if attempt < _FINISH_RETRY_ATTEMPTS:
                time.sleep(_FINISH_RETRY_BACKOFF * attempt)
    else:
        raise RuntimeError(
            f"phase_c: finish_run exhausted {_FINISH_RETRY_ATTEMPTS} attempts: {last_err}"
        )

    # C5: UDS nudge — best-effort; poller covers the 1s worst-case.
    _nudge(state.bridge_home, state.task_id)


def _signal_name_from_exit_code(exit_code: int) -> Optional[str]:
    """Popen returncode < 0 means killed by signal |-returncode|."""
    sig = -exit_code
    try:
        return signal.Signals(sig).name
    except (ValueError, KeyError):
        return f"SIG{sig}"


def _nudge(bridge_home: Path, task_id: str) -> None:
    """Send \\x03+uuid delivery-ready nudge to bridge wake.sock.

    Uses SOCK_STREAM to match the supervisor listener (bg_supervisor.py) and
    the CLI nudger (cli._bg_nudge). Fail-open: if no bridge is listening or
    the connect fails, the poller will catch up within poll_interval.
    """
    sock_path = bridge_home / "wake.sock"
    if not sock_path.exists():
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(str(sock_path))
            s.sendall(b"\x03" + uuid.UUID(hex=task_id).bytes)
    except (OSError, ValueError) as exc:
        log.debug("nudge to %s failed (non-fatal): %s", sock_path, exc)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _configure_logging(bridge_home: Path, task_id: str) -> None:
    log_dir = bridge_home / "logs" / "task-runner"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task_id}.log"

    handler = logging.FileHandler(str(log_path), mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    ))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="task-runner",
        description="feishu-bridge background-task wrapper",
    )
    p.add_argument("--task-id", required=True,
                   help="uuid4 hex identifying the bg_tasks row this wrapper owns")
    p.add_argument("--db-path", required=True,
                   help="path to bg_tasks.db; also determines bridge home directory")
    p.add_argument("--tasks-dir", required=True,
                   help="directory containing active/ and completed/ subdirs")
    p.add_argument("--runner-token", required=True,
                   help="uuid4 nonce injected into child env as BG_TASK_TOKEN")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)

    if not _TASK_ID_RE.fullmatch(args.task_id):
        print(f"invalid --task-id: {args.task_id!r}", file=sys.stderr)
        return 2
    try:
        uuid.UUID(args.runner_token)
    except ValueError:
        print(f"invalid --runner-token (not a uuid): {args.runner_token!r}", file=sys.stderr)
        return 2

    db_path = Path(args.db_path).expanduser().resolve()
    tasks_dir = Path(args.tasks_dir).expanduser().resolve()
    bridge_home = db_path.parent

    _configure_logging(bridge_home, args.task_id)
    log.info(
        "task-runner start: task_id=%s wrapper_pid=%d",
        args.task_id, os.getpid(),
    )

    state = _WrapperState(
        task_id=args.task_id,
        runner_token=args.runner_token,
        db_path=db_path,
        tasks_dir=tasks_dir,
        bridge_home=bridge_home,
    )

    try:
        try:
            phase_p(state)
        except Exception as exc:
            log.error("phase_p failed: %s — exiting (reconciler will reap)", exc, exc_info=True)
            return 1

        try:
            phase_s(state)
        except Exception as exc:
            log.error("phase_s failed: %s — exiting (reconciler will reap)", exc, exc_info=True)
            return 1

        try:
            terminal_state, reason = phase_w(state)
        except Exception as exc:
            log.error("phase_w failed: %s", exc, exc_info=True)
            terminal_state = TaskState.FAILED.value
            reason = f"phase_w: {type(exc).__name__}"

        try:
            phase_c(state, terminal_state=terminal_state, reason=reason)
        except Exception as exc:
            log.error("phase_c failed: %s — reconciler will replay from manifest", exc,
                      exc_info=True)
            return 1

        log.info("task-runner done: task_id=%s state=%s", args.task_id, terminal_state)
        return 0
    finally:
        _cleanup_child_io(
            state.child_proc, state.stdout_collector, state.stderr_collector,
        )
        try:
            state.conn.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
