"""Background-task supervisor — single instance per bridge process.

Responsibilities (Section 4.1–4.4):
    - UDS listener thread on ``~/.feishu-bridge/wake.sock``
    - 1s fallback poller (so a dropped nudge never strands a queued task)
    - Launcher: CAS claim ``queued→launching`` + spawn ``task-runner`` wrapper
    - Flip cancel-before-launch rows to ``cancelled`` (Cancel SLO ≤10s)

Section 4.5 (delivery watcher) lands later; ``_scan_delivery_outbox`` is the
seam it will fill. The ``b'\\x03'`` wake payload from wrapper Phase C5 already
routes here so the delivery path is wired end-to-end when 4.5 drops.

See .specs/changes/feishu-bridge-bg-tasks/design.md §UDS Wake Protocol and
§Startup Reconciler for the protocol and reconcile seam (Section 6).
"""

from __future__ import annotations

import base64
import binascii
import errno
import json
import logging
import os
import re
import signal as _signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from feishu_bridge.bg_synthetic_turn import build_synthetic_turn
from feishu_bridge.bg_tasks_db import (
    BgTaskRepo,
    TaskState,
    _FinishRace,
    connect,
    init_db,
    cleanup_and_archive,
    cleanup_quarantine_files,
    integrity_check_and_maybe_quarantine,
    promote_active_to_completed,
    rebuild_from_manifests,
)
from feishu_bridge.runtime import SessionMap
from feishu_bridge.session_resume import (
    SessionsIndex,
    build_fresh_fallback_prefix,
    resolve_resume_status,
)

log = logging.getLogger("feishu-bridge.bg-supervisor")

# Wake payload sizes. Longer payloads are truncated by recv().
_WAKE_RECV_BYTES = 17  # 1 kind byte + 16 UUID bytes
_ACCEPT_TIMEOUT_S = 0.5
_RECV_TIMEOUT_S = 0.5
_PROBE_TIMEOUT_S = 0.2
_LISTEN_BACKLOG = 8

# 5 min: max time an `enqueued` row may sit before we assume the enqueueing
# bridge crashed between CAS and handing the item to worker. Rollback is
# bridge-crash recovery, so it does NOT bump_attempt (the retry budget is
# reserved for genuine delivery failures). Value is spec-pinned at
# design.md:391/470/509 — changing it requires a spec revision.
_STUCK_ENQUEUED_MS = 5 * 60 * 1000
_DELIVERY_BATCH_LIMIT = 32

# §6.2: rows that claimed_at more than 30s ago but never transitioned out of
# `launching` are stranded — the bridge that claimed them died between CAS and
# Popen-success. Matches design.md:279.
_STALE_LAUNCHING_MS = 30 * 1000

# §6.5: delivery_attempt_count has a hard retry cap. At boot the reconciler
# logs ERROR for any run that has already burned all its budget — these are
# the "terminal" rows that will never be delivered without operator
# intervention.
_DELIVERY_ATTEMPT_CAP = 10

# §6.3 reap tuning: grace between SIGTERM and SIGKILL mirrors wrapper Phase W.
_REAP_SIGTERM_GRACE_S = 5.0
_REAP_POLL_INTERVAL_S = 0.5
# Reaper thread poll cadence when bridge takes over a still-running orphan.
_REAPER_POLL_INTERVAL_S = 1.0
# Max reaper threads a single reconcile() may spawn. A huge backlog of orphans
# shouldn't flood the process with watcher threads; the rest are left in
# running state and picked up on the next boot (or cancel/timeout trigger).
_REAPER_MAX_THREADS = 32

# uuid4 hex nonce injected as BG_TASK_TOKEN into the child env. Anchor the
# regex tightly — any looser match risks a partial collision mis-identifying
# an unrelated process under `ps -E` (which dumps the full environment block,
# not just our token).
_RUNNER_TOKEN_RE = re.compile(r"BG_TASK_TOKEN=([0-9a-f]{32})")

# §6.3 reap reasons — keep string literals in one place so tests and reports
# can assert on them without re-inventing the vocabulary.
_REAP_REASON_WRAPPER_DIED_POST_REGISTER = "reaped_by_bridge_after_wrapper_death"
_REAP_REASON_WRAPPER_DIED_PRE_REGISTER = "wrapper_died_pre_register"
_REAP_REASON_BOTH_DIED = "wrapper_and_child_both_died"
_REAP_REASON_TIMEOUT = "reaped_by_bridge_timeout_after_wrapper_death"


# ---------------------------------------------------------------------------
# §6.3 identity verification helpers
# ---------------------------------------------------------------------------
#
# These are intentionally module-level pure-ish functions so the triage logic
# can unit-test them via ``monkeypatch.setattr(bg_supervisor, "_fn", ...)``
# without needing subprocess plumbing in every test.
#
# Contract, rigorously: every helper returns ``None`` / ``False`` on any
# failure mode (pid gone, EPERM, parse failure, OS not supported). The
# triage code treats "can't verify" identically to "mismatch" — ``mark
# orphan, no signal``. This is the design's safety default: pid reuse MUST
# NOT be able to sneak a SIGKILL onto an unrelated process, so helpers fail
# closed and the caller's default branch is ``no signal``.


def _proc_start_time_us(pid: int) -> Optional[int]:
    """Return ``pid``'s μs-precision start time, or ``None`` on any failure.

    Wraps ``task_runner.read_proc_start_time_us`` (libproc.proc_pidinfo on
    macOS) with fail-closed semantics. The wrapper itself *raises* OSError
    intentionally for its own pre-mortem self-check; the reconciler cannot
    afford to abort, so this wrapper swallows every error into ``None``.

    Linux / other platforms: returns ``None`` (no μs-precision API available,
    triage will mark every running row as "can't verify" → orphan). That's
    intentional: deploying background tasks on Linux requires a follow-up
    design for ``/proc/<pid>/stat`` parsing.
    """
    try:
        # Deferred import so non-darwin platforms (CI linters) don't choke at
        # import time — task_runner only supports macOS libproc.
        from feishu_bridge.task_runner import (
            read_proc_start_time_us as _read_us,
        )
    except Exception:
        return None
    try:
        return int(_read_us(int(pid)))
    except (OSError, RuntimeError, ValueError, TypeError):
        return None
    except Exception:
        # Defensive: libproc wrapper could surface unexpected ctypes errors.
        # A reconciler that crashes here would leave every running task
        # stranded — fail closed instead.
        log.debug("bg reconcile: _proc_start_time_us(%r) unexpected error",
                  pid, exc_info=True)
        return None


def _read_proc_env_token(pid: int, *, timeout_s: float = 2.0) -> Optional[str]:
    """Extract ``BG_TASK_TOKEN`` from ``pid``'s environment via ``ps e``.

    ``ps eww -p <pid>`` dumps the command line *followed by* the entire
    environment block on macOS (BSD bundled syntax: e=env, ww=wide). We
    regex-match the uuid-hex shape strictly so an attacker cannot smuggle
    a shorter token that happens to share a prefix.

    macOS SIP caveat: env of SIP-protected binaries (e.g. /bin/sleep,
    /usr/bin/*) is unreadable even to the parent process. Production
    runners exec ``claude`` (non-SIP) so env is visible; tests that spawn
    /bin/sleep will fail this lookup.

    Returns ``None`` if the pid is gone, ``ps`` fails, or the token is
    absent. Never raises.
    """
    try:
        proc = subprocess.run(
            ["/bin/ps", "eww", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        # Non-zero usually means the pid vanished between the caller's
        # liveness check and our ps call. That's a race, not an error.
        return None
    m = _RUNNER_TOKEN_RE.search(proc.stdout or "")
    return m.group(1) if m else None


def _verify_triple(
    pid: Optional[int],
    *,
    expected_start_us: Optional[int],
    expected_token: Optional[str],
) -> bool:
    """Return True iff all three identity anchors match the live process.

    Any input being ``None`` / ``0`` / empty → False (spec bug or missing
    column — we refuse to signal on incomplete data). Any anchor
    disagreeing → False. Only three-way agreement returns True.
    """
    if not pid or pid <= 0:
        return False
    if not expected_start_us or expected_start_us <= 0:
        return False
    if not expected_token:
        return False
    # Liveness + ownership are implicit in proc_pidinfo: if the pid is gone
    # or belongs to another uid, _proc_start_time_us returns None.
    live_start = _proc_start_time_us(pid)
    if live_start is None:
        return False
    if int(live_start) != int(expected_start_us):
        return False
    live_token = _read_proc_env_token(pid)
    if live_token is None:
        return False
    return live_token == expected_token


def _scan_ps_for_token(expected_token: str, *, timeout_s: float = 3.0) -> Optional[tuple[int, int]]:
    """Full ``ps axeww -o pid=,pgid=,command=`` scan for the
    post_spawn_pre_register window: wrapper died between ``Popen()`` and the
    Phase S single-transaction that stamps pid/pgid on ``bg_runs``.

    The child is alive (wrapper spawned it with ``start_new_session=True`` so it
    has its own pgid) but the DB row carries no pid. The only anchor left is
    the runner_token in env.

    ``axeww`` (BSD bundled syntax): a=all users, x=processes without tty,
    e=env, ww=wide. ``-o pid=,pgid=,command=`` still appends env to the
    command column when ``e`` is in the flag cluster, but the ``a`` and ``x``
    flags are required — a newly spawned child with start_new_session=True
    has no controlling tty and would be skipped by default ps output.

    Returns ``(pid, pgid)`` of the matching process, or ``None`` if no match.
    Multiple matches would be a token-collision bug — we return the first and
    log a warning (uuid4 collision probability is ~0; a repeat means the same
    token was injected twice, which is a wrapper implementation bug).
    """
    if not expected_token or not _RUNNER_TOKEN_RE.fullmatch(
        f"BG_TASK_TOKEN={expected_token}",
    ):
        return None
    try:
        proc = subprocess.run(
            ["/bin/ps", "axeww", "-o", "pid=,pgid=,command="],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    matches: list[tuple[int, int]] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # Rough split: first field is pid, second is pgid, rest is command+env.
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            pgid = int(parts[1])
        except ValueError:
            continue
        if _RUNNER_TOKEN_RE.search(parts[2]):
            m = _RUNNER_TOKEN_RE.search(parts[2])
            if m and m.group(1) == expected_token:
                matches.append((pid, pgid))
    if not matches:
        return None
    if len(matches) > 1:
        log.warning(
            "bg reconcile: runner_token %s matched %d processes — "
            "using first; investigate token collision",
            expected_token[:8], len(matches),
        )
    return matches[0]


def _decode_b64_tail(
    obj: Any, field: str, manifest_path: str,
) -> Optional[bytes]:
    """Strict base64 decode mirroring bg_tasks_db._replay_completed_manifest's
    tail handling. Strict-mode b64 means a junk manifest surfaces as a WARN
    with the field name, not a silent data-mangling.
    """
    if obj is None:
        return None
    if isinstance(obj, bytes):
        return obj
    if isinstance(obj, str):
        try:
            return base64.b64decode(obj, validate=True)
        except (ValueError, TypeError, binascii.Error) as exc:
            log.warning(
                "bg triage: manifest %s has corrupt %s (base64): %s — "
                "storing NULL", manifest_path, field, exc,
            )
            return None
    log.warning(
        "bg triage: manifest %s %s has unexpected type %s — storing NULL",
        manifest_path, field, type(obj).__name__,
    )
    return None


def _kill_pgid_with_grace(
    pgid: int,
    *,
    grace_s: float = _REAP_SIGTERM_GRACE_S,
    poll_s: float = _REAP_POLL_INTERVAL_S,
) -> str:
    """SIGTERM → wait → SIGKILL. Returns final signal name actually delivered.

    ``pgid`` must have been verified via ``_verify_triple`` on the leader
    before calling this. The function itself does no identity check; it
    assumes the caller has already established that the pgid is safe to
    signal.

    Returns ``'SIGTERM'`` if the group ended during the grace window,
    ``'SIGKILL'`` if the fallback escalation fired.
    """
    try:
        os.killpg(pgid, _signal.SIGTERM)
    except ProcessLookupError:
        return "SIGTERM"  # already gone; treat as successful graceful reap
    except PermissionError:
        # EPERM on killpg with valid pgid on same uid shouldn't happen; log
        # and skip escalation (we'd just get another EPERM).
        log.warning("bg reconcile: EPERM on killpg(%d, SIGTERM)", pgid)
        return "SIGTERM"
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return "SIGTERM"
        except PermissionError:
            # Group is gone to us (it may still exist under another owner,
            # but that means pid reuse happened during grace — absolutely
            # must not escalate). Treat as gone.
            return "SIGTERM"
        time.sleep(poll_s)
    try:
        os.killpg(pgid, _signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        log.warning("bg reconcile: EPERM on killpg(%d, SIGKILL) escalation", pgid)
    return "SIGKILL"


class BgSupervisor:
    """Single per-bridge-process supervisor for background tasks.

    Threading model:
        - 1 UDS listener thread (accept loop with 0.5s timeout for shutdown)
        - 1 poller thread (1s fallback; tick also handles cancel-before-launch)

    Both threads use their own sqlite3 connection — BgTaskRepo docstring
    requires per-thread connections, not a shared one.

    ``stop()`` does NOT terminate running wrappers: they live in their own
    session and outlive bridge by design.
    """

    def __init__(
        self,
        *,
        db_path: Path | str,
        tasks_dir: Path | str,
        sock_path: Path | str,
        bridge_instance_id: Optional[str] = None,
        runner_cmd: Optional[list[str]] = None,
        poll_interval: float = 1.0,
        spawner: Callable[..., Any] = subprocess.Popen,
        enqueue_fn: Optional[Callable[..., Any]] = None,
        bot_id: Optional[str] = None,
        sessions_index: Optional[SessionsIndex] = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._tasks_dir = Path(tasks_dir)
        self._sock_path = Path(sock_path)
        self._bridge_instance_id = bridge_instance_id or uuid.uuid4().hex
        # Default runner_cmd is `python -m feishu_bridge.task_runner` — works
        # without relying on the `task-runner` console_script being on PATH.
        self._runner_cmd = list(runner_cmd) if runner_cmd else [
            sys.executable, "-m", "feishu_bridge.task_runner",
        ]
        self._poll_interval = float(poll_interval)
        self._spawner = spawner
        # 4.5 delivery watcher wiring. None when tests instantiate the
        # supervisor in isolation — `_scan_delivery_outbox` still runs
        # stuck-rollback (pure DB) and then no-ops before touching the
        # queue so unit tests without a full bridge stay stable.
        self._enqueue_fn = enqueue_fn
        self._bot_id = bot_id
        self._sessions_index = sessions_index

        self._stop_evt = threading.Event()
        self._started = False
        self._listen_sock: Optional[socket.socket] = None
        self._listener_thread: Optional[threading.Thread] = None
        self._poller_thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()

        # §6.3 reaper threads — one per orphaned child whose wrapper died but
        # whose child is still alive with no cancel/timeout trigger yet. Each
        # polls ``os.kill(pid, 0)`` until the child exits, then commits the
        # terminal row. Spawned from reconcile(); shut down via stop().
        self._reapers: dict[int, threading.Thread] = {}
        self._reapers_lock = threading.Lock()
        # Only True when *this* instance successfully bound the socket — used
        # to prevent stop() from unlinking a peer's live socket in fallback
        # poller-only mode (addresses review finding #2).
        self._owns_sock_path = False

    # ---- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Idempotent. Ensures DB exists, binds UDS, spins up both threads."""
        with self._start_lock:
            if self._started:
                return
            self._stop_evt.clear()

            # Ensure schema exists; also chmods parent dir 0o700.
            init_db(self._db_path).close()
            self._tasks_dir.mkdir(parents=True, exist_ok=True)

            self._listen_sock = self._bind_listener()  # may be None on bind failure

            # Rollback on partial-success failure: if poller thread fails to
            # start after listener is live, close+unlink the listener so we
            # don't leak a bound fd and a stale socket file.
            try:
                if self._listen_sock is not None:
                    t_listen = threading.Thread(
                        target=self._listener_loop,
                        name="bg-supervisor-listener",
                        daemon=True,
                    )
                    t_listen.start()
                    self._listener_thread = t_listen

                t_poll = threading.Thread(
                    target=self._poller_loop,
                    name="bg-supervisor-poller",
                    daemon=True,
                )
                t_poll.start()
                self._poller_thread = t_poll
            except Exception:
                self._stop_evt.set()
                sock = self._listen_sock
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                    self._listen_sock = None
                if self._owns_sock_path:
                    try:
                        self._sock_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        log.debug("bg-supervisor: unlink on start() rollback: %s", exc)
                    self._owns_sock_path = False
                if self._listener_thread is not None:
                    self._listener_thread.join(timeout=2.0)
                self._listener_thread = None
                self._poller_thread = None
                raise

            self._started = True
            log.info(
                "bg-supervisor started: instance=%s sock=%s listener=%s poll=%.1fs",
                self._bridge_instance_id[:8], self._sock_path,
                "up" if self._listen_sock else "fallback-only",
                self._poll_interval,
            )

    def stop(self, timeout: float = 2.0) -> None:
        """Idempotent. Safe to call before start() (no-op)."""
        with self._start_lock:
            if not self._started:
                return
            self._stop_evt.set()

            # Close socket to unblock any accept() immediately; the 0.5s
            # accept timeout covers the race where close() loses.
            sock = self._listen_sock
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
                self._listen_sock = None

            for t in (self._listener_thread, self._poller_thread):
                if t is not None and t.is_alive():
                    t.join(timeout=timeout)
            self._listener_thread = None
            self._poller_thread = None

            # §6.3: reaper threads check ``_stop_evt`` every poll tick.
            # Snapshot under lock, join without lock to avoid deadlock if a
            # reaper thread happens to call back into the supervisor.
            with self._reapers_lock:
                reapers = list(self._reapers.values())
                self._reapers.clear()
            for t in reapers:
                if t.is_alive():
                    t.join(timeout=timeout)

            # Only unlink the socket file if THIS instance bound it. In
            # poller-only fallback mode another bridge owns it; unlinking
            # would break that peer (review finding #2).
            if self._owns_sock_path:
                try:
                    self._sock_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    log.debug("bg-supervisor: sock unlink on stop: %s", exc)
                self._owns_sock_path = False

            self._started = False
            log.info("bg-supervisor stopped")

    def is_running(self) -> bool:
        return self._started

    # ---- startup reconciler (Section 6) ---------------------------------------

    def reconcile(self) -> dict[str, int]:
        """Run the startup reconciler once, synchronously.

        Must be called BEFORE ``start()``: the DB quarantine step may rename
        ``bg_tasks.db`` out from under any listener/poller connection, so all
        work here runs on a private connection the caller owns.

        Steps follow design.md §Startup Reconciler:
          1. ``PRAGMA integrity_check``; on failure quarantine + replay all
             committed manifests (:func:`rebuild_from_manifests`).
          2. Stale ``launching`` rows → ``failed`` (claimed_at older than 30s).
          3. ``running`` rows → WARN only (deferred: §6.3 liveness triage is
             landed in Commit B with triple-verification + orphan reaper).
          4. Manifest-only backfill (``tasks/active/<id>/task.json.done`` with
             no DB row) — reuses :func:`rebuild_from_manifests` which skips
             rows that already exist, making it idempotent on a live DB.
          5. Stranded ``enqueued`` rows (``enqueued_at IS NULL``) → ``pending``.
             Safety rests on the single-bridge-per-home architectural
             invariant (proposal.md "NOT 多 bridge 分布式"); see
             :func:`_reset_stranded_enqueued` for the full argument and the
             documented launchd-reload-overlap edge case.
          6. Drive ``queued`` rows forward (``_scan_and_launch_queued``).
          7. Drive delivery outbox forward (``_scan_delivery_outbox``); also
             emits ERROR for any ``delivery_failed`` row that has burned its
             full attempt budget.

        Returns a stats dict for logging / tests.
        """
        stats = {
            "quarantined": 0,
            "manifests_replayed": 0,
            "manifest_orphans_created": 0,
            "stale_launching_failed": 0,
            "pre_register_reaped": 0,
            "pre_register_orphaned": 0,
            # §6.3 triage counters (all five branches tallied by
            # _triage_running_rows; zeros kept for stable log shape).
            "running_attached": 0,
            "running_reaped": 0,
            "running_pending_reap": 0,
            "running_orphaned": 0,
            "running_manifest_applied": 0,
            "stranded_enqueued_reset": 0,
            "queued_launched": 0,
            "deliveries_handed_off": 0,
            "retry_budget_exhausted": 0,
            # §6.6 cleanup+archive counters.
            "archived": 0,
            "archive_expired": 0,
            "archive_skipped": 0,
            "quarantine_pruned": 0,
        }

        # Step 1: integrity check (may quarantine + replay).
        conn = None
        try:
            path_before = self._db_path
            existed_before = path_before.exists()
            # integrity_check_and_maybe_quarantine returns the original path
            # regardless of outcome. A missing file after the call means
            # either (a) the DB was quarantined (existed_before=True) or
            # (b) fresh boot (existed_before=False) — very different stories
            # that we must distinguish before setting stats["quarantined"].
            resolved_path = integrity_check_and_maybe_quarantine(path_before)
            file_absent = not resolved_path.exists()
            quarantined = existed_before and file_absent
            fresh_boot = not existed_before and file_absent
            if quarantined:
                stats["quarantined"] = 1
                log.warning(
                    "bg reconcile: DB quarantined; rebuilding from manifests",
                )
                conn = init_db(resolved_path)
                replay_stats = rebuild_from_manifests(conn, self._tasks_dir)
                stats["manifests_replayed"] = replay_stats.get(
                    "completed_replayed", 0,
                )
                stats["manifest_orphans_created"] = replay_stats.get(
                    "orphans_created", 0,
                )
            elif fresh_boot:
                log.info("bg reconcile: fresh install — initialising empty DB")
                conn = init_db(resolved_path)
            else:
                # File exists and is healthy — still go through init_db() so
                # schema migrations run before any recovery query touches a
                # possibly-older schema. init_db() is idempotent.
                conn = init_db(resolved_path)
        except Exception:
            log.exception("bg reconcile: integrity/rebuild failed — aborting")
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            return stats

        repo = BgTaskRepo(conn)
        try:
            # Step 2a: §6.3 pre-register crash window. If bg_runs exists but
            # pid is still NULL, the wrapper died after child spawn but before
            # attach_child() committed the child identity. Reap by token scan
            # before the generic stale-launching sweep marks it merely failed.
            try:
                pre_stats = self._triage_pre_register_launching(repo)
                stats["pre_register_reaped"] = pre_stats["reaped"]
                stats["pre_register_orphaned"] = pre_stats["orphaned"]
            except Exception:
                log.exception("bg reconcile: pre-register triage failed")

            # Step 2b: §6.2 stale launching → failed.
            try:
                stats["stale_launching_failed"] = self._reap_stale_launching(repo)
            except Exception:
                log.exception("bg reconcile: stale launching reap failed")

            # Step 3: §6.3 running-liveness triage (Commit B). Per-row
            # isolation inside _triage_running_rows; an unexpected outer
            # failure here means the query itself exploded.
            try:
                triage_stats = self._triage_running_rows(repo)
                for k, v in triage_stats.items():
                    stats[f"running_{k}"] = v
            except Exception:
                log.exception("bg reconcile: running-row triage failed")

            # Step 4: §6.6 manifest-only backfill (delta catch-up; idempotent
            # on a live DB because _replay_completed_manifest short-circuits
            # when the row already exists). Pass replay_only=True so the
            # active/no-manifest branch does NOT mint orphan bg_tasks rows —
            # on a healthy boot the wrapper may still be alive and writing
            # its manifest; only quarantine recovery (empty DB) should
            # synthesize orphan rows.
            if not stats["quarantined"]:
                try:
                    replay_stats = rebuild_from_manifests(
                        repo.conn, self._tasks_dir, replay_only=True,
                    )
                    stats["manifests_replayed"] = replay_stats.get(
                        "completed_replayed", 0,
                    )
                    stats["manifest_orphans_created"] = replay_stats.get(
                        "orphans_created", 0,
                    )
                except Exception:
                    log.exception(
                        "bg reconcile: manifest backfill failed",
                    )

            # Step 5: stranded `enqueued` rows (enqueued_at IS NULL) → pending.
            try:
                stats["stranded_enqueued_reset"] = self._reset_stranded_enqueued(repo)
            except Exception:
                log.exception("bg reconcile: stranded enqueued reset failed")

            # Step 6: §6.4 queued → launching (normal CAS path).
            try:
                stats["queued_launched"] = self._scan_and_launch_queued(repo)
            except Exception:
                log.exception("bg reconcile: queued scan failed")

            # Step 7: §6.5 delivery outbox + retry-budget ERROR log.
            try:
                self._log_retry_budget_exhausted(repo, stats)
                stats["deliveries_handed_off"] = self._scan_delivery_outbox(repo)
            except Exception:
                log.exception("bg reconcile: delivery outbox scan failed")

            # Step 8: §6.6 archive cleanup + quarantine retention. Runs last
            # so cleanup cannot pull the rug out from under an earlier step
            # that still needed the rows.
            try:
                cleanup_stats = cleanup_and_archive(repo.conn, self._tasks_dir)
                stats["archived"] = cleanup_stats.get("archived", 0)
                stats["archive_expired"] = cleanup_stats.get("expired", 0)
                stats["archive_skipped"] = cleanup_stats.get("skipped", 0)
            except Exception:
                log.exception("bg reconcile: cleanup+archive failed")
            try:
                stats["quarantine_pruned"] = cleanup_quarantine_files(self._db_path)
            except Exception:
                log.exception("bg reconcile: quarantine prune failed")

            log.info(
                "bg reconcile done: pre_register[reaped=%d orphaned=%d] "
                "launching→failed=%d "
                "running[attached=%d reaped=%d pending_reap=%d "
                "orphaned=%d manifest_applied=%d] "
                "stranded-reset=%d manifests=%d orphans=%d queued=%d "
                "deliveries=%d attempts-exhausted=%d "
                "archived=%d expired=%d skipped=%d quarantine_pruned=%d",
                stats["pre_register_reaped"],
                stats["pre_register_orphaned"],
                stats["stale_launching_failed"],
                stats["running_attached"],
                stats["running_reaped"],
                stats["running_pending_reap"],
                stats["running_orphaned"],
                stats["running_manifest_applied"],
                stats["stranded_enqueued_reset"],
                stats["manifests_replayed"],
                stats["manifest_orphans_created"],
                stats["queued_launched"],
                stats["deliveries_handed_off"],
                stats["retry_budget_exhausted"],
                stats["archived"],
                stats["archive_expired"],
                stats["archive_skipped"],
                stats["quarantine_pruned"],
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return stats

    def _reap_stale_launching(self, repo: BgTaskRepo) -> int:
        """§6.2: rows in ``launching`` > 30s → ``failed, launch_interrupted``.

        Direct UPDATE (not set_state_guarded) because we're moving a batch and
        the guard-check index (``idx_bg_tasks_launching``) already scopes the
        predicate narrowly. updated_at is refreshed so the /status command
        surfaces the just-happened recovery.
        """
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - _STALE_LAUNCHING_MS
        cur = repo.conn.execute(
            """UPDATE bg_tasks
                 SET state='failed',
                     reason='launch_interrupted',
                     updated_at=?
               WHERE state='launching'
                 AND claimed_at IS NOT NULL
                 AND claimed_at < ?""",
            (now_ms, cutoff),
        )
        return cur.rowcount or 0

    def _triage_pre_register_launching(self, repo: BgTaskRepo) -> dict[str, int]:
        """§7.5 ``post_spawn_pre_register`` recovery for stale launching rows.

        A wrapper can die after ``phase_p`` created ``bg_runs`` and after it
        spawned the child, but before ``attach_child`` records pid/pgid and
        flips the task to ``running``. The only durable identity anchor left is
        ``runner_token``. Scan the process table for that token; if found, reap
        the matching pgid and mark the task orphan. If not found, mark orphan
        without signaling. Both paths run before the generic stale-launching
        reaper so the task is not mislabeled as a simple pre-Popen failure.
        """
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - _STALE_LAUNCHING_MS
        rows = repo.conn.execute(
            """SELECT t.id AS task_id, r.runner_token
                 FROM bg_tasks t
                 JOIN bg_runs r ON r.task_id = t.id
                WHERE t.state='launching'
                  AND t.claimed_at IS NOT NULL
                  AND t.claimed_at < ?
                  AND r.finished_at IS NULL
                  AND r.pid IS NULL""",
            (cutoff,),
        ).fetchall()

        stats = {"reaped": 0, "orphaned": 0}
        for row in rows:
            task_id = row["task_id"]
            token = row["runner_token"]
            try:
                scan = _scan_ps_for_token(token) if token else None
                if scan is None:
                    log.warning(
                        "bg triage: task=%s stale pre-register row has no "
                        "live token match — marking orphan without signal",
                        task_id,
                    )
                    repo.finalise_pre_register_orphan(
                        task_id=task_id,
                        reason=_REAP_REASON_WRAPPER_DIED_PRE_REGISTER,
                    )
                    stats["orphaned"] += 1
                    continue

                found_pid, found_pgid = scan
                log.warning(
                    "bg triage: task=%s stale pre-register row matched "
                    "pid=%d pgid=%d — reaping and marking orphan",
                    task_id, found_pid, found_pgid,
                )
                sig = _kill_pgid_with_grace(found_pgid)
                repo.finalise_pre_register_orphan(
                    task_id=task_id,
                    reason=_REAP_REASON_WRAPPER_DIED_PRE_REGISTER,
                    signal=sig,
                )
                stats["reaped"] += 1
            except _FinishRace as exc:
                log.info(
                    "bg triage: task=%s pre-register state race: %s",
                    task_id, exc,
                )
            except Exception:
                log.exception(
                    "bg triage: task=%s pre-register triage failed",
                    task_id,
                )
        return stats

    # ---- §6.3 running-row triage (Commit B) ---------------------------------

    def _triage_running_rows(self, repo: BgTaskRepo) -> dict[str, int]:
        """§6.3 full triage: for every ``running`` bg_task joined with an open
        ``bg_runs`` row, classify by wrapper/child liveness and act:

        Returned dict keys (all populated, zero when unused — callers can
        log a fixed shape):
            attached         — wrapper alive (or reaper-cap exceeded); row
                               left in ``running``.
            reaped           — bridge issued SIGTERM/KILL and committed a
                               terminal state this tick (cancel / timeout /
                               pre-register orphan with alive child).
            pending_reap     — reaper thread spawned to watch an alive child
                               after wrapper death; terminal commit deferred.
            orphaned         — both wrapper and child dead (or pre-register
                               with no token match); row marked ``orphan``.
            manifest_applied — wrapper + child dead, but wrapper's last-gasp
                               manifest was on disk; ``finish_run`` replayed
                               it instead of orphan-marking.

        Safety invariant: every ``killpg`` is preceded by ``_verify_triple``
        or ``_scan_ps_for_token`` — on any ambiguity these return fail-closed
        (None/False), and the caller falls through to the no-signal orphan
        branch. Pid reuse cannot cause a stray signal here.
        """
        stats = {
            "attached": 0,
            "reaped": 0,
            "pending_reap": 0,
            "orphaned": 0,
            "manifest_applied": 0,
        }
        rows = repo.conn.execute(
            """SELECT t.id               AS task_id,
                      t.cancel_requested_at,
                      t.timeout_seconds,
                      r.id               AS run_id,
                      r.pid,
                      r.pgid,
                      r.process_start_time_us,
                      r.wrapper_pid,
                      r.wrapper_start_time_us,
                      r.runner_token,
                      r.started_at
                 FROM bg_tasks t
                 JOIN bg_runs  r ON r.task_id = t.id
                WHERE t.state = 'running' AND r.finished_at IS NULL""",
        ).fetchall()

        for row in rows:
            try:
                label = self._triage_one(repo, row)
            except Exception:
                # Per-row isolation: one bad row must never strand the rest.
                log.exception(
                    "bg triage: task %s raised — leaving row in running",
                    row["task_id"],
                )
                label = "attached"
            stats[label] = stats.get(label, 0) + 1
        return stats

    def _triage_one(self, repo: BgTaskRepo, row: Any) -> str:
        """Classify one running row and execute its branch. Returns the
        stats label (``attached|reaped|pending_reap|orphaned|
        manifest_applied``).

        Branches map onto design.md §Startup Reconciler step 3:

            wrapper_alive → attached (skip; wrapper still owns lifecycle)
            wrapper_dead + pid IS NULL → post_spawn_pre_register:
                ps -E scan for token:
                    match      → reap pgid → orphan (reason=pre_register)
                    no match   → orphan, no signal
            wrapper_dead + child_dead:
                manifest exists → finish_run from manifest
                manifest absent → orphan (reason=both_died)
            wrapper_dead + child_alive:
                cancel_requested or timeout exceeded → reap now
                else → spawn reaper thread (or "attached" if cap reached)
        """
        tid = row["task_id"]

        # ---- wrapper liveness -------------------------------------------
        wrapper_pid = row["wrapper_pid"]
        wrapper_start_us = row["wrapper_start_time_us"]
        token = row["runner_token"]

        if _verify_triple(
            wrapper_pid,
            expected_start_us=wrapper_start_us,
            expected_token=token,
        ):
            log.debug(
                "bg triage: task=%s wrapper pid=%s alive — skip",
                tid, wrapper_pid,
            )
            return "attached"

        # ---- wrapper is dead (or unverifiable); check child -------------
        child_pid = row["pid"]

        # Branch: post_spawn_pre_register — wrapper died between Popen and
        # Phase S, so pid/pgid never made it to bg_runs. Only anchor left
        # is the runner_token injected via BG_TASK_TOKEN env.
        if not child_pid or int(child_pid) <= 0:
            return self._triage_pre_register(repo, tid, token)

        # Verify child identity; fail-closed on any mismatch.
        child_alive = _verify_triple(
            int(child_pid),
            expected_start_us=row["process_start_time_us"],
            expected_token=token,
        )
        if not child_alive:
            return self._triage_both_dead(repo, row)

        # ---- child is alive; evaluate triggers --------------------------
        cancel_at = row["cancel_requested_at"]
        started_at_ms = int(row["started_at"] or 0)
        timeout_s = int(row["timeout_seconds"] or 0)
        # wall-clock elapsed since wrapper's Phase S commit. started_at is
        # ms-epoch (set by start_run); compare via wall time since we're
        # across boots (monotonic doesn't survive).
        now_ms = int(time.time() * 1000)
        timed_out = (
            timeout_s > 0
            and started_at_ms > 0
            and (now_ms - started_at_ms) >= timeout_s * 1000
        )

        if cancel_at is not None or timed_out:
            terminal = (
                TaskState.CANCELLED.value if cancel_at is not None
                else TaskState.TIMEOUT.value
            )
            reason = (
                _REAP_REASON_WRAPPER_DIED_POST_REGISTER if cancel_at is not None
                else _REAP_REASON_TIMEOUT
            )
            return self._reap_now(
                repo, tid, int(row["pgid"] or 0), terminal, reason,
            )

        # Child alive, no trigger — bridge takes over via reaper thread.
        spawned = self._spawn_reaper(row)
        if spawned:
            log.info(
                "bg triage: task=%s wrapper dead, child pid=%d alive — "
                "reaper spawned (will commit on child exit)",
                tid, int(child_pid),
            )
            return "pending_reap"
        log.warning(
            "bg triage: task=%s wrapper dead, child pid=%d alive — "
            "reaper cap (%d) reached; row left in running, next boot retries",
            tid, int(child_pid), _REAPER_MAX_THREADS,
        )
        return "attached"

    def _triage_pre_register(
        self, repo: BgTaskRepo, task_id: str, token: Optional[str],
    ) -> str:
        """Wrapper died before Phase S committed pid. Scan ps for token."""
        if not token:
            # No token means row predates Commit A's runner_token column or
            # someone truncated it. Fail-closed → orphan, no signal.
            self._mark_orphan(
                repo, task_id, _REAP_REASON_WRAPPER_DIED_PRE_REGISTER,
            )
            return "orphaned"
        scan = _scan_ps_for_token(token)
        if scan is None:
            log.info(
                "bg triage: task=%s pre-register orphan, no live token match "
                "— marking orphan (no signal)",
                task_id,
            )
            self._mark_orphan(
                repo, task_id, _REAP_REASON_WRAPPER_DIED_PRE_REGISTER,
            )
            return "orphaned"
        found_pid, found_pgid = scan
        log.warning(
            "bg triage: task=%s pre-register orphan; ps matched pid=%d "
            "pgid=%d — reaping and marking orphan",
            task_id, found_pid, found_pgid,
        )
        sig = _kill_pgid_with_grace(found_pgid)
        try:
            repo.finalise_reaped(
                task_id=task_id,
                terminal_state=TaskState.ORPHAN.value,
                reason=_REAP_REASON_WRAPPER_DIED_PRE_REGISTER,
                signal=sig,
            )
        except _FinishRace as exc:
            log.info("bg triage: task=%s pre-register race: %s", task_id, exc)
            return "attached"
        return "reaped"

    def _triage_both_dead(self, repo: BgTaskRepo, row: Any) -> str:
        """Wrapper + child both dead. Prefer manifest replay, else orphan."""
        tid = row["task_id"]
        manifest = self._load_task_manifest(tid)
        if manifest is not None:
            if self._apply_manifest_to_running(repo, row, manifest):
                log.info(
                    "bg triage: task=%s wrapper+child dead, applied "
                    "terminal manifest", tid,
                )
                return "manifest_applied"
        log.warning(
            "bg triage: task=%s wrapper+child dead, no usable manifest "
            "— marking orphan", tid,
        )
        self._mark_orphan(repo, tid, _REAP_REASON_BOTH_DIED)
        return "orphaned"

    def _reap_now(
        self, repo: BgTaskRepo, task_id: str, pgid: int,
        terminal: str, reason: str,
    ) -> str:
        """Send SIGTERM→SIGKILL to a verified pgid, commit terminal state."""
        if pgid <= 0:
            log.warning(
                "bg triage: task=%s %s trigger but pgid=%d — orphan, no signal",
                task_id, terminal, pgid,
            )
            self._mark_orphan(repo, task_id, reason)
            return "orphaned"
        sig = _kill_pgid_with_grace(pgid)
        try:
            repo.finalise_reaped(
                task_id=task_id,
                terminal_state=terminal,
                reason=reason,
                signal=sig,
            )
        except _FinishRace as exc:
            log.info(
                "bg triage: task=%s reap race (%s) — someone else committed",
                task_id, exc,
            )
            return "attached"
        log.warning(
            "bg triage: task=%s reaped wrapper-dead child (pgid=%d, sig=%s, "
            "terminal=%s)", task_id, pgid, sig, terminal,
        )
        return "reaped"

    def _mark_orphan(
        self, repo: BgTaskRepo, task_id: str, reason: str,
    ) -> None:
        """Best-effort orphan commit — swallow _FinishRace (someone else won)."""
        try:
            repo.finalise_reaped(
                task_id=task_id,
                terminal_state=TaskState.ORPHAN.value,
                reason=reason,
            )
        except _FinishRace as exc:
            log.info(
                "bg triage: task=%s orphan commit race (%s)", task_id, exc,
            )

    # ---- §6.3 reaper thread (pending reap) ----------------------------------

    def _spawn_reaper(self, row: Any) -> bool:
        """Start a daemon thread that polls ``os.kill(pid, 0)`` until the
        child exits, then commits the terminal row. Returns False if the
        per-reconcile cap is already reached; callers then leave the row
        in ``running`` for the next boot.

        Idempotency: ``_reapers`` is keyed by pid so repeated ticks within
        the same reconcile (or overlapping reconciles) won't double-watch
        the same child. A pid-reuse between ticks would key the new child
        under the same dict entry — but by that point the original child
        is gone, the old reaper will observe ``ProcessLookupError`` and
        exit, and the new reconcile tick's _verify_triple will fail, so
        the stale dict entry is reaped at worst one interval later.
        """
        pid = int(row["pid"] or 0)
        if pid <= 0:
            return False
        with self._reapers_lock:
            if len(self._reapers) >= _REAPER_MAX_THREADS:
                return False
            if pid in self._reapers:
                return True  # already watching — treat as success
            t = threading.Thread(
                target=self._reaper_worker,
                args=(dict(row),),
                name=f"bg-reaper-{str(row['task_id'])[:8]}",
                daemon=True,
            )
            self._reapers[pid] = t
        t.start()
        return True

    def _reaper_worker(self, row: dict) -> None:
        """Thread body: poll the child's pid; on exit, commit terminal row.

        - ``ProcessLookupError`` → child gone cleanly; read manifest and
          commit (or orphan if missing).
        - ``PermissionError`` (EPERM) → pid reuse under another uid; abort
          without commit. The next reconcile cycle will re-triage.
        - ``_stop_evt.is_set()`` → supervisor shutting down; leave row in
          ``running`` so the next boot picks it up. Do NOT commit a
          half-observed state.
        """
        pid = int(row["pid"])
        task_id = row["task_id"]
        try:
            while not self._stop_evt.is_set():
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break  # child exited normally
                except PermissionError:
                    log.warning(
                        "bg reaper[%s]: pid %d EPERM — pid reuse suspected; "
                        "aborting without DB commit", task_id, pid,
                    )
                    return
                if self._stop_evt.wait(_REAPER_POLL_INTERVAL_S):
                    return
            # Child exited. Commit terminal row on a private connection —
            # the supervisor's main conn is owned by reconcile()'s thread.
            conn = None
            try:
                conn = init_db(self._db_path)
                repo = BgTaskRepo(conn)
                manifest = self._load_task_manifest(task_id)
                if manifest is not None:
                    if self._apply_manifest_to_running(repo, row, manifest):
                        log.info(
                            "bg reaper[%s]: manifest applied post-exit",
                            task_id,
                        )
                        return
                # No manifest or apply refused: orphan.
                try:
                    repo.finalise_reaped(
                        task_id=task_id,
                        terminal_state=TaskState.ORPHAN.value,
                        reason="wrapper_dead_child_died_no_manifest",
                    )
                    log.warning(
                        "bg reaper[%s]: child exited, no manifest — "
                        "marked orphan", task_id,
                    )
                except _FinishRace as exc:
                    log.info(
                        "bg reaper[%s]: orphan commit race (%s)",
                        task_id, exc,
                    )
            except Exception:
                log.exception(
                    "bg reaper[%s]: unexpected error committing terminal",
                    task_id,
                )
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        finally:
            with self._reapers_lock:
                self._reapers.pop(pid, None)

    # ---- §6.3 manifest helpers ---------------------------------------------

    def _load_task_manifest(self, task_id: str) -> Optional[dict]:
        """Try ``active/<tid>/task.json.done`` then ``completed/<tid>/...``.

        Refuses symlinks (matches _is_trusted_task_dir semantics). Returns
        parsed dict or None on missing/corrupt.
        """
        for subdir in ("active", "completed"):
            p = self._tasks_dir / subdir / task_id / "task.json.done"
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                return json.loads(p.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                log.warning(
                    "bg triage: manifest %s unreadable: %s", p, exc,
                )
                return None
        return None

    def _apply_manifest_to_running(
        self, repo: BgTaskRepo, row: Any, manifest: dict,
    ) -> bool:
        """Apply a wrapper-committed manifest to an existing running row via
        ``finish_run``. Returns False on any refusal (state not terminal,
        _FinishRace, ValueError) — caller falls back to orphan.
        """
        state = manifest.get("state")
        if not isinstance(state, str) or not TaskState.is_terminal(state):
            log.warning(
                "bg triage: manifest state=%r not terminal — skipping apply",
                state,
            )
            return False

        # Find the manifest path we actually loaded (matches _load_task_manifest
        # search order). This is what finish_run persists so future queries
        # can locate the on-disk payload.
        tid = row["task_id"]
        manifest_path: Optional[str] = None
        manifest_subdir: Optional[str] = None
        for subdir in ("active", "completed"):
            cand = self._tasks_dir / subdir / tid / "task.json.done"
            if cand.is_file() and not cand.is_symlink():
                manifest_path = str(cand)
                manifest_subdir = subdir
                break
        if manifest_path is None:
            # Race: file existed at _load_task_manifest and is now gone.
            return False

        stdout_tail = _decode_b64_tail(
            manifest.get("stdout_tail_b64")
            if manifest.get("stdout_tail_b64") is not None
            else manifest.get("stdout_tail"),
            "stdout_tail", manifest_path,
        )
        stderr_tail = _decode_b64_tail(
            manifest.get("stderr_tail_b64")
            if manifest.get("stderr_tail_b64") is not None
            else manifest.get("stderr_tail"),
            "stderr_tail", manifest_path,
        )
        try:
            repo.finish_run(
                run_id=int(row["run_id"]),
                task_id=tid,
                terminal_state=state,
                exit_code=manifest.get("exit_code"),
                signal=manifest.get("signal"),
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                manifest_path=manifest_path,
                reason=manifest.get("reason"),
            )
        except _FinishRace as exc:
            log.info(
                "bg triage: finish_run race on task=%s (%s)", tid, exc,
            )
            return False
        except ValueError as exc:
            log.warning(
                "bg triage: finish_run refused task=%s: %s", tid, exc,
            )
            return False
        # Wrapper Phase C3 (mv active/ → completed/) was skipped because the
        # wrapper died before it could run. Do it ourselves now so
        # cleanup_and_archive can scope its archival pass to completed/ only.
        # Bounded crash window: if we die between finish_run (durable) and
        # rename, the row is terminal but active/<tid>/ sits stranded until
        # operator cleanup. Accepted — the row won't resurface for triage.
        if manifest_subdir == "active":
            promote_active_to_completed(self._tasks_dir, tid)
        return True

    def _reset_stranded_enqueued(self, repo: BgTaskRepo) -> int:
        """Reset ``enqueued_at IS NULL`` rows back to ``pending``.

        These are rows the previous bridge's supervisor CAS-claimed from
        ``pending`` into ``enqueued``, but whose worker never stamped
        ``enqueued_at`` before the bridge died. The 5-min stuck-rollback in
        ``_scan_delivery_outbox`` skips them (its guard requires
        ``enqueued_at IS NOT NULL``), so without this reset they would be
        stranded forever.

        Safety rests on the architectural invariant that a bg-tasks home
        (``~/.feishu-bridge/``) hosts exactly one active bridge process
        (proposal.md "NOT 多机/多 bridge 分布式执行"). A previous bridge's
        in-memory ChatTaskQueue and worker threads die with the process, so
        at reconcile time no worker is ever in-flight on a stranded row.

        Known edge case (design.md:169 "launchd reload 重叠窗口"): two bridges
        overlap briefly. In that window a concurrent worker between its
        watcher's CAS and its ``_bg_mark_dequeued`` stamp could be racing
        this reset. Accepted risk: the worker discards ``_bg_mark_dequeued``'s
        return value (worker.py:927) and proceeds to send, so a reset during
        that millisecond-level gap could produce double delivery. The race
        is bounded by launchd's kill-old-then-start-new sequencing; the
        tradeoff is accepted over stranding rows forever.
        """
        cur = repo.conn.execute(
            """UPDATE bg_runs
                 SET delivery_state='pending'
               WHERE delivery_state='enqueued'
                 AND enqueued_at IS NULL""",
        )
        return cur.rowcount or 0

    def _log_retry_budget_exhausted(
        self, repo: BgTaskRepo, stats: dict[str, int],
    ) -> None:
        """Emit ERROR for delivery_failed rows that have burned their budget."""
        rows = repo.conn.execute(
            """SELECT id, task_id, delivery_attempt_count, delivery_error
                 FROM bg_runs
                WHERE delivery_state='delivery_failed'
                  AND delivery_attempt_count >= ?""",
            (_DELIVERY_ATTEMPT_CAP,),
        ).fetchall()
        stats["retry_budget_exhausted"] = len(rows)
        for r in rows:
            log.error(
                "bg reconcile: run %s (task=%s) exhausted delivery retries "
                "(%d/%d): last error=%r — operator intervention required",
                r["id"], r["task_id"], r["delivery_attempt_count"],
                _DELIVERY_ATTEMPT_CAP, r["delivery_error"],
            )

    # ---- UDS bind -------------------------------------------------------------

    def _bind_listener(self) -> Optional[socket.socket]:
        """Bind wake.sock with EADDRINUSE probe+unlink fallback.

        Returns the bound socket on success, ``None`` if both attempts fail
        (in which case the caller proceeds with poller-only mode).
        """
        p = self._sock_path
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(p.parent, 0o700)
        except OSError as exc:
            log.debug("bg-supervisor: chmod parent 0700: %s", exc)

        try:
            return self._try_bind()
        except OSError as first:
            if first.errno != errno.EADDRINUSE:
                log.warning("bg-supervisor: wake.sock bind failed: %s", first)
                return None

        # EADDRINUSE: is someone still listening?
        if self._probe_existing_listener():
            log.warning(
                "bg-supervisor: another bridge holds wake.sock (%s); "
                "this instance falls back to poller-only", p,
            )
            return None

        # Stale socket file — safe to unlink + retry.
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("bg-supervisor: unlink stale wake.sock failed: %s", exc)
            return None

        try:
            return self._try_bind()
        except OSError as exc:
            log.warning("bg-supervisor: wake.sock rebind failed: %s", exc)
            return None

    def _try_bind(self) -> socket.socket:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(str(self._sock_path))
            try:
                os.chmod(self._sock_path, 0o600)
            except OSError as exc:
                log.debug("bg-supervisor: chmod 0600 sock: %s", exc)
            s.listen(_LISTEN_BACKLOG)
            s.settimeout(_ACCEPT_TIMEOUT_S)
            # Mark ownership only after bind+listen succeed.
            self._owns_sock_path = True
            return s
        except OSError:
            s.close()
            raise

    def _probe_existing_listener(self) -> bool:
        """True if some process accepts() on sock_path right now."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(_PROBE_TIMEOUT_S)
        try:
            s.connect(str(self._sock_path))
            return True
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            return False
        finally:
            try:
                s.close()
            except OSError:
                pass

    # ---- listener thread ------------------------------------------------------

    def _listener_loop(self) -> None:
        try:
            conn = connect(self._db_path)
        except Exception as exc:
            log.error("bg-supervisor listener: DB connect failed: %s", exc)
            # Close the listener so a peer's _probe_existing_listener() sees
            # this socket as dead, not as a silent blackhole accepting
            # connections no handler ever drains (review finding #4).
            sock = self._listen_sock
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
                self._listen_sock = None
            return
        repo = BgTaskRepo(conn)
        try:
            while not self._stop_evt.is_set():
                sock = self._listen_sock
                if sock is None:
                    break
                try:
                    client, _ = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    # Socket was closed by stop(); exit cleanly.
                    if self._stop_evt.is_set():
                        break
                    log.exception("bg-supervisor listener: accept error")
                    continue

                try:
                    client.settimeout(_RECV_TIMEOUT_S)
                    try:
                        payload = client.recv(_WAKE_RECV_BYTES)
                    except (socket.timeout, OSError):
                        payload = b""
                finally:
                    try:
                        client.close()
                    except OSError:
                        pass

                try:
                    self._handle_payload(repo, payload)
                except Exception:
                    log.exception("bg-supervisor: payload handler crashed")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _handle_payload(self, repo: BgTaskRepo, payload: bytes) -> None:
        if not payload:
            return
        kind = payload[0:1]
        if kind == b"\x01":
            self._scan_and_launch_queued(repo)
            self._scan_delivery_outbox(repo)
            return
        if kind in (b"\x02", b"\x03"):
            raw = payload[1:17]
            if len(raw) != 16:
                return
            try:
                task_id = uuid.UUID(bytes=raw).hex
            except ValueError:
                return
            if kind == b"\x02":
                self._launch_specific(repo, task_id)
            else:
                # \x03: wrapper signaled delivery ready. 4.5 watcher processes;
                # the seam below is the hook, currently empty.
                self._scan_delivery_outbox(repo)
            return
        log.debug("bg-supervisor: unknown wake payload kind %r", kind)

    # ---- poller thread --------------------------------------------------------

    def _poller_loop(self) -> None:
        try:
            conn = connect(self._db_path)
        except Exception as exc:
            log.error("bg-supervisor poller: DB connect failed: %s", exc)
            return
        repo = BgTaskRepo(conn)
        try:
            while not self._stop_evt.is_set():
                try:
                    self._flip_cancel_requested_queued(repo)
                    self._scan_and_launch_queued(repo)
                    self._scan_delivery_outbox(repo)
                except Exception:
                    log.exception("bg-supervisor poller: tick crashed")
                # stop_evt.wait returns True when set — exits promptly on stop.
                if self._stop_evt.wait(self._poll_interval):
                    break
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ---- scan + launch --------------------------------------------------------

    def _scan_and_launch_queued(self, repo: BgTaskRepo) -> int:
        ids = repo.list_queued_for_launch(limit=16)
        launched = 0
        for tid in ids:
            if self._launch_specific(repo, tid):
                launched += 1
        return launched

    def _launch_specific(self, repo: BgTaskRepo, task_id: str) -> bool:
        """CAS-claim ``task_id`` and spawn wrapper. Idempotent.

        Returns True iff this call both won the CAS and the Popen succeeded.
        A lost CAS is silent (another caller won); a Popen failure rolls the
        claim back to ``failed`` so the reconciler doesn't wait 30s.
        """
        won = repo.claim_queued_cas(task_id, self._bridge_instance_id)
        if not won:
            return False

        runner_token = uuid.uuid4().hex
        argv = [
            *self._runner_cmd,
            "--task-id", task_id,
            "--db-path", str(self._db_path),
            "--tasks-dir", str(self._tasks_dir),
            "--runner-token", runner_token,
        ]
        env = {**os.environ, "BG_TASK_TOKEN": runner_token}

        try:
            proc = self._spawner(
                argv,
                shell=False,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                env=env,
            )
        except (OSError, ValueError) as exc:
            log.error(
                "bg-supervisor: task %s spawn failed: %s — rolling claim back",
                task_id, exc,
            )
            try:
                repo.set_state_guarded(
                    task_id,
                    expected_from="launching",
                    new_state="failed",
                    reason="spawn_failed",
                    error_message=str(exc),
                )
            except Exception:
                log.exception("bg-supervisor: rollback to failed also errored")
            return False

        log.info(
            "bg-supervisor: task %s spawned wrapper pid=%s token=%s",
            task_id, getattr(proc, "pid", "?"), runner_token[:8],
        )
        return True

    # ---- cancel-before-launch -------------------------------------------------

    def _flip_cancel_requested_queued(self, repo: BgTaskRepo) -> int:
        """Flip queued+cancel_requested rows to cancelled (Cancel SLO ≤10s)."""
        rows = repo.conn.execute(
            "SELECT id FROM bg_tasks "
            "WHERE state='queued' AND cancel_requested_at IS NOT NULL"
        ).fetchall()
        flipped = 0
        for r in rows:
            try:
                if repo.set_state_guarded(
                    r["id"],
                    expected_from="queued",
                    new_state="cancelled",
                    reason="cancelled_before_launch",
                ):
                    flipped += 1
            except ValueError:
                # state machine rejected — someone already moved the row.
                pass
        return flipped

    # ---- delivery watcher (Section 4.5) --------------------------------------

    def _scan_delivery_outbox(self, repo: BgTaskRepo) -> int:
        """Drain pending bg-run deliveries into the chat queue.

        Steps, in order:
          1. Rollback runs stuck in ``enqueued`` > 5 min (bridge crashed
             between CAS and the worker consuming the item). NO attempt
             bump — crash recovery isn't a genuine delivery failure.
          2. Early-return if the supervisor was constructed without
             ``enqueue_fn`` (unit tests exercise the DB-only paths).
          3. For each pending/delivery_failed run: resolve session resume
             status, build the synthetic turn, CAS-claim the row into
             ``enqueued``, and hand to ``enqueue_fn``. An enqueue exception
             drops the row back to ``delivery_failed`` with ``bump_attempt``.

        Returns the number of rows successfully handed to ``enqueue_fn``.

        Depends on ``connect()`` running in autocommit mode
        (``isolation_level=None`` per bg_tasks_db.py:210). The stuck-enqueued
        UPDATE and the CAS below must be visible cross-connection immediately,
        otherwise concurrent supervisors would double-deliver.
        """
        now_ms = int(time.time() * 1000)

        # Step 1: stuck-enqueued rollback. Race with worker's own
        # `expected_from='enqueued'` CAS is resolved by whichever UPDATE
        # wins; loser is a silent no-op.
        try:
            repo.conn.execute(
                """UPDATE bg_runs
                   SET delivery_state='pending', enqueued_at=NULL
                   WHERE delivery_state='enqueued'
                     AND enqueued_at IS NOT NULL
                     AND enqueued_at < ?""",
                (now_ms - _STUCK_ENQUEUED_MS,),
            )
        except Exception:
            log.exception("bg-supervisor: stuck-enqueued rollback failed")

        if self._enqueue_fn is None:
            return 0

        try:
            pending = repo.list_pending_deliveries(limit=_DELIVERY_BATCH_LIMIT)
        except Exception:
            log.exception("bg-supervisor: list_pending_deliveries failed")
            return 0

        delivered = 0
        for run_row in pending:
            run_id = run_row["id"]
            task_id = run_row["task_id"]
            try:
                task_row = repo.get(task_id)
            except Exception:
                log.exception("bg-supervisor: repo.get(%s) failed", task_id)
                continue
            if task_row is None:
                # Orphan run (bg_tasks row deleted but bg_runs row survived —
                # FK CASCADE should prevent this, but DB corruption or
                # reconciler bugs could produce it). Mark terminal so the
                # attempt_count<10 retry cap eventually retires the row
                # instead of looping forever on every poll tick.
                log.warning(
                    "bg-supervisor: run %s references missing task %s — "
                    "marking delivery_failed", run_id, task_id,
                )
                try:
                    repo.mark_delivery_state(
                        run_id, "delivery_failed",
                        expected_from=run_row["delivery_state"],
                        bump_attempt=True,
                        error=f"missing_task:{task_id}",
                    )
                except Exception:
                    log.exception(
                        "bg-supervisor: mark orphan run %s failed", run_id,
                    )
                continue

            try:
                status, reason = resolve_resume_status(
                    task_row.session_id, self._sessions_index, now_ms,
                )
            except Exception:
                log.exception(
                    "bg-supervisor: resolve_resume_status crashed for run %s",
                    run_id,
                )
                status, reason = ("resume_failed", "resolve_exception")

            # Build synthetic turn from run_row + task_row. Wrapper writes
            # tails as BLOB; build_synthetic_turn expects bytes, so pass
            # through untouched (None → b"" via `or b""`).
            duration_s = 0
            finished = run_row["finished_at"]
            started = run_row["started_at"]
            if isinstance(finished, int) and isinstance(started, int):
                duration_s = max(0, (finished - started) // 1000)
            synthetic = build_synthetic_turn(
                task_id=task_id,
                manifest_path=run_row["manifest_path"] or "",
                state=task_row.state,
                reason=task_row.reason,
                duration_seconds=duration_s,
                exit_code=run_row["exit_code"],
                signal=run_row["signal"],
                output_paths=task_row.output_paths or [],
                stdout_tail=run_row["stdout_tail"] or b"",
                stderr_tail=run_row["stderr_tail"] or b"",
                on_done_prompt=task_row.on_done_prompt or "",
            )

            # design.md:517 — both `fresh_fallback` and `resume_failed` go
            # through the new-session + NOTE branch. Only `resumed` keeps the
            # stored sid. Routing `resume_failed` back into resume would defeat
            # the whole point of the probe: we already know resume won't work.
            if status in ("fresh_fallback", "resume_failed"):
                prompt = build_fresh_fallback_prefix(reason) + "\n\n" + synthetic
                effective_sid: Optional[str] = None
            else:
                prompt = synthetic
                effective_sid = task_row.session_id

            # CAS: pending → enqueued. If we lose (another scheduler won
            # this row, or it was cancelled), skip silently.
            # enqueued_at is intentionally NOT stamped here — worker.py stamps
            # it at dequeue time via `_bg_mark_dequeued`. This scopes the
            # stuck-rollback's 5 min to "worker picked up but never ack'd"
            # (i.e. bridge-crash recovery, per design.md:391), matching the
            # spec's crash-only intent rather than bounding total queue-wait +
            # turn time, which would cause duplicate delivery on long turns.
            try:
                claimed = repo.mark_delivery_state(
                    run_id, "enqueued",
                    expected_from=run_row["delivery_state"],
                    session_resume_status=f"{status}:{reason}",
                )
            except Exception:
                log.exception(
                    "bg-supervisor: CAS to enqueued failed for run %s", run_id,
                )
                continue
            if not claimed:
                continue

            session_key = SessionMap.format_key(
                (self._bot_id, task_row.chat_id, task_row.thread_id),
            )
            try:
                self._enqueue_fn(
                    chat_id=task_row.chat_id,
                    session_key=session_key,
                    prompt=prompt,
                    kind="bg_task_completion",
                    session_id=effective_sid,
                    extras={
                        "_bg_run_id": run_id,
                        "thread_id": task_row.thread_id,
                    },
                )
                delivered += 1
            except Exception as e:
                log.exception(
                    "bg-supervisor: enqueue_fn raised for run %s", run_id,
                )
                try:
                    repo.mark_delivery_state(
                        run_id, "delivery_failed",
                        expected_from="enqueued",
                        bump_attempt=True,
                        error=f"enqueue_failed: {type(e).__name__}: {e}",
                    )
                except Exception:
                    log.exception(
                        "bg-supervisor: rollback to delivery_failed also failed "
                        "for run %s", run_id,
                    )
        return delivered
