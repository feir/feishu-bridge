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
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from feishu_bridge.bg_tasks_db import BgTaskRepo, connect, init_db

log = logging.getLogger("feishu-bridge.bg-supervisor")

# Wake payload sizes. Longer payloads are truncated by recv().
_WAKE_RECV_BYTES = 17  # 1 kind byte + 16 UUID bytes
_ACCEPT_TIMEOUT_S = 0.5
_RECV_TIMEOUT_S = 0.5
_PROBE_TIMEOUT_S = 0.2
_LISTEN_BACKLOG = 8


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

    # ---- delivery seam (Section 4.5 fills) -----------------------------------

    def _scan_delivery_outbox(self, repo: BgTaskRepo) -> int:
        """Hook for the delivery watcher (Section 4.5).

        The poller and every wake kind already route here so 4.5 only has to
        fill the body; the wiring is done.
        """
        return 0
