"""Session resume fallback for bg-task synthetic turns (Section 5.4a).

Three pieces that compose into the delivery-watcher policy:

1. ``SessionsIndex`` — thread-safe in-memory dict persisted to
   ``~/.feishu-bridge/sessions.json`` via atomic tempfile+rename. Keyed by
   Claude session UUID (the string ``claude -p --resume <id>`` consumes),
   not by bridge's ``session_key`` (bot:chat:thread) — different concepts.

2. ``sentinel_probe(session_id)`` — 5 s subprocess call
   ``claude -p --resume <id> -p ":probe:"`` that classifies the session
   as still-resumable or a terminal failure.

3. ``resolve_resume_status(...)`` — pure policy function. Takes the index,
   ``now_ms``, and a ``probe_fn`` so tests can inject deterministic probe
   outcomes without spawning real CLI subprocesses.

This module is *only* the foundation. Section 5.4b wires ``touch()`` into
the worker post-turn (where the Claude UUID is first known) and prepends
the NOTE prefix in the delivery watcher.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path

log = logging.getLogger(__name__)


STALE_THRESHOLD_MS = 24 * 60 * 60 * 1000  # 24h — matches design.md
PROBE_TIMEOUT_SEC = 5.0

# Verbatim template from design.md §Session Resume Fallback.
# {reason} is the machine-readable tag from resolve_resume_status
# (e.g. "probe_timeout", "session_not_found").
FRESH_FALLBACK_NOTE_TEMPLATE = (
    "[NOTE: original session no longer resumable (reason: {reason}); "
    "this is a fresh-context bg-task completion. Previously-discussed "
    "context is NOT available — read output files for ground truth.]"
)


class SessionsIndex:
    """In-memory map of Claude session_id → last-seen metadata.

    Persisted lazily to JSON on each ``touch()`` via atomic
    tempfile+os.replace so a crash mid-write leaves the old file intact.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as e:
            log.warning("sessions_index: read failed at %s: %s", self._path, e)
            return
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning("sessions_index: corrupt JSON at %s (%s) — starting empty",
                        self._path, e)
            return
        if isinstance(parsed, dict):
            # Defensive: drop entries with non-dict values so callers can
            # assume ``lookup`` returns dict | None.
            self._data = {
                k: v for k, v in parsed.items()
                if isinstance(k, str) and isinstance(v, dict)
            }

    def _save_locked(self) -> None:
        """Atomic write. Caller must hold ``self._lock``."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".sessions.", suffix=".json.tmp", dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def touch(self, session_id: str, chat_id: str, now_ms: int) -> None:
        """Record/refresh last-seen metadata for ``session_id``.

        Persists to disk on every call. Infrequent (once per completed
        turn), so O(n) JSON serialization is fine at our scale (dozens
        to low hundreds of sessions).
        """
        if not session_id:
            return
        with self._lock:
            self._data[session_id] = {
                "last_seen_at_ms": int(now_ms),
                "chat_id": str(chat_id) if chat_id else None,
            }
            self._save_locked()

    def lookup(self, session_id: str) -> dict | None:
        """Return a shallow copy of the entry or None."""
        if not session_id:
            return None
        with self._lock:
            entry = self._data.get(session_id)
            return dict(entry) if entry is not None else None


def sentinel_probe(
    session_id: str,
    *,
    timeout_sec: float = PROBE_TIMEOUT_SEC,
    claude_bin: str = "claude",
) -> tuple[str, str]:
    """Probe whether ``session_id`` is still resumable.

    Returns ``(status, reason)`` where status is one of
    ``{"resumed", "fresh_fallback", "resume_failed"}`` and reason is a
    short machine-readable tag. The caller prepends ``reason`` into the
    NOTE prefix verbatim.

    Classification:
      * ``TimeoutExpired``         → ("fresh_fallback", "probe_timeout")
      * exit=0                     → ("resumed", "probe_ok")
      * exit!=0 + stderr matches   → ("fresh_fallback", "session_not_found")
        session-gone signatures
      * exit!=0 otherwise          → ("resume_failed", "probe_error")
      * unexpected exception       → ("resume_failed", "probe_exception")
    """
    cmd = [claude_bin, "-p", "--resume", session_id, "-p", ":probe:"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ("fresh_fallback", "probe_timeout")
    except FileNotFoundError:
        # claude binary missing — treat as resume_failed so caller
        # doesn't silently degrade every session to fresh.
        return ("resume_failed", "claude_not_found")
    except Exception as e:  # pragma: no cover - defensive
        log.warning("sentinel_probe: unexpected %s: %s", type(e).__name__, e)
        return ("resume_failed", "probe_exception")

    if proc.returncode == 0:
        return ("resumed", "probe_ok")

    stderr = (proc.stderr or "").lower()
    session_gone = (
        "session not found" in stderr
        or "no such session" in stderr
        or "compacted" in stderr
    )
    if session_gone:
        return ("fresh_fallback", "session_not_found")
    return ("resume_failed", "probe_error")


def resolve_resume_status(
    session_id: str | None,
    index: SessionsIndex,
    now_ms: int,
    probe_fn=sentinel_probe,
    *,
    stale_threshold_ms: int = STALE_THRESHOLD_MS,
) -> tuple[str, str]:
    """Pure policy deciding how to handle a bg-task completion turn.

    Returns ``(status, reason)``:
      * ``("resumed", "recent_activity")`` — seen within threshold
      * ``("resumed", "probe_ok")`` — stale but probe succeeded
      * ``("fresh_fallback", "not_in_index")`` — never seen
      * ``("fresh_fallback", "probe_timeout" | "session_not_found")``
      * ``("resume_failed", "probe_error" | "claude_not_found")``
    """
    if not session_id:
        return ("fresh_fallback", "not_in_index")

    entry = index.lookup(session_id)
    if entry is None:
        return ("fresh_fallback", "not_in_index")

    last_seen = entry.get("last_seen_at_ms")
    if isinstance(last_seen, int):
        age = now_ms - last_seen
        # Require 0 <= age < threshold. Negative age (future timestamp from
        # clock rollback / hand-edited JSON / NTP jump) falls through to
        # probe — cheaper to pay 5s once than to trust a bogus timestamp.
        if 0 <= age < stale_threshold_ms:
            return ("resumed", "recent_activity")

    # Stale, missing, or negative-age — pay for one probe. Injection
    # promises callers can swap probes; enforce the return-tuple
    # contract here so a buggy probe can't crash the delivery watcher.
    try:
        return probe_fn(session_id)
    except Exception as e:
        log.warning("resolve_resume_status: probe raised %s: %s",
                    type(e).__name__, e)
        return ("resume_failed", "probe_exception")


def build_fresh_fallback_prefix(reason: str) -> str:
    """Render the NOTE prefix using the verbatim design.md template."""
    return FRESH_FALLBACK_NOTE_TEMPLATE.format(reason=reason or "unknown")
