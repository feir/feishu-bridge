"""Bridge-owned per-turn session journal (Phase 6.3).

Minimal append-only log of turn boundaries, workflow events, and artifact paths,
scoped by (bot_id, chat_id, thread_id). Observational-only for Claude: the
journal is a bridge artifact, not a Claude session replacement. Future phases
(6.6 /status, 6.7 richer consumption) read from this surface.

Storage:
    $FEISHU_BRIDGE_HOME/journals/<sha1(bot_id|chat_id|thread_id)>.jsonl
    $FEISHU_BRIDGE_HOME/journals/<sha1(...)>.jsonl.lock  (sidecar advisory lock)

Entry schema::

    {
        "ts": <float epoch seconds>,
        "kind": "user_turn" | "assistant_turn" | "workflow_event" | "artifact",
        "runner_type": "claude" | "pi" | "codex" | "local",
        "provider": str | None,
        "model": str | None,
        "session_id": str | None,
        "text": str,                # for user_turn/assistant_turn
        "truncated": bool,
        "redactions": int,
        # workflow_event only:
        "command": str, "decision": str,
        # artifact only:
        "path": str,
        # workflow_event/artifact also carry a "redactions" count and a
        # "truncated" flag when their sanitized fields were modified.
    }

Privacy rules:
- Never serialize env vars or raw auth headers.
- Truncate user text > USER_MAX_BYTES, assistant text > ASSISTANT_MAX_BYTES.
- Workflow/artifact fields also sanitized at tighter caps
  (WORKFLOW_COMMAND_MAX_BYTES / WORKFLOW_DECISION_MAX_BYTES /
  ARTIFACT_PATH_MAX_BYTES) so callers can't accidentally persist unbounded
  user text or secret-bearing URLs through these low-traffic kinds.
- Redact common secret shapes: `sk-…` / `sk-ant-…`, bearer tokens, AWS
  `AKIA…`/`ASIA…` access keys, Slack `xox?-…`, GitHub `ghp_…` /
  `github_pat_…` / `gh[ousr]_…`, raw JWTs (3 base64 segments), and
  SHA-256+ hex blobs (64+ chars). The hex rule was narrowed from 40+ to
  avoid eating 40-char git commit SHAs and UUIDs-without-hyphens that
  carry useful engineering context.
- All failures swallowed by caller's try/except wrap — journal must never
  propagate an exception into the turn flow.

Concurrency:
- Per-scope advisory `fcntl.flock` (sidecar `.lock` file) brackets append
  and prune as one critical section. Without the lock, O_APPEND protects
  a single write but not the composite: a pruner reads the old tail, a
  writer appends, then `os.replace` overwrites and the appended line is
  lost. POSIX-only; bridge only runs on macOS/Linux.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from feishu_bridge.paths import bridge_home

# Privacy constants — kept module-level so tests can monkeypatch.
USER_MAX_BYTES = 16 * 1024
ASSISTANT_MAX_BYTES = 32 * 1024
# Tighter caps for workflow/artifact kinds: callers should only pass
# normalized short values (`/plan`, `bridge_workflow`, known paths). The
# caps are a backstop against "future caller forwards raw user text" bugs.
WORKFLOW_COMMAND_MAX_BYTES = 256
WORKFLOW_DECISION_MAX_BYTES = 256
ARTIFACT_PATH_MAX_BYTES = 2048
MAX_ENTRIES = 500

_TRUNC_MARKER = "…[TRUNCATED]"
_REDACT_MARKER = "[REDACTED]"

# Redaction patterns (applied in order). Ordered roughly strict-prefix-first
# so sk-ant-… matches before the generic sk-… rule.
_REDACT_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    # Bearer tokens (case-insensitive header form).
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    # AWS access keys: AKIA (long-term) and ASIA (temporary STS).
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # Slack tokens: xoxb/xoxp/xoxa/xoxr/xoxs.
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    # GitHub tokens: classic PAT (ghp_), fine-grained (github_pat_), and
    # app/OAuth variants gho_/ghu_/ghs_/ghr_.
    re.compile(r"\bghp_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgh[ousr]_[A-Za-z0-9]{30,}\b"),
    # Raw JWTs: eyJ-prefixed header . payload . signature (base64url).
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    # SHA-256+ hex blobs (narrowed from 40 → 64 so commit SHAs and
    # UUIDs-without-hyphens pass through; real secrets tend to be ≥ 64 hex).
    re.compile(r"\b[a-f0-9]{64,}\b"),
)


def _redact(text: str) -> tuple[str, int]:
    count = 0
    for pat in _REDACT_PATTERNS:
        new_text, n = pat.subn(_REDACT_MARKER, text)
        count += n
        text = new_text
    return text, count


def _truncate(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    # Reserve room for the marker, cut on byte boundary, decode with
    # errors='ignore' to drop any split code-point fragment.
    marker_bytes = _TRUNC_MARKER.encode("utf-8")
    keep = max_bytes - len(marker_bytes)
    if keep < 0:
        keep = 0
    truncated = encoded[:keep].decode("utf-8", errors="ignore") + _TRUNC_MARKER
    # Final safety: if rounding pushed us over, strip marker only.
    if len(truncated.encode("utf-8")) > max_bytes:
        truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated, True


def _sanitize(text: str, max_bytes: int) -> tuple[str, bool, int]:
    redacted, count = _redact(text or "")
    final, truncated = _truncate(redacted, max_bytes)
    return final, truncated, count


class SessionJournal:
    """Append-only per-scope JSONL journal.

    Per-scope advisory `fcntl.flock` brackets append + prune to close the
    race where a concurrent prune's `os.replace` would otherwise overwrite
    a freshly appended entry. Readers are unlocked — they either see the
    pre-replace file or the post-replace file via the atomic rename.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root_override = root

    @property
    def root(self) -> Path:
        if self._root_override is not None:
            return self._root_override
        return bridge_home() / "journals"

    def _scope_hash(self, bot_id: str, chat_id: str,
                    thread_id: str | None) -> str:
        key = f"{bot_id}|{chat_id}|{thread_id or ''}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()

    def _path_for(self, bot_id: str, chat_id: str,
                  thread_id: str | None) -> Path:
        return self.root / f"{self._scope_hash(bot_id, chat_id, thread_id)}.jsonl"

    def _lock_path_for(self, path: Path) -> Path:
        return path.parent / (path.name + ".lock")

    @contextmanager
    def _scope_lock(self, path: Path):
        """Acquire an exclusive advisory flock on the scope sidecar lock file.

        Creates the lock file on first access. The lock is released when the
        context exits (either normally or via exception). We do not unlink
        the lock file — lock files are per-scope and tiny, and unlinking
        under contention would reintroduce a race.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._lock_path_for(path)
        # Use "a+" so the file is created on first use and we don't truncate.
        lock_fp = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
        finally:
            lock_fp.close()

    # ---- writers ----

    def append_user_turn(self, bot_id: str, chat_id: str,
                         thread_id: str | None, *, text: str,
                         runner_type: str,
                         provider: str | None = None,
                         model: str | None = None,
                         session_id: str | None = None) -> None:
        clean, truncated, redactions = _sanitize(text, USER_MAX_BYTES)
        self._append(bot_id, chat_id, thread_id, {
            "ts": time.time(),
            "kind": "user_turn",
            "runner_type": runner_type,
            "provider": provider,
            "model": model,
            "session_id": session_id,
            "text": clean,
            "truncated": truncated,
            "redactions": redactions,
        })

    def append_assistant_turn(self, bot_id: str, chat_id: str,
                              thread_id: str | None, *, text: str,
                              runner_type: str,
                              provider: str | None = None,
                              model: str | None = None,
                              session_id: str | None = None) -> None:
        clean, truncated, redactions = _sanitize(text, ASSISTANT_MAX_BYTES)
        self._append(bot_id, chat_id, thread_id, {
            "ts": time.time(),
            "kind": "assistant_turn",
            "runner_type": runner_type,
            "provider": provider,
            "model": model,
            "session_id": session_id,
            "text": clean,
            "truncated": truncated,
            "redactions": redactions,
        })

    def append_workflow_event(self, bot_id: str, chat_id: str,
                              thread_id: str | None, *, command: str,
                              decision: str, runner_type: str,
                              session_id: str | None = None) -> None:
        cmd_clean, cmd_trunc, cmd_red = _sanitize(
            command, WORKFLOW_COMMAND_MAX_BYTES,
        )
        dec_clean, dec_trunc, dec_red = _sanitize(
            decision, WORKFLOW_DECISION_MAX_BYTES,
        )
        self._append(bot_id, chat_id, thread_id, {
            "ts": time.time(),
            "kind": "workflow_event",
            "runner_type": runner_type,
            "session_id": session_id,
            "command": cmd_clean,
            "decision": dec_clean,
            "truncated": cmd_trunc or dec_trunc,
            "redactions": cmd_red + dec_red,
        })

    def append_artifact(self, bot_id: str, chat_id: str,
                        thread_id: str | None, *, path: str,
                        runner_type: str,
                        session_id: str | None = None) -> None:
        path_clean, path_trunc, path_red = _sanitize(
            path, ARTIFACT_PATH_MAX_BYTES,
        )
        self._append(bot_id, chat_id, thread_id, {
            "ts": time.time(),
            "kind": "artifact",
            "runner_type": runner_type,
            "session_id": session_id,
            "path": path_clean,
            "truncated": path_trunc,
            "redactions": path_red,
        })

    # ---- low-level append + prune (holds scope lock) ----

    def _append(self, bot_id: str, chat_id: str,
                thread_id: str | None, entry: dict) -> None:
        path = self._path_for(bot_id, chat_id, thread_id)
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        with self._scope_lock(path):
            with open(path, "a", encoding="utf-8") as fp:
                fp.write(line + "\n")
            self._maybe_prune_locked(path)

    def _maybe_prune_locked(self, path: Path) -> None:
        """Prune to MAX_ENTRIES tail. Caller must hold the scope lock."""
        try:
            with open(path, "r", encoding="utf-8") as fp:
                lines = fp.readlines()
        except OSError:
            return
        if len(lines) <= MAX_ENTRIES:
            return
        tail = lines[-MAX_ENTRIES:]
        tmp_fd: int | None = None
        tmp_name: str | None = None
        try:
            tmp_fd, tmp_name = tempfile.mkstemp(
                prefix=".journal-", suffix=".tmp", dir=str(path.parent),
            )
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as out:
                tmp_fd = None  # ownership transferred to the file object
                out.writelines(tail)
            os.replace(tmp_name, path)
            tmp_name = None  # replace consumed the temp file
        except OSError:
            # Close dangling fd if fdopen never took ownership, then unlink
            # the temp file if replace didn't consume it.
            if tmp_fd is not None:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    # ---- readers (unlocked) ----

    def read(self, bot_id: str, chat_id: str,
             thread_id: str | None) -> Iterator[dict]:
        path = self._path_for(bot_id, chat_id, thread_id)
        if not path.is_file():
            return
        with open(path, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def entry_count(self, bot_id: str, chat_id: str,
                    thread_id: str | None) -> int:
        path = self._path_for(bot_id, chat_id, thread_id)
        if not path.is_file():
            return 0
        count = 0
        with open(path, "r", encoding="utf-8") as fp:
            for line in fp:
                if line.strip():
                    count += 1
        return count

    def latest_timestamp(self, bot_id: str, chat_id: str,
                         thread_id: str | None) -> float | None:
        latest: float | None = None
        for entry in self.read(bot_id, chat_id, thread_id):
            ts = entry.get("ts")
            if isinstance(ts, (int, float)):
                if latest is None or ts > latest:
                    latest = ts
        return latest
