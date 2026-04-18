"""Synthetic turn constructor for bg-task completion deliveries.

Per design.md §Synthetic Turn Format: assemble a prompt describing a
finished background task and deliver it as a synthetic user turn so the
owning Claude session can continue with awareness of the outcome.

Deterministic 4-step truncation keeps the final prompt <= 16 KiB:

1. stdout_tail / stderr_tail each clamped to TAIL_MAX_BYTES (UTF-8 safe).
2. output_paths sorted and trimmed to OUTPUT_PATHS_MAX with a
   "... (N more omitted)" marker when truncated.
3. on_done_prompt truncated to remaining budget with a marker reporting
   the original char count.
4. Header + manifest path + state/reason/signal/duration/exit_code lines
   are always preserved (the caller's last-resort debug anchor).

Note on header/manifest: design.md's format block illustrates the body
but step 4 explicitly names `manifest: {path}` as a preserved line, so
we emit it immediately after the header on every turn.
"""

from __future__ import annotations

MAX_PROMPT_BYTES = 16 * 1024
TAIL_MAX_BYTES = 1024
OUTPUT_PATHS_MAX = 5
TAIL_TRUNCATED_MARKER = "...[truncated]\n"

# Per-field caps for preserved prelude inputs. Sum stays well below
# MAX_PROMPT_BYTES so the step-4 invariant (preserve header/manifest/
# state/reason/signal/duration/exit_code) never collides with the
# 16 KiB ceiling. `reason` is the only realistically-unbounded input
# (wrapper error handlers may carry stack traces); the others are
# defensive against future schema drift.
REASON_MAX_BYTES = 2048
MANIFEST_PATH_MAX_BYTES = 1024
SIGNAL_MAX_BYTES = 64

_STATE_VERBS = {
    "completed": "completed",
    "failed": "failed",
    "cancelled": "was cancelled",
    "timeout": "timed out",
    "orphan": "was orphaned",
}


def _utf8_safe_tail(data: bytes, max_bytes: int) -> str:
    """Return last `max_bytes` of `data` decoded as UTF-8.

    If truncation occurred, strips any leading continuation bytes so we
    never emit a partial multibyte sequence, then prepends a marker.
    """
    if not data:
        return ""
    if len(data) <= max_bytes:
        return data.decode("utf-8", errors="replace")
    tail = data[-max_bytes:]
    # Continuation bytes have the high two bits 10xxxxxx — shift forward
    # until we're at the start of a code point.
    i = 0
    while i < len(tail) and (tail[i] & 0xC0) == 0x80:
        i += 1
    return TAIL_TRUNCATED_MARKER + tail[i:].decode("utf-8", errors="replace")


def _utf8_safe_truncate(text: str, max_bytes: int) -> str:
    """Truncate `text` to at most `max_bytes` UTF-8 bytes.

    Appends `...[truncated from original N chars]` if truncated. If
    `max_bytes` is too small to fit the marker, returns a best-effort
    prefix of the marker itself (degenerate path; caller should have
    budgeted upstream).
    """
    if not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    marker = f"...[truncated from original {len(text)} chars]"
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= max_bytes:
        return marker[:max_bytes]
    available = max_bytes - len(marker_bytes)
    truncated = encoded[:available]
    # Drop any trailing continuation bytes, then drop a multibyte lead
    # byte if it's the last byte (would decode to partial char).
    while truncated and (truncated[-1] & 0xC0) == 0x80:
        truncated = truncated[:-1]
    if truncated and (truncated[-1] & 0xC0) == 0xC0:
        truncated = truncated[:-1]
    return truncated.decode("utf-8", errors="replace") + marker


def _format_output_paths(paths: list[str]) -> list[str]:
    """Sort lexically, keep top OUTPUT_PATHS_MAX, annotate overflow."""
    if not paths:
        return []
    sorted_paths = sorted(paths)
    if len(sorted_paths) <= OUTPUT_PATHS_MAX:
        return sorted_paths
    omitted = len(sorted_paths) - OUTPUT_PATHS_MAX
    return sorted_paths[:OUTPUT_PATHS_MAX] + [f"... ({omitted} more omitted)"]


def _fmt(v: object) -> str:
    return "null" if v is None else str(v)


def build_synthetic_turn(
    *,
    task_id: str,
    manifest_path: str,
    state: str,
    reason: str | None,
    duration_seconds: float | int,
    exit_code: int | None,
    signal: str | None,
    output_paths: list[str],
    stdout_tail: bytes,
    stderr_tail: bytes,
    on_done_prompt: str,
) -> str:
    """Build a synthetic turn prompt. See module docstring for contract."""
    state_verb = _STATE_VERBS.get(state, "finished")

    # Step 1: clamp tails to TAIL_MAX_BYTES (UTF-8 safe).
    stdout_str = _utf8_safe_tail(stdout_tail, TAIL_MAX_BYTES)
    stderr_str = _utf8_safe_tail(stderr_tail, TAIL_MAX_BYTES)

    # Step 2: output_paths top N, lexical sort.
    formatted_paths = _format_output_paths(output_paths)

    # Step 4 pre-pass: bound each preserved prelude field so the total
    # prelude cannot exceed MAX_PROMPT_BYTES on any input. Without this,
    # a multi-KB `reason` (wrapper stack trace) would push the fallback
    # branch below into a raw byte slice — breaking both UTF-8 safety
    # and the state-line preservation invariant.
    manifest_capped = (
        _utf8_safe_truncate(manifest_path, MANIFEST_PATH_MAX_BYTES)
        if manifest_path else "")
    reason_capped = (
        _utf8_safe_truncate(reason, REASON_MAX_BYTES)
        if reason is not None else None)
    signal_capped = (
        _utf8_safe_truncate(signal, SIGNAL_MAX_BYTES)
        if signal is not None else None)

    # Preserved prelude — step 4 invariant.
    prelude = "\n".join([
        f"[bg-task:{task_id}] Background task {state_verb}.",
        f"manifest: {manifest_capped}",
        "",
        f"State: {state}",
        f"Reason: {_fmt(reason_capped)}",
        f"Duration: {duration_seconds}s",
        f"Exit code: {_fmt(exit_code)}",
        f"Signal: {_fmt(signal_capped)}",
    ])

    paths_body = (
        "\n".join(f"  - {p}" for p in formatted_paths)
        if formatted_paths else "  (none)"
    )
    output_section = f"Output files:\n{paths_body}"
    stdout_section = (
        f"Stdout tail (last {TAIL_MAX_BYTES} bytes, UTF-8-safe):\n{stdout_str}"
    )
    stderr_section = (
        f"Stderr tail (last {TAIL_MAX_BYTES} bytes, UTF-8-safe):\n{stderr_str}"
    )

    static_body = "\n\n".join([
        prelude, output_section, stdout_section, stderr_section, "Original intent:",
    ])
    static_prefix = static_body + "\n"

    # Step 3: on_done_prompt uses remaining budget. After the step-4
    # pre-pass + steps 1-2 clamping, static_prefix is bounded by
    # (prelude ~3.5 KiB) + (output_section ~1 KiB for 5 paths) +
    # (2 * tail ~2.5 KiB formatted) + constants — comfortably below
    # MAX_PROMPT_BYTES. The assert guards against future schema drift
    # adding a new unbounded field to prelude.
    remaining = MAX_PROMPT_BYTES - len(static_prefix.encode("utf-8"))
    assert remaining > 0, (
        f"synthetic turn static prefix {MAX_PROMPT_BYTES - remaining} B "
        f"exceeded budget; a preserved field is missing a per-field cap"
    )
    truncated_prompt = _utf8_safe_truncate(on_done_prompt or "", remaining)
    return static_prefix + truncated_prompt
