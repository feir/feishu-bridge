"""Thread → project binding persistence for Stage 2 memory-system-fix.

Maps `(bot_id, chat_id, thread_id)` to a project workspace, allowing the bridge
to route fresh-session memory injection, image/file downloads and runner cwd
to the user-selected repo instead of the default workspace.

File layout (one per bot, mirrors ``sessions-{bot_id}.json``):

    ~/.claude/state/feishu-bridge/thread-projects-{bot_id}.json

Schema (R1+R3):

    {
        "<bot_id>:<chat_id>:<thread_id>": {
            "project_id": "feishu-bridge",
            "workspace":  "/Users/feir/projects/feishu-bridge",
            "bound_at":   "2026-05-28T16:42:00+00:00",
            "source":     "explicit"
        },
        ...
    }

Keys MUST be produced via ``feishu_bridge.runtime.SessionMap.format_key`` so the
on-disk encoding stays in sync with the rest of the bridge.

Path normalization contract (R2):
    1. strip() leading/trailing whitespace
    2. strip surrounding Markdown backticks
    3. ``os.path.expanduser`` (so ``~/foo`` works)
    4. ``os.path.abspath``
    5. ``os.path.isdir`` — refuse to bind to a missing target

A failed normalization in :meth:`ThreadProjects.set` raises ``ValueError`` so
the caller can reply with a precise error to the user; nothing is written.

A corrupt JSON file is renamed to ``<path>.corrupt-<ts>.json`` and treated as
an empty table (fail-open + breadcrumb for recovery), matching ``RuntimeState``
philosophy elsewhere in the bridge.

All public methods are thread-safe via an ``RLock``; writes are atomic
(``tmp`` → ``fsync`` → ``os.replace``) with 0600 perms.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ── Path normalization ───────────────────────────────────────────────────────


_BACKTICK_RE = re.compile(r"^`+|`+$")


def normalize_path(raw: str) -> Optional[str]:
    """Normalize a user-supplied path; return None when input is empty.

    Does NOT enforce existence — callers that need that should follow up with
    ``os.path.isdir``. Kept separate so the same helper can normalize paths
    parsed from ``projects.md`` (which may legitimately reference repos not
    present on this machine yet).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = _BACKTICK_RE.sub("", s).strip()
    if not s:
        return None
    s = os.path.expanduser(s)
    s = os.path.abspath(s)
    return s


# ── projects.md parser (lookup table for /project <id>) ──────────────────────


@dataclass(frozen=True)
class ProjectEntry:
    """Single row from ``memory/projects.md``.

    ``path`` is the raw cell value (still wrapped in backticks, possibly with
    ``~``). Callers should run it through :func:`normalize_path` before use.
    """

    id: str
    name: str
    path: str


_SEPARATOR_CHARS = set("-: ")
_HEADER_IDS = {"id", "项目"}


def parse_projects_registry(content: str) -> list[ProjectEntry]:
    """Extract structured entries from a ``memory/projects.md`` table.

    Table shape: ``| ID | 名称 | 路径 | 状态 | ...``. Extra columns past 路径
    are tolerated and ignored. Header / separator / empty rows are skipped.
    """
    out: list[ProjectEntry] = []
    for raw in (content or "").splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        ident, name, path_cell = cells[0], cells[1], cells[2]
        if not ident or not path_cell:
            continue
        if set(ident) <= _SEPARATOR_CHARS:
            continue
        if ident.lower() in _HEADER_IDS:
            continue
        out.append(ProjectEntry(id=ident, name=name, path=path_cell))
    return out


# ── ThreadProjects store ─────────────────────────────────────────────────────


VALID_SOURCES = ("explicit",)  # R2: D5 — heuristic does NOT write the table


class ThreadProjects:
    """Persistent map of thread tag → bound project metadata.

    Tag format is the bridge-wide canonical ``bot_id:chat_id:thread_id`` string
    produced by :meth:`SessionMap.format_key`.
    """

    def __init__(self, path: Path):
        self._lock = threading.RLock()
        self._path = Path(path)
        self._data: dict[str, dict] = {}
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._load()

    # -- io ---------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            text = self._path.read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            self._quarantine_corrupt(exc)
            return
        if not isinstance(data, dict):
            self._quarantine_corrupt(
                TypeError(f"top-level JSON must be object, got {type(data).__name__}")
            )
            return
        # Keep only well-formed entries; quietly drop garbage rows so a single
        # bad write doesn't poison the whole file.
        cleaned: dict[str, dict] = {}
        for k, v in data.items():
            if not isinstance(k, str) or not isinstance(v, dict):
                continue
            if not v.get("workspace") or not v.get("project_id"):
                continue
            cleaned[k] = {
                "project_id": str(v.get("project_id")),
                "workspace":  str(v.get("workspace")),
                "bound_at":   str(v.get("bound_at") or ""),
                "source":     str(v.get("source") or "explicit"),
            }
        self._data = cleaned
        log.info("ThreadProjects loaded %d entries from %s", len(cleaned), self._path)

    def _quarantine_corrupt(self, exc: BaseException) -> None:
        ts = int(time.time())
        backup = self._path.with_suffix(f".corrupt-{ts}.json")
        try:
            os.replace(str(self._path), str(backup))
            log.warning(
                "ThreadProjects %s was corrupt (%s); quarantined to %s; using empty table",
                self._path, exc, backup,
            )
        except OSError as rename_exc:
            log.warning(
                "ThreadProjects %s was corrupt (%s) and quarantine rename failed (%s); "
                "using empty table",
                self._path, exc, rename_exc,
            )
        self._data = {}

    def _save_locked(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            try:
                os.unlink(str(tmp))
            except OSError:
                pass
            raise
        os.replace(str(tmp), str(self._path))

    # -- public api -------------------------------------------------------

    def get(self, tag: str) -> Optional[dict]:
        """Return a defensive copy of the binding, or ``None`` if unbound."""
        with self._lock:
            entry = self._data.get(tag)
            if entry is None:
                return None
            return dict(entry)

    def set(
        self,
        tag: str,
        *,
        project_id: str,
        workspace: str,
        source: str = "explicit",
    ) -> dict:
        """Persist a binding. Returns the stored entry (defensive copy).

        Raises:
            ValueError: if ``tag``/``project_id`` are empty, ``workspace`` is
                empty/un-normalizable, the normalized path is not a directory,
                or ``source`` is not in :data:`VALID_SOURCES`.
        """
        if not tag or not isinstance(tag, str):
            raise ValueError(f"tag must be a non-empty string, got {tag!r}")
        if not project_id or not isinstance(project_id, str):
            raise ValueError(f"project_id must be a non-empty string, got {project_id!r}")
        if source not in VALID_SOURCES:
            raise ValueError(
                f"source must be one of {VALID_SOURCES!r}, got {source!r}"
            )
        normalized = normalize_path(workspace)
        if not normalized:
            raise ValueError(f"workspace path is empty or invalid: {workspace!r}")
        if not os.path.isdir(normalized):
            raise ValueError(
                f"workspace path does not exist or is not a directory: {normalized}"
            )

        entry = {
            "project_id": project_id,
            "workspace":  normalized,
            "bound_at":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source":     source,
        }
        with self._lock:
            previous = self._data.get(tag)
            self._data[tag] = entry
            try:
                self._save_locked()
            except Exception:
                # Roll back the in-memory mutation so a save failure does not
                # leave the runtime view ahead of the persisted state.
                if previous is None:
                    self._data.pop(tag, None)
                else:
                    self._data[tag] = previous
                raise
        return dict(entry)

    def clear(self, tag: str) -> bool:
        """Remove a binding. Returns True iff something was removed."""
        with self._lock:
            previous = self._data.pop(tag, None)
            if previous is None:
                return False
            try:
                self._save_locked()
            except Exception:
                self._data[tag] = previous
                raise
            return True

    def all(self) -> dict[str, dict]:
        """Snapshot of all bindings (defensive copy). For diagnostics only."""
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}
