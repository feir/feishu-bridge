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

import errno
import logging
import os
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
    connect,
    init_db,
    integrity_check_and_maybe_quarantine,
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
            "running_rows_observed": 0,
            "stranded_enqueued_reset": 0,
            "queued_launched": 0,
            "deliveries_handed_off": 0,
            "retry_budget_exhausted": 0,
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
            # Step 2: §6.2 stale launching → failed.
            try:
                stats["stale_launching_failed"] = self._reap_stale_launching(repo)
            except Exception:
                log.exception("bg reconcile: stale launching reap failed")

            # Step 3: §6.3 running-liveness — WARN stub until Commit B.
            try:
                stats["running_rows_observed"] = self._warn_running_rows(repo)
            except Exception:
                log.exception("bg reconcile: running-row warn stub failed")

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

            log.info(
                "bg reconcile done: launching→failed=%d running-warned=%d "
                "stranded-reset=%d manifests=%d orphans=%d queued=%d "
                "deliveries=%d attempts-exhausted=%d",
                stats["stale_launching_failed"],
                stats["running_rows_observed"],
                stats["stranded_enqueued_reset"],
                stats["manifests_replayed"],
                stats["manifest_orphans_created"],
                stats["queued_launched"],
                stats["deliveries_handed_off"],
                stats["retry_budget_exhausted"],
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

    def _warn_running_rows(self, repo: BgTaskRepo) -> int:
        """§6.3 stub — Commit B ships the triple-verification triage.

        Until then we log (at WARN) how many ``running`` rows survived across
        the boot so operators can spot a stuck reconciler. Every row here is
        a candidate for orphan-alive-bridge-reap; the current version leaves
        them alone rather than risk SIGKILL to a reused pid.
        """
        count = repo.conn.execute(
            "SELECT COUNT(*) FROM bg_tasks WHERE state='running'",
        ).fetchone()[0]
        if count:
            log.warning(
                "bg reconcile: %d running row(s) carried across boot — "
                "Section 6.3 liveness triage not yet active (Commit B scope); "
                "rows remain untouched until timeout or explicit cancel",
                count,
            )
        return int(count)

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
