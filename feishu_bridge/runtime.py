"""Runtime primitives for Feishu Bridge."""

import contextlib
import json
import logging
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from importlib.resources import as_file, files
from pathlib import Path
from typing import ClassVar, Optional

log = logging.getLogger("feishu-bridge")

def pick_primary_model(model_usage: dict, configured: str | None) -> str | None:
    """从 modelUsage 中挑选主模型名。

    配置的模型（runner.model）若出现在 modelUsage 中则直接采用；
    否则回退到 token 用量最大的条目。避免 /new 后首个 turn 的 haiku
    (title gen / autocompact probe) 因 dict 插入顺序被误识别为主模型。
    """
    if not model_usage:
        return None
    if configured and configured in model_usage:
        return configured

    def _tok(mu: dict) -> int:
        return (mu.get("inputTokens", 0)
                + mu.get("outputTokens", 0)
                + mu.get("cacheReadInputTokens", 0)
                + mu.get("cacheCreationInputTokens", 0))

    return max(model_usage.items(), key=lambda kv: _tok(kv[1]))[0]


DEFAULT_TIMEOUT = 300  # 5 minutes
SILENT_TIMEOUT = 480   # 8 min — no assistant text output
BG_AGENT_SILENT_TIMEOUT = 3600  # 1 hour — background agents (e.g. Codex review)
DEDUP_TTL = 43200  # 12 hours
DEDUP_MAX = 5000
QUEUE_MAX = 50
MAX_PROMPT_CHARS = 50_000

# Static resources — materialized once at startup via ExitStack
_DATA = files("feishu_bridge.data")
_resource_stack = contextlib.ExitStack()
_BRIDGE_SETTINGS_PATH: Optional[str] = None


def materialize_data_files():
    """Extract data files and hold them for process lifetime. Idempotent."""
    global _BRIDGE_SETTINGS_PATH
    if _BRIDGE_SETTINGS_PATH is not None:
        return
    _BRIDGE_SETTINGS_PATH = str(
        _resource_stack.enter_context(as_file(_DATA.joinpath("bridge-settings.json")))
    )
    import shutil
    dst = Path.home() / ".feishu-bridge" / "bridge-settings.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_BRIDGE_SETTINGS_PATH, dst)


def get_bridge_settings_path() -> str:
    """Return materialized bridge-settings.json path, initializing on first call."""
    if _BRIDGE_SETTINGS_PATH is None:
        materialize_data_files()
    return _BRIDGE_SETTINGS_PATH

EMPTY_RESULT_MESSAGE = "Claude 本次未返回任何内容，请稍后重试。"

SESSION_HISTORY_HINT = (
    "若需历史细节请用 `session-history search <关键词>`（位于 `~/.claude/bin/`）。"
)

# Section names extracted from project MEMORY.md when project_workspace is set.
# Matching is case-insensitive and whitespace-tolerant; variant headings like
# "Pitfalls (project-specific)" still match "Known Pitfalls" via substring.
_MEMORY_SECTION_KEYS = (
    "Commands",
    "Constraints",
    "Known Pitfalls",
    "Pitfalls",
    "待办",
    "TODO",
    "Anchor",
)


def _read_text_safe(path: Path) -> Optional[str]:
    """Read a UTF-8 text file or return None on any failure."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError) as exc:
        log.warning("fresh-context: could not read %s: %s", path, exc)
        return None


def _parse_projects_md(content: str) -> str:
    """Extract a compact `<id> → <path>` index from `memory/projects.md`.

    The file format is a Markdown table with columns `ID | 名称 | 路径 | 状态`.
    We tolerate extra columns and skip rows whose path cell is empty or whose id
    looks like a separator (e.g. `---`).
    """
    lines = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        ident, _name, path_cell = cells[0], cells[1], cells[2]
        if not ident or not path_cell:
            continue
        if set(ident) <= set("-: "):
            continue
        if ident.lower() in {"id", "项目"}:
            continue
        path = path_cell.strip().strip("`").strip()
        if not path:
            continue
        lines.append(f"- {ident} → {path}")
    if not lines:
        return ""
    return "## Projects index\n" + "\n".join(lines)


def _parse_memory_sections(content: str, anchor_max_bytes: int) -> str:
    """Extract the relevant H2 sections from a project MEMORY.md.

    Headings are matched case-insensitively against `_MEMORY_SECTION_KEYS` via
    substring; this tolerates variants like `## Known Pitfalls (project-specific)`.
    Long sections (notably `Anchor`) are truncated to `anchor_max_bytes` bytes to
    keep the injected payload bounded.
    """
    out: list[str] = []
    current_heading: Optional[str] = None
    current_buf: list[str] = []

    def _flush() -> None:
        if current_heading is None:
            return
        body = "\n".join(current_buf).strip("\n")
        if not body:
            return
        encoded = body.encode("utf-8")
        if len(encoded) > anchor_max_bytes:
            body = encoded[:anchor_max_bytes].decode("utf-8", errors="ignore").rstrip()
            body += "\n…(truncated)"
        out.append(f"{current_heading}\n{body}")

    for raw in content.splitlines():
        if raw.startswith("## "):
            _flush()
            heading_text = raw[3:].strip()
            lower = heading_text.lower()
            if any(key.lower() in lower for key in _MEMORY_SECTION_KEYS):
                current_heading = f"## {heading_text}"
                current_buf = []
            else:
                current_heading = None
                current_buf = []
            continue
        if current_heading is not None:
            current_buf.append(raw)
    _flush()

    return "\n\n".join(out)


def build_fresh_context_prompt(
    workspace: str,
    *,
    project_workspace: Optional[str] = None,
    max_bytes: int = 2048,
    anchor_max_bytes: int = 1024,
) -> Optional[str]:
    """Compose a fresh-session memory prompt for `--append-system-prompt`.

    Sources merged in order (partial-OK; bad sources are skipped, not fatal):
      1. ``<workspace>/.claude/compact-context.md`` — global rolling context.
      2. ``<workspace>/.claude/memory/projects.md`` — distilled into a compact
         ``<id> → <path>`` index so the agent knows where each project lives.
      3. ``<project_workspace>/.claude/MEMORY.md`` — only when ``project_workspace``
         is provided (Stage 2 binding). Restricted to the H2 sections enumerated
         in :data:`_MEMORY_SECTION_KEYS`.

    A trailing :data:`SESSION_HISTORY_HINT` reminds the agent to consult
    ``session-history`` for older context.

    Returns ``None`` only when every source contributes zero usable content; any
    single source failure is logged at WARNING and skipped. Never raises.

    The combined payload is hard-capped at ``max_bytes`` bytes; long single
    sections (especially MEMORY ``Anchor``) are further bounded by
    ``anchor_max_bytes``.
    """
    if workspace is None or not str(workspace).strip():
        return None

    parts: list[str] = []
    ws_path = Path(str(workspace)).expanduser()

    # Source 1 — compact-context.md
    compact_path = ws_path / ".claude" / "compact-context.md"
    if compact_path.exists():
        text = _read_text_safe(compact_path)
        if text and text.strip():
            parts.append(f"## Compact context\n{text.strip()}")

    # Source 2 — projects.md index
    projects_path = ws_path / ".claude" / "memory" / "projects.md"
    if projects_path.exists():
        text = _read_text_safe(projects_path)
        if text:
            try:
                index = _parse_projects_md(text)
            except Exception as exc:  # defensive: never raise out
                log.warning("fresh-context: projects.md parse failed: %s", exc)
                index = ""
            if index:
                parts.append(index)

    # Source 3 — project-specific MEMORY (Stage 2 only)
    if project_workspace:
        proj_path = Path(str(project_workspace)).expanduser()
        memory_path = proj_path / ".claude" / "MEMORY.md"
        if memory_path.exists():
            text = _read_text_safe(memory_path)
            if text:
                try:
                    sections = _parse_memory_sections(text, anchor_max_bytes)
                except Exception as exc:  # defensive
                    log.warning("fresh-context: MEMORY.md parse failed: %s", exc)
                    sections = ""
                if sections:
                    parts.append(f"## Project MEMORY ({proj_path.name})\n{sections}")

    if not parts:
        return None

    body = "\n\n".join(parts)
    if len(body.encode("utf-8")) > max_bytes:
        # Truncate at byte boundary, then decode-tolerant rstrip to keep valid UTF-8
        body = body.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore").rstrip()
        body += "\n…(truncated)"
    return f"{body}\n\n---\n{SESSION_HISTORY_HINT}"

SILENT_OK_MESSAGE = "✓ 操作已完成（无文本输出）"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """Runner 统一返回结构。"""
    result: str = ""
    session_id: Optional[str] = None
    is_error: bool = False
    cancelled: bool = False
    usage: Optional[dict] = None
    last_call_usage: Optional[dict] = None
    model_usage: Optional[dict] = None
    total_cost_usd: Optional[float] = None
    peak_context_tokens: int = 0
    compact_detected: bool = False
    default_context_window: int = 0
    rate_limit_info: Optional[dict] = None

    def to_dict(self) -> dict:
        """向后兼容：转为 dict，保持 camelCase key。"""
        from dataclasses import asdict
        d = {k: v for k, v in asdict(self).items() if v is not None}
        if "model_usage" in d:
            d["modelUsage"] = d.pop("model_usage")
        return d


@dataclass
class StreamState:
    """流式解析过程中的可变状态。"""
    accumulated_text: str = ""
    session_id: Optional[str] = None
    final_result: Optional[dict] = None
    last_call_usage: Optional[dict] = None
    peak_context_tokens: int = 0
    compact_detected: bool = False
    rate_limit_info: Optional[dict] = None
    is_error: bool = False
    done: bool = False
    pending_output: list[str] = field(default_factory=list)
    pending_tool_status: list = field(default_factory=list)
    pending_todo_update: list[dict] | None = None
    pending_agent_launches: list[dict] | None = None
    bg_agent_running: bool = False
    # OMP todo state machine — rebuilt from ops deltas
    _todo_phases: list[dict] = field(default_factory=list)

    # ── Todo state machine (OMP ops format) ──

    def apply_todo_ops(self, ops: list) -> None:
        """Apply OMP todo_write ops to internal state."""
        for op_dict in ops:
            if not isinstance(op_dict, dict):
                continue
            op = op_dict.get("op")
            if op == "init":
                self._todo_phases = []
                for phase_def in (op_dict.get("list") or []):
                    if not isinstance(phase_def, dict):
                        continue
                    phase: dict = {"name": phase_def.get("phase", ""), "tasks": []}
                    for item in (phase_def.get("items") or []):
                        phase["tasks"].append({"content": str(item), "status": "pending"})
                    self._todo_phases.append(phase)
                self._auto_promote()
            elif op == "start":
                task = op_dict.get("task")
                if task:
                    # Demote any existing in_progress task before activating
                    self._demote_active()
                    self._set_task_status(task, "in_progress")
            elif op == "done":
                task = op_dict.get("task")
                phase_name = op_dict.get("phase")
                if task:
                    self._set_task_status(task, "completed")
                    self._auto_promote()
                elif phase_name:
                    self._set_phase_status(phase_name, "completed")
                    self._auto_promote()
            elif op == "drop":
                task = op_dict.get("task")
                phase_name = op_dict.get("phase")
                if task:
                    self._set_task_status(task, "dropped")
                    self._auto_promote()
                elif phase_name:
                    self._set_phase_status(phase_name, "dropped")
                    self._auto_promote()
            elif op == "rm":
                task = op_dict.get("task")
                phase_name = op_dict.get("phase")
                if not task and not phase_name:
                    self._todo_phases = []
                elif task:
                    for p in self._todo_phases:
                        p["tasks"] = [t for t in p["tasks"] if t["content"] != task]
                    self._auto_promote()
                elif phase_name:
                    self._todo_phases = [p for p in self._todo_phases if p["name"] != phase_name]
                    self._auto_promote()
            elif op == "append":
                phase_name = op_dict.get("phase", "")
                items = op_dict.get("items") or []
                phase = next((p for p in self._todo_phases if p["name"] == phase_name), None)
                if phase is None:
                    phase = {"name": phase_name, "tasks": []}
                    self._todo_phases.append(phase)
                for item in items:
                    phase["tasks"].append({"content": str(item), "status": "pending"})
                self._auto_promote()
            # "note" — no state change

    def get_todo_list(self) -> list[dict]:
        """Flatten phases into [{content, status}] for UI rendering."""
        result: list[dict] = []
        for phase in self._todo_phases:
            for task in phase["tasks"]:
                result.append({"content": task["content"], "status": task["status"]})
        return result

    def _auto_promote(self) -> None:
        """Promote first pending task to in_progress if none is active."""
        for phase in self._todo_phases:
            for task in phase["tasks"]:
                if task["status"] == "in_progress":
                    return  # already have an active task
        for phase in self._todo_phases:
            for task in phase["tasks"]:
                if task["status"] == "pending":
                    task["status"] = "in_progress"
                    return

    def _set_task_status(self, content: str, status: str,
                         phase_name: str | None = None) -> None:
        phases = self._todo_phases
        if phase_name:
            phases = [p for p in phases if p["name"] == phase_name]
        for phase in phases:
            for task in phase["tasks"]:
                if task["content"] == content:
                    task["status"] = status
                    return
        # Fallback: if phase_name was given but no match, try globally
        if phase_name:
            self._set_task_status(content, status)

    def _set_phase_status(self, name: str, status: str) -> None:
        """Set all non-terminal tasks in a phase to the given status."""
        for phase in self._todo_phases:
            if phase["name"] == name:
                for task in phase["tasks"]:
                    if task["status"] not in ("completed", "dropped"):
                        task["status"] = status

    def _demote_active(self) -> None:
        """Demote any in_progress task back to pending."""
        for phase in self._todo_phases:
            for task in phase["tasks"]:
                if task["status"] == "in_progress":
                    task["status"] = "pending"
                    return


def _extract_hint_data(tool_name: str, tool_input: dict) -> str:
    """Extract minimal hint string from tool input (avoids holding large dicts).

    Supports both Claude Code (Alma) param names (file_path, command) and
    OMP param names (path, command, _i).  Falls back to OMP's ``_i`` intent
    field when no tool-specific key is found.
    """
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if not cmd:
            return (tool_input.get("_i") or "")[:50]
        # Prefer safe descriptors over raw command (may contain secrets)
        desc = tool_input.get("description") or tool_input.get("_i") or ""
        if desc:
            return desc[:50]
        # Fallback: executable basename only — never expose full args
        first = cmd.split(maxsplit=1)[0]
        return os.path.basename(first)
    if tool_name in ("Read", "Write", "Edit"):
        # Alma uses "file_path", OMP uses "path"
        return tool_input.get("file_path") or tool_input.get("path") or ""
    if tool_name in ("Agent", "Task"):
        # OMP format: tasks[] array with per-task descriptions
        tasks = tool_input.get("tasks")
        if isinstance(tasks, list) and tasks:
            descs = [t.get("description", "") for t in tasks if isinstance(t, dict)]
            joined = ", ".join(d for d in descs if d)
            if joined:
                return joined[:40]
        # Claude Code / Alma format: top-level description
        return (tool_input.get("description") or tool_input.get("_i") or "")[:40]
    if tool_name == "Skill":
        return tool_input.get("skill", "")
    if tool_name in ("Grep", "Search"):
        return (tool_input.get("pattern") or "")[:30]
    if tool_name == "WebSearch":
        return (tool_input.get("query") or "")[:40]
    if tool_name == "WebFetch":
        return (tool_input.get("url") or "")[:60]
    if tool_name == "Find":
        paths = tool_input.get("paths")
        if isinstance(paths, list) and paths:
            first = str(paths[0])[:40]
            return f"{first} +{len(paths)-1}" if len(paths) > 1 else first
        return (tool_input.get("_i") or "")[:50]
    if tool_name == "Lsp":
        action = tool_input.get("action", "")
        target = tool_input.get("file") or tool_input.get("symbol") or ""
        if action and target:
            return f"{action} {target}"[:50]
        return action or (tool_input.get("_i") or "")[:50]
    if tool_name == "Browser":
        action = tool_input.get("action", "")
        url = tool_input.get("url", "")
        if url:
            return f"{action} {url}"[:50]
        return action or (tool_input.get("_i") or "")[:50]
    if tool_name == "Eval":
        cells = tool_input.get("cells")
        if isinstance(cells, list) and cells:
            title = cells[0].get("title", "")
            if title:
                return title[:40]
            return cells[0].get("language", "")
        return (tool_input.get("_i") or "")[:50]
    if tool_name in ("AstGrep", "AstEdit"):
        pat = tool_input.get("pat", "")
        if pat:
            return pat[:30]
        ops = tool_input.get("ops")
        if isinstance(ops, list) and ops:
            return (ops[0].get("pat") or "")[:30]
        return (tool_input.get("_i") or "")[:50]
    if tool_name == "Debug":
        action = tool_input.get("action", "")
        prog = tool_input.get("program", "")
        if action and prog:
            return f"{action} {prog}"[:50]
        return action or (tool_input.get("_i") or "")[:50]
    # Universal fallback: OMP tools carry _i (intent) field
    return (tool_input.get("_i") or "")[:50]


# ---------------------------------------------------------------------------
# Dedup / Session / Queue (unchanged)
# ---------------------------------------------------------------------------

class MessageDedup:
    """LRU message dedup with TTL."""

    def __init__(self, ttl: int = DEDUP_TTL, max_entries: int = DEDUP_MAX):
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._ttl = ttl
        self._max = max_entries
        self._lock = threading.Lock()

    def is_duplicate(self, message_id: str) -> bool:
        now = time.time()
        with self._lock:
            expired = []
            for mid, ts in self._seen.items():
                if now - ts > self._ttl:
                    expired.append(mid)
                else:
                    break
            for mid in expired:
                del self._seen[mid]

            while len(self._seen) >= self._max:
                self._seen.popitem(last=False)

            if message_id in self._seen:
                return True
            self._seen[message_id] = now
            return False


class SessionMap:
    """Thread-safe session mapping with atomic JSON persistence."""

    _AGENT_TYPE_KEY = "_agent_type"

    def __init__(self, path: Path, agent_type: str | None = None):
        self._lock = threading.RLock()
        self._path = path
        self._data: dict[str, str] = {}
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._load()
        if agent_type:
            self._reconcile_agent_type(agent_type)

    def _reconcile_agent_type(self, agent_type: str):
        """Clear stale sessions when agent type changes."""
        stored = self._data.get(self._AGENT_TYPE_KEY)
        if stored == agent_type:
            return  # match — nothing to do

        session_count = sum(1 for k in self._data if k != self._AGENT_TYPE_KEY)
        if stored is None and agent_type == "claude" and session_count > 0:
            # Legacy file without metadata + still using claude → preserve sessions
            log.info("Adding agent_type=claude to existing sessions file")
        elif stored is not None and stored != agent_type and session_count > 0:
            log.warning(
                "Agent type changed %s → %s; clearing %d stale sessions",
                stored, agent_type, session_count,
            )
            self._data = {}
        elif stored is None and agent_type != "claude" and session_count > 0:
            log.warning(
                "Agent type set to %s but existing sessions have no type marker; "
                "clearing %d sessions", agent_type, session_count,
            )
            self._data = {}

        self._data[self._AGENT_TYPE_KEY] = agent_type
        self._save()

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
                count = sum(1 for k in self._data if k != self._AGENT_TYPE_KEY)
                log.info("Loaded %d sessions from %s", count, self._path)
            except (json.JSONDecodeError, IOError) as e:
                log.warning("Failed to load sessions: %s", e)
                self._data = {}

    def _save(self):
        """Best-effort atomic: write tmp (0600) -> fsync -> os.replace."""
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            try:
                os.unlink(str(tmp))
            except OSError:
                pass
            raise
        os.replace(str(tmp), str(self._path))

    @staticmethod
    def format_key(key: tuple) -> str:
        """Public key encoding for cross-module consumers (e.g. bg_supervisor).

        Single source of truth for "bot:chat:thread" string form so consumers
        don't have to re-implement (and silently drift from) the format.
        """
        return ":".join(str(k or "") for k in key)

    def get(self, key: tuple) -> Optional[str]:
        with self._lock:
            return self._data.get(self.format_key(key))

    def put(self, key: tuple, session_id: str):
        with self._lock:
            ks = self.format_key(key)
            old = self._data.get(ks)
            self._data[ks] = session_id
            try:
                self._save()
            except Exception:
                if old is None:
                    self._data.pop(ks, None)
                else:
                    self._data[ks] = old
                raise

    def delete(self, key: tuple):
        with self._lock:
            ks = self.format_key(key)
            old = self._data.pop(ks, None)
            try:
                self._save()
            except Exception:
                if old is not None:
                    self._data[ks] = old
                raise


class SessionQueueFull(Exception):
    """Raised when a session's pending queue exceeds MAX_PENDING."""


class ChatTaskQueue:
    """Per-session FIFO task queue. Only one task per session in flight."""

    MAX_PENDING_PER_SESSION = 10

    def __init__(self, work_queue: queue.Queue):
        self._work_queue = work_queue
        self._active: set[str] = set()
        self._pending: dict[str, deque] = {}
        self._lock = threading.Lock()

    def enqueue(
        self, key: str, item: dict, *, bypass_backpressure: bool = False,
    ) -> str:
        """Enqueue `item` for session `key`.

        bypass_backpressure: skip the MAX_PENDING_PER_SESSION check. Used
        for synthetic bg-task completion turns where dropping the delivery
        means the user never sees the result. Human messages keep the
        backpressure so a session can't flood the worker.
        """
        with self._lock:
            if key in self._active:
                pending = self._pending.get(key)
                if (
                    not bypass_backpressure
                    and pending
                    and len(pending) >= self.MAX_PENDING_PER_SESSION
                ):
                    raise SessionQueueFull(
                        f"Session {key} has {self.MAX_PENDING_PER_SESSION} pending"
                    )
                self._pending.setdefault(key, deque()).append(item)
                return "queued"

            self._active.add(key)
            self._work_queue.put_nowait(item)
            return "immediate"

    def on_complete(self, key: str) -> None:
        with self._lock:
            pending = self._pending.get(key)
            if pending:
                next_item = pending.popleft()
                if not pending:
                    del self._pending[key]
                try:
                    self._work_queue.put_nowait(next_item)
                except queue.Full:
                    self._pending.setdefault(key, deque()).appendleft(next_item)
                    log.warning("work_queue full in on_complete, retry in 2s (key=%s)", key)
                    threading.Timer(2.0, self.on_complete, args=(key,)).start()
            else:
                self._active.discard(key)
                self._pending.pop(key, None)

    def drain(self, key: str) -> list:
        with self._lock:
            return list(self._pending.pop(key, deque()))

    def pending_count(self, key: str) -> int:
        with self._lock:
            return len(self._pending.get(key, []))


# ---------------------------------------------------------------------------
# BaseRunner ABC
# ---------------------------------------------------------------------------

class BaseRunner(ABC):
    """Abstract base for AI Agent CLI runners."""

    ALWAYS_STREAMING: ClassVar[bool] = False

    _SAFETY_PROMPT = (
        "CRITICAL: You are running as a subprocess of feishu-bridge. "
        "NEVER execute systemctl restart/stop/reload on feishu-bridge - "
        "doing so kills your own parent process, causing an infinite restart loop.\n\n"
        "Do not output 'Status:' lines at the end of responses — "
        "status is tracked externally by the bridge."
    )
    _MINIMAL_SAFETY_PROMPT = (
        "CRITICAL: You are running as a subprocess of feishu-bridge. "
        "NEVER restart, stop, or reload feishu-bridge itself."
    )

    def __init__(self, command: Optional[str], model: Optional[str], workspace: str, timeout: int,
                 max_budget_usd: Optional[float] = None,
                 extra_system_prompts: Optional[list[str]] = None,
                 extra_cli_args: Optional[list[str]] = None,
                 fixed_env: Optional[dict[str, str]] = None,
                 safety_prompt_mode: str = "full",
                 setting_sources: Optional[str] = None):
        self.command = command
        self.model = model
        self.workspace = workspace
        self.timeout = timeout
        self.max_budget_usd = max_budget_usd
        self._extra_system_prompts = extra_system_prompts or []
        self._extra_cli_args = [str(arg) for arg in (extra_cli_args or [])]
        self._fixed_env = {
            str(key): str(value) for key, value in (fixed_env or {}).items()
        }
        mode = str(safety_prompt_mode or "full").strip().lower()
        self._safety_prompt_mode = mode if mode in {"full", "minimal", "off"} else "full"
        self._setting_sources = setting_sources
        self._active: dict[str, subprocess.Popen] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    # ── Abstract methods (subclass must implement) ──

    @abstractmethod
    def build_args(self, prompt: str, session_id: Optional[str],
                   resume: bool, streaming: bool, *,
                   fork_session: bool = False,
                   fresh_context: Optional[str] = None) -> list:
        """构建 CLI 命令行参数列表。

        ``fresh_context`` 用于把 :func:`build_fresh_context_prompt` 的产物追加到
        子进程的 ``--append-system-prompt``；仅在 ``resume=False`` 时由 caller 传入。
        """

    @abstractmethod
    def parse_streaming_line(self, event: dict, state: StreamState) -> None:
        """解析单行流式 JSONL 事件，更新 StreamState。"""

    @abstractmethod
    def parse_blocking_output(self, stdout: str, session_id: Optional[str]) -> dict:
        """解析阻塞模式的完整 stdout，返回 result dict。"""

    # ── Optional overrides ──

    def get_session_not_found_signatures(self) -> list[str]:
        """返回表示 session 不存在的错误签名列表。默认空。"""
        return []

    def get_extra_env(self) -> dict:
        """额外环境变量。默认注入用户 PATH。"""
        env = dict(self._fixed_env)
        # Ensure user-local bin dirs are in PATH for subprocesses.
        # systemd services don't source ~/.profile, so ~/.local/bin etc.
        # are missing from PATH, causing tools like gh to be not found.
        user_bins = [
            os.path.expanduser("~/.local/bin"),
            os.path.expanduser("~/bin"),
        ]
        existing = env.get("PATH") or os.environ.get("PATH", "")
        additions = [p for p in user_bins if os.path.isdir(p) and p not in existing]
        if additions:
            env["PATH"] = ":".join(additions) + ":" + existing
        return env

    def get_display_name(self) -> str:
        """用户可见的 Agent 名称。"""
        return "AI Agent"

    def supports_compact(self) -> bool:
        """是否支持 /compact 命令。"""
        return True

    def supports_auto_compact(self) -> bool:
        """Whether IdleCompactManager should track this runner's sessions."""
        return self.supports_compact()

    def has_session(self, session_id: str) -> bool:
        """Whether this runner holds state for the given session_id.

        Default True — CLI runners persist via side-files, so we assume
        the state exists unless a subclass explicitly tracks it in memory.
        """
        return True

    def wants_auth_file(self) -> bool:
        """Whether the worker should create /tmp/feishu_auth_*.json for this runner.

        Default True — CLI runners need the file for feishu-cli OAuth.
        """
        return True

    def _build_system_prompt(self, extra: Optional[str] = None) -> str:
        """Merge safety guard + extra system prompts into one string.

        ``extra`` is an optional per-run addendum (e.g. fresh-session memory
        produced by :func:`build_fresh_context_prompt`); it is appended after
        the safety guard and any instance-level ``extra_system_prompts``.
        Passing ``extra`` does not mutate instance state — callers may invoke
        this concurrently with different ``extra`` values without interference.
        """
        parts: list[str] = []
        if self._safety_prompt_mode == "full":
            parts.append(self._SAFETY_PROMPT)
        elif self._safety_prompt_mode == "minimal":
            parts.append(self._MINIMAL_SAFETY_PROMPT)
        parts.extend(self._extra_system_prompts)
        if extra:
            parts.append(extra)
        return "\n\n".join(parts)

    def _build_streaming_result(self, state: StreamState,
                                session_id: Optional[str]) -> Optional[dict]:
        """Build content-level result from streaming state.

        Return dict if a definitive result is available, None to fall through
        to generic BaseRunner fallbacks (timeout, exit code, empty text).
        Subclasses override this for protocol-specific result handling.
        """
        return None

    # ── Shared subprocess management ──

    @staticmethod
    def _force_kill(proc: subprocess.Popen):
        """Send SIGKILL to process tree (last resort)."""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    @staticmethod
    def _kill_proc_tree(proc: subprocess.Popen, graceful_timeout: float = 15):
        """Non-blocking graceful kill: SIGTERM now, SIGKILL after grace period.

        Does NOT call proc.wait() — the caller's main thread handles reaping.
        This avoids concurrent proc.wait() races when called from a Timer thread.
        """
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError):
            return
        # Phase 1: send SIGTERM (non-blocking)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        # Phase 2: schedule SIGKILL after grace period (non-blocking).
        def _deferred_sigkill():
            if proc.poll() is None:  # Still alive
                log.warning("Process %d did not exit after SIGTERM (%ds), sending SIGKILL",
                            proc.pid, graceful_timeout)
                BaseRunner._force_kill(proc)
        threading.Timer(graceful_timeout, _deferred_sigkill).start()

    def cancel(self, tag: str) -> bool:
        with self._lock:
            proc = self._active.get(tag)
            if proc:
                self._cancelled.add(tag)
        if proc:
            log.info("Cancelling %s process: tag=%s pid=%d",
                     self.get_display_name(), tag, proc.pid)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            return True
        return False

    def _cleanup_tag(self, tag: Optional[str]) -> bool:
        if not tag:
            return False
        with self._lock:
            self._active.pop(tag, None)
            was_cancelled = tag in self._cancelled
            self._cancelled.discard(tag)
        return was_cancelled

    def run(self, prompt: str, session_id: Optional[str] = None,
            resume: bool = False, tag: Optional[str] = None,
            on_output=None, on_tool_status=None, on_todo_update=None,
            on_agent_update=None, env_extra: Optional[dict] = None,
            fork_session: bool = False,
            fresh_context: Optional[str] = None,
            workspace_override: Optional[str] = None) -> dict:

        if len(prompt) > MAX_PROMPT_CHARS:
            log.warning("Prompt truncated: %d -> %d chars", len(prompt), MAX_PROMPT_CHARS)
            prompt = prompt[:MAX_PROMPT_CHARS] + "\n\n...(message truncated)"

        streaming = bool(on_output) or self.ALWAYS_STREAMING
        args = self.build_args(prompt, session_id, resume, streaming,
                               fork_session=fork_session,
                               fresh_context=fresh_context)

        # Mirror what build_args composed so the log reports the actual size
        # the CLI saw (including fresh_context). _build_system_prompt is a pure
        # function of (instance prompts + extra), so this is cheap and safe.
        _sp = self._build_system_prompt(extra=fresh_context)
        cwd = workspace_override or self.workspace
        log.info("%s: resume=%s sid=%s stream=%s prompt=%d chars sys_prompt=%d chars (~%d tokens) cwd=%s%s",
                 self.get_display_name(), resume,
                 session_id[:8] if session_id else "-",
                 streaming, len(prompt), len(_sp), len(_sp) // 4,
                 cwd, " (override)" if workspace_override else "")

        env = None
        extra_env = self.get_extra_env()
        if env_extra:
            extra_env.update(env_extra)
        if extra_env:
            env = os.environ.copy()
            env.update(extra_env)

        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )

        if tag:
            with self._lock:
                self._active[tag] = proc

        if streaming:
            result = self._run_streaming(proc, session_id, tag, on_output,
                                         on_tool_status=on_tool_status,
                                         on_todo_update=on_todo_update,
                                         on_agent_update=on_agent_update)
        else:
            result = self._run_blocking(proc, session_id, tag)

        return result

    def _run_blocking(self, proc, session_id, tag) -> dict:
        t0 = time.monotonic()
        try:
            stdout, stderr = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t0
            log.error(
                "%s blocking timeout: sid=%s elapsed=%.0fs limit=%ds",
                self.get_display_name(),
                (session_id or "-")[:8], elapsed, self.timeout,
            )
            # Graceful kill: SIGTERM + deferred SIGKILL. proc.communicate()
            # blocks until exit — will unblock after SIGTERM or deferred SIGKILL.
            self._kill_proc_tree(proc)
            proc.communicate()
            return {
                "result": f"{self.get_display_name()} 超时（已运行 {int(elapsed)}s，限制 {self.timeout}s）",
                "session_id": session_id,
                "is_error": True,
            }
        finally:
            was_cancelled = self._cleanup_tag(tag)

        if tag and was_cancelled:
            return {
                "result": "任务已取消。",
                "session_id": session_id,
                "is_error": False,
                "cancelled": True,
            }

        if proc.returncode != 0 and not stdout.strip():
            return {
                "result": f"{self.get_display_name()} 退出码 {proc.returncode}: {stderr[:500]}",
                "session_id": session_id,
                "is_error": True,
            }

        return self.parse_blocking_output(stdout, session_id)

    def _run_streaming(self, proc, session_id, tag, on_output,
                        on_tool_status=None, on_todo_update=None,
                        on_agent_update=None) -> dict:
        state = StreamState(session_id=session_id)
        timed_out = False
        silent_timed_out = False
        result_received = threading.Event()
        stderr_lines = []
        t0 = time.monotonic()

        def _drain_stderr():
            for line in proc.stderr:
                stderr_lines.append(line)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        def _timeout_kill():
            nonlocal timed_out
            if result_received.is_set() or silent_timed_out:
                return
            timed_out = True
            BaseRunner._kill_proc_tree(proc)

        # Idle timeout: resets on every stdout line from the CLI.
        timer = threading.Timer(self.timeout, _timeout_kill)
        timer.start()

        def _reset_idle_timer():
            nonlocal timer
            timer.cancel()
            timer = threading.Timer(self.timeout, _timeout_kill)
            timer.start()

        # Silent timeout: resets only on assistant text output.
        silent_limit = getattr(self, 'silent_timeout', SILENT_TIMEOUT)

        def _silent_timeout_kill():
            nonlocal silent_timed_out
            if result_received.is_set() or timed_out:
                return
            log.warning(
                "%s silent timeout: sid=%s no assistant text for %ds",
                self.get_display_name(),
                (session_id or "-")[:8], silent_limit,
            )
            silent_timed_out = True
            BaseRunner._kill_proc_tree(proc)

        silent_timer = threading.Timer(silent_limit, _silent_timeout_kill)
        silent_timer.start()

        def _reset_silent_timer():
            nonlocal silent_timer
            silent_timer.cancel()
            silent_timer = threading.Timer(silent_limit, _silent_timeout_kill)
            silent_timer.start()

        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                _reset_idle_timer()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                self.parse_streaming_line(event, state)

                if state.bg_agent_running and silent_limit < BG_AGENT_SILENT_TIMEOUT:
                    silent_limit = BG_AGENT_SILENT_TIMEOUT
                    _reset_silent_timer()

                # Drain pending_output → on_output callback
                if on_output and state.pending_output:
                    for text in state.pending_output:
                        on_output(text)
                    state.pending_output.clear()
                    _reset_silent_timer()

                # Drain pending_tool_status → on_tool_status callback
                if state.pending_tool_status:
                    if on_tool_status:
                        on_tool_status(list(state.pending_tool_status))
                        _reset_silent_timer()
                    state.pending_tool_status.clear()

                # Drain pending_todo_update → on_todo_update callback
                if on_todo_update and state.pending_todo_update is not None:
                    on_todo_update(state.pending_todo_update)
                    state.pending_todo_update = None

                # Drain pending_agent_launches → on_agent_update callback
                if on_agent_update and state.pending_agent_launches is not None:
                    on_agent_update(state.pending_agent_launches)
                    state.pending_agent_launches = None

                if state.done:
                    result_received.set()
                    break

            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t0
            if silent_timed_out:
                log.error(
                    "%s silent timeout + proc.wait hang: sid=%s elapsed=%.0fs",
                    self.get_display_name(),
                    (state.session_id or session_id or "-")[:8], elapsed,
                )
                self._force_kill(proc)
                proc.wait()
                # Fall through to the silent_timed_out handler after finally.
            elif timed_out:
                # The idle timer fired, killed the process, stdout EOF'd,
                # and now proc.wait(30) also timed out — unusual but possible.
                log.error(
                    "%s idle timeout + proc.wait hang: sid=%s elapsed=%.0fs idle_limit=%ds",
                    self.get_display_name(),
                    (state.session_id or session_id or "-")[:8], elapsed, self.timeout,
                )
                self._force_kill(proc)
                proc.wait()
            elif state.done or state.final_result:
                # Agent sent a result event (task completed or deferred) but
                # the process didn't exit within 30s. Treat as success.
                log.warning(
                    "%s process hung after result event: sid=%s elapsed=%.0fs, force-killing",
                    self.get_display_name(),
                    (state.session_id or session_id or "-")[:8], elapsed,
                )
                self._force_kill(proc)
                proc.wait()
                # Fall through to the content result handler below.
            else:
                # stdout closed without a result event and process won't exit.
                # Likely a crash or abnormal termination.
                log.error(
                    "%s process hung (no result): sid=%s elapsed=%.0fs accumulated=%d chars, force-killing",
                    self.get_display_name(),
                    (state.session_id or session_id or "-")[:8], elapsed, len(state.accumulated_text),
                )
                self._force_kill(proc)
                proc.wait()
                return {
                    "result": (state.accumulated_text + "\n\n⚠️ %s 进程未正常退出（已运行 %ds）" % (self.get_display_name(), int(elapsed)))
                             if state.accumulated_text else
                             "%s 进程未正常退出（已运行 %ds，无输出）" % (self.get_display_name(), int(elapsed)),
                    "session_id": state.session_id or session_id,
                    "is_error": True,
                }
        except Exception:
            self._force_kill(proc)
            proc.wait()
            raise
        finally:
            timer.cancel()
            silent_timer.cancel()
            stderr_thread.join(timeout=5)
            was_cancelled = self._cleanup_tag(tag)

        if tag and was_cancelled:
            return {
                "result": "任务已取消。",
                "session_id": state.session_id or session_id,
                "is_error": False,
                "cancelled": True,
            }

        if silent_timed_out:
            elapsed = time.monotonic() - t0
            log.error(
                "%s silent timeout: sid=%s elapsed=%.0fs silent_limit=%ds",
                self.get_display_name(),
                (state.session_id or session_id or "-")[:8], elapsed, silent_limit,
            )
            warning = "\n\n⚠️ 长时间无文本输出（>%ds），自动中断恢复中…" % silent_limit
            return {
                "result": (state.accumulated_text + warning) if state.accumulated_text else warning.strip(),
                "session_id": state.session_id or session_id,
                "is_error": False,
                "silent_timeout": True,
                "peak_context_tokens": state.peak_context_tokens,
                "compact_detected": state.compact_detected,
            }

        # Content-level result from subclass (checked BEFORE timed_out
        # to handle race where idle timer fires during proc.wait after
        # result was already received).
        content_result = self._build_streaming_result(state, session_id)
        if content_result is not None:
            return content_result

        if timed_out:
            elapsed = time.monotonic() - t0
            log.error(
                "%s idle timeout: sid=%s elapsed=%.0fs idle_limit=%ds",
                self.get_display_name(),
                (state.session_id or session_id or "-")[:8], elapsed, self.timeout,
            )
            return {
                "result": f"{self.get_display_name()} 空闲超时（连续无输出超过 {self.timeout}s，已运行 {int(elapsed)}s）",
                "session_id": state.session_id or session_id,
                "is_error": True,
            }

        stderr = "".join(stderr_lines)
        if proc.returncode != 0:
            return {
                "result": f"{self.get_display_name()} 退出码 {proc.returncode}: {stderr[:500]}",
                "session_id": state.session_id or session_id,
                "is_error": True,
            }

        if not state.accumulated_text:
            log.warning(
                "%s streaming completed without text or result event: sid=%s stderr_len=%d",
                self.get_display_name(),
                (state.session_id or session_id or "-")[:8],
                len(stderr),
            )
            return {
                "result": f"{self.get_display_name()} 本次未返回任何内容，请稍后重试。",
                "session_id": state.session_id or session_id,
                "is_error": True,
            }

        return {
            "result": state.accumulated_text,
            "session_id": state.session_id or session_id,
            "is_error": state.is_error,
            "peak_context_tokens": state.peak_context_tokens,
            "compact_detected": state.compact_detected,
            "rate_limit_info": state.rate_limit_info,
        }


# ---------------------------------------------------------------------------
# ClaudeRunner
# ---------------------------------------------------------------------------

class ClaudeRunner(BaseRunner):
    """Claude Code CLI runner."""

    SESSION_NOT_FOUND_SIGNATURES = [
        "session not found",
        "Session not found",
        "no such session",
        "session does not exist",
        "sessionId that does not exist",
        "Could not find session",
        "ENOENT",
        "no such file or directory",
    ]

    def build_args(self, prompt, session_id, resume, streaming, *,
                   fork_session=False,
                   fresh_context: Optional[str] = None):
        args = [
            self.command, "-p",
        ]

        if self._extra_cli_args:
            args.extend(self._extra_cli_args)

        args.extend([
            "--settings", get_bridge_settings_path(),
        ])
        if self.model:
            args.extend(["--model", self.model])
        if self._setting_sources is not None:
            args.extend(["--setting-sources", self._setting_sources])
        system_prompt = self._build_system_prompt(extra=fresh_context)
        if system_prompt:
            args.extend(["--append-system-prompt", system_prompt])

        if self.max_budget_usd is not None:
            args.extend(["--max-budget-usd", str(self.max_budget_usd)])

        if streaming:
            args.extend(["--output-format", "stream-json",
                         "--verbose", "--include-partial-messages"])
        else:
            args.extend(["--output-format", "json"])

        if resume and session_id:
            args.extend(["--resume", session_id])
            if fork_session:
                args.extend(["--fork-session", "--disallowed-tools", "*"])
        elif session_id:
            args.extend(["--session-id", session_id])

        args.append("--")
        args.append(prompt)
        return args

    def parse_streaming_line(self, event, state):
        etype = event.get("type", "")
        if etype == "result":
            state.final_result = event
            # Don't break on tool_deferred results — Claude CLI may emit
            # a second result event with the actual response text after
            # internally restarting the deferred tool.
            if event.get("stop_reason") != "tool_deferred":
                state.done = True
        elif etype == "assistant":
            msg = event.get("message", {})
            msg_usage = msg.get("usage")
            if msg_usage:
                state.last_call_usage = msg_usage
                # Peak excludes cache_creation_input_tokens: those are tokens
                # newly written to cache this turn, a transient cost that
                # inflates peak on the first cache-warming turn and makes
                # subsequent cache_read-only turns look like a drop.  Peak
                # tracks the actual context the model has been carrying
                # (new input + prior cache read).
                ctx_tokens = ((msg_usage.get("input_tokens", 0) or 0)
                              + (msg_usage.get("cache_read_input_tokens", 0) or 0))
                # Detect auto-compact via large token drop from peak.
                # Tightened from the earlier 30% heuristic: drop must be
                # >50%, and prior peak must be substantial (>=50K tokens)
                # to avoid false positives on small sessions where an
                # incremental tool-output trim can look proportionally
                # large.  Full session compacts always shrink context by
                # much more than 50%.
                if (not state.compact_detected
                        and state.peak_context_tokens >= 50_000
                        and ctx_tokens < state.peak_context_tokens * 0.5):
                    state.compact_detected = True
                    log.info(
                        "Auto-compact detected via token drop: %d → %d",
                        state.peak_context_tokens, ctx_tokens)
                if ctx_tokens > state.peak_context_tokens:
                    state.peak_context_tokens = ctx_tokens
            # Extract tool-use metadata for progress feedback.
            for block in msg.get("content", []):
                if block.get("type") == "tool_use":
                    tool_name = block.get("name", "")
                    if tool_name:
                        state.pending_tool_status.append({
                            "name": tool_name,
                            "hint_data": _extract_hint_data(
                                tool_name, block.get("input") or {}),
                        })
                    if tool_name == "TodoWrite":
                        todos = block.get("input", {}).get("todos")
                        if isinstance(todos, list):
                            state.pending_todo_update = todos
                    elif tool_name == "Agent":
                        ai = block.get("input", {})
                        if ai.get("run_in_background"):
                            state.bg_agent_running = True
                        else:
                            launch = {
                                "description": ai.get("description", ""),
                                "name": ai.get("name"),
                                "subagent_type": ai.get("subagent_type", ""),
                            }
                            if state.pending_agent_launches is None:
                                state.pending_agent_launches = []
                            state.pending_agent_launches.append(launch)
        elif etype == "rate_limit_event":
            rli = event.get("rate_limit_info")
            if rli:
                state.rate_limit_info = rli
        elif etype == "stream_event":
            inner = event.get("event", {})
            if (inner.get("type") == "content_block_delta"
                    and inner.get("delta", {}).get("type") == "text_delta"):
                state.accumulated_text += inner["delta"].get("text", "")
                state.pending_output.append(state.accumulated_text)
            elif inner.get("type") == "message_delta":
                # NOTE: context_management.applied_edits is Anthropic's
                # incremental context trimming (e.g. clear_tool_uses_*),
                # which fires on large tool outputs and is NOT the same as
                # a Claude Code session-level auto-compact.  We intentionally
                # do NOT mark compact_detected here — relying solely on the
                # token-drop heuristic above avoids false "上下文已自动压缩"
                # alerts for incremental tool-result trims.
                pass

    def parse_blocking_output(self, stdout, session_id):
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return {
                "result": f"{self.get_display_name()} 输出解析失败: {stdout[:500]}",
                "session_id": session_id,
                "is_error": True,
            }

        result_text = data.get("result", "")
        if not data.get("is_error", False) and not result_text:
            log.info(
                "Claude returned empty blocking result (silent OK): sid=%s stdout_len=%d",
                (data.get("session_id") or session_id or "-")[:8],
                len(stdout),
            )
            return {
                "result": SILENT_OK_MESSAGE,
                "session_id": data.get("session_id", session_id),
                "is_error": False,
                "usage": data.get("usage"),
                "modelUsage": data.get("modelUsage"),
                "total_cost_usd": data.get("total_cost_usd"),
            }

        return {
            "result": result_text,
            "session_id": data.get("session_id", session_id),
            "is_error": data.get("is_error", False),
            "usage": data.get("usage"),
            "modelUsage": data.get("modelUsage"),
            "total_cost_usd": data.get("total_cost_usd"),
        }

    def _build_streaming_result(self, state, session_id):
        if not state.final_result:
            return None

        fr = state.final_result
        result_text = fr.get("result") or state.accumulated_text
        sid = fr.get("session_id", session_id)

        # Fix output_tokens: assistant events report per-turn start values
        # (often single-digit), while result.usage has the cumulative total.
        # Merge the correct output_tokens into last_call_usage (which has
        # the detailed cache breakdown we want for input).
        result_usage = fr.get("usage") or {}
        if state.last_call_usage and result_usage.get("output_tokens"):
            state.last_call_usage["output_tokens"] = result_usage["output_tokens"]

        stop_reason = fr.get("stop_reason")
        deferred_tool = fr.get("deferred_tool_use")

        if not fr.get("is_error", False) and not result_text and stop_reason != "tool_deferred":
            log.info(
                "Claude returned empty streaming result (silent OK): sid=%s accumulated=%d",
                (sid or "-")[:8],
                len(state.accumulated_text),
            )
            return {
                "result": SILENT_OK_MESSAGE,
                "session_id": sid,
                "is_error": False,
                "usage": fr.get("usage"),
                "last_call_usage": state.last_call_usage,
                "modelUsage": fr.get("modelUsage"),
                "total_cost_usd": fr.get("total_cost_usd"),
                "peak_context_tokens": state.peak_context_tokens,
                "compact_detected": state.compact_detected,
                "rate_limit_info": state.rate_limit_info,
                "stop_reason": stop_reason,
                "deferred_tool_use": deferred_tool,
            }

        if state.accumulated_text and not fr.get("result"):
            log.info(
                "Claude streaming fallback used accumulated text: sid=%s chars=%d",
                (sid or "-")[:8],
                len(state.accumulated_text),
            )

        return {
            "result": result_text,
            "session_id": sid,
            "is_error": fr.get("is_error", False),
            "usage": fr.get("usage"),
            "last_call_usage": state.last_call_usage,
            "modelUsage": fr.get("modelUsage"),
            "total_cost_usd": fr.get("total_cost_usd"),
            "peak_context_tokens": state.peak_context_tokens,
            "compact_detected": state.compact_detected,
            "rate_limit_info": state.rate_limit_info,
            "stop_reason": stop_reason,
            "deferred_tool_use": deferred_tool,
        }

    def get_session_not_found_signatures(self):
        return self.SESSION_NOT_FOUND_SIGNATURES

    def get_display_name(self):
        return "Claude"


# ---------------------------------------------------------------------------
# CodexRunner
# ---------------------------------------------------------------------------

class CodexRunner(BaseRunner):
    """OpenAI Codex CLI runner."""

    ALWAYS_STREAMING = True  # session_id comes from first event (thread.started)

    def __init__(self, command: str, model: Optional[str], workspace: str, timeout: int,
                 max_budget_usd: Optional[float] = None,
                 extra_system_prompts: Optional[list[str]] = None,
                 extra_cli_args: Optional[list[str]] = None,
                 fixed_env: Optional[dict[str, str]] = None,
                 safety_prompt_mode: str = "full",
                 setting_sources: Optional[str] = None):
        if max_budget_usd is not None:
            log.warning("Codex does not support budget tracking, max_budget_usd ignored")
        super().__init__(
            command=command, model=model, workspace=workspace,
            timeout=timeout, max_budget_usd=None,
            extra_system_prompts=extra_system_prompts,
            extra_cli_args=extra_cli_args,
            fixed_env=fixed_env,
            safety_prompt_mode=safety_prompt_mode,
            setting_sources=setting_sources,
        )
        # Thread-local storage for per-invocation temp file path.
        # run() writes the path; build_args() reads it (same thread).
        self._tls = threading.local()

    def build_args(self, prompt, session_id, resume, streaming, *,
                   fork_session=False,
                   fresh_context: Optional[str] = None):
        args = [
            self.command, "exec",
        ]

        if self._extra_cli_args:
            args.extend(self._extra_cli_args)

        args.extend([
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "-C", getattr(self._tls, "workspace_override", None) or self.workspace,
        ])
        if self.model:
            args.extend(["-m", self.model])

        # Inject system prompt via -c model_instructions_file (set by run())
        instructions_path = getattr(self._tls, "instructions_path", None)
        if instructions_path:
            args.extend(["-c", f"model_instructions_file={instructions_path}"])

        if resume and session_id:
            args.extend(["resume", session_id, "--", prompt])
        else:
            # Codex assigns its own thread_id; ignore caller-provided session_id
            args.extend(["--", prompt])

        return args

    def run(self, prompt: str, session_id: Optional[str] = None,
            resume: bool = False, tag: Optional[str] = None,
            on_output=None, on_tool_status=None, on_todo_update=None,
            on_agent_update=None, env_extra: Optional[dict] = None,
            fork_session: bool = False,
            fresh_context: Optional[str] = None,
            workspace_override: Optional[str] = None) -> dict:
        """Override run() to manage per-invocation system prompt temp file.

        ``fresh_context`` is folded into the temp instructions file so the
        Codex CLI receives one merged ``model_instructions_file``. The base
        run() must not re-inject it via ``build_args`` (Codex doesn't honor
        ``--append-system-prompt``), so we pass ``fresh_context=None`` upstream.
        """
        self._tls.instructions_path = None
        self._tls.workspace_override = workspace_override
        try:
            system_prompt = self._build_system_prompt(extra=fresh_context)
            if system_prompt:
                fd, path = tempfile.mkstemp(
                    prefix="codex-instructions-", suffix=".md", text=True,
                )
                # Set early so finally can always clean up, even if fdopen/write fails
                self._tls.instructions_path = path
                with os.fdopen(fd, "w") as f:
                    f.write(system_prompt)

            return super().run(
                prompt, session_id=session_id, resume=resume,
                tag=tag, on_output=on_output, on_tool_status=on_tool_status,
                on_todo_update=on_todo_update, on_agent_update=on_agent_update,
                env_extra=env_extra,
                fresh_context=None,
                workspace_override=workspace_override,
            )
        finally:
            path = getattr(self._tls, "instructions_path", None)
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                self._tls.instructions_path = None
            self._tls.workspace_override = None

    def parse_streaming_line(self, event, state):
        etype = event.get("type", "")

        if etype == "thread.started":
            # Session ID comes from the first event; ignore if missing/null
            # to avoid persisting a caller-generated placeholder as a real
            # Codex thread id (which would break later resume attempts).
            tid = event.get("thread_id")
            if tid:
                state.session_id = tid

        elif etype == "item.completed":
            item = event.get("item") or {}
            item_type = item.get("type", "")
            if item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    state.accumulated_text += text
                    state.pending_output.append(state.accumulated_text)
            elif item_type == "error":
                err_msg = item.get("text", "") or item.get("message", "")
                log.error("Codex item error: %s", err_msg)
                # Propagate error — may be the only error signal before stream ends
                state.accumulated_text += (
                    f"\n\n⚠️ Codex error: {err_msg}" if state.accumulated_text
                    else f"Codex error: {err_msg}"
                )
                state.is_error = True
            # command_execution items are intermediate tool-use events — ignore

        elif etype == "turn.completed":
            usage = event.get("usage", {})
            if usage:
                # Normalize Codex usage keys to match Claude convention
                state.last_call_usage = {
                    "input_tokens": (usage.get("input_tokens", 0) or 0),
                    "cache_read_input_tokens": (usage.get("cached_input_tokens", 0) or 0),
                    "cache_creation_input_tokens": 0,
                    "output_tokens": (usage.get("output_tokens", 0) or 0),
                }
                ctx_tokens = (
                    state.last_call_usage["input_tokens"]
                    + state.last_call_usage["cache_read_input_tokens"]
                )
                if ctx_tokens > state.peak_context_tokens:
                    state.peak_context_tokens = ctx_tokens
            state.done = True

        elif etype == "turn.failed":
            err = event.get("error", {})
            err_msg = err.get("message", "") if isinstance(err, dict) else str(err)
            log.error("Codex turn failed: %s", err_msg)
            state.accumulated_text += f"\n\n⚠️ Codex error: {err_msg}" if state.accumulated_text else f"Codex error: {err_msg}"
            state.is_error = True
            state.done = True

        elif etype == "error":
            err_msg = event.get("message") or "Unknown error"
            log.error("Codex top-level error: %s", err_msg)
            state.accumulated_text += f"\n\n⚠️ Codex error: {err_msg}" if state.accumulated_text else f"Codex error: {err_msg}"
            state.is_error = True
            state.done = True

        # turn.started — ignored (no useful data)

    def _build_streaming_result(self, state, session_id):
        if not state.done:
            return None

        sid = state.session_id or session_id
        result_text = state.accumulated_text

        if not result_text:
            return None  # Fall through to BaseRunner empty-text handler

        usage = state.last_call_usage or {}
        model_usage = None
        if self.model:
            model_usage = {
                self.model: {
                    "contextWindow": 0,
                    "inputTokens": (usage.get("input_tokens", 0) or 0),
                    "outputTokens": (usage.get("output_tokens", 0) or 0),
                    "cacheReadInputTokens": (usage.get("cache_read_input_tokens", 0) or 0),
                    "cacheCreationInputTokens": (usage.get("cache_creation_input_tokens", 0) or 0),
                },
            }

        return {
            "result": result_text,
            "session_id": sid,
            "is_error": state.is_error,
            "usage": usage,
            "last_call_usage": usage,
            **({"modelUsage": model_usage} if model_usage else {}),
            "peak_context_tokens": state.peak_context_tokens,
            "compact_detected": False,
        }

    def parse_blocking_output(self, stdout, session_id):
        # CodexRunner always streams (ALWAYS_STREAMING=True).
        # This method should never be called.
        raise NotImplementedError("CodexRunner always uses streaming mode")

    def get_display_name(self):
        return "Codex"

    def supports_compact(self):
        return False
