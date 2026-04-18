"""Tests for feishu_bridge.bg_synthetic_turn.build_synthetic_turn."""

from __future__ import annotations

import pytest

from feishu_bridge.bg_synthetic_turn import (
    MAX_PROMPT_BYTES,
    OUTPUT_PATHS_MAX,
    REASON_MAX_BYTES,
    TAIL_MAX_BYTES,
    TAIL_TRUNCATED_MARKER,
    build_synthetic_turn,
)


TASK_ID = "abc123def456"
MANIFEST_PATH = "/home/user/.feishu-bridge/bg/abc123def456.done"


def _base_kwargs(**overrides):
    kwargs = dict(
        task_id=TASK_ID,
        manifest_path=MANIFEST_PATH,
        state="completed",
        reason=None,
        duration_seconds=42,
        exit_code=0,
        signal=None,
        output_paths=["/tmp/out1.json"],
        stdout_tail=b"hello stdout\n",
        stderr_tail=b"",
        on_done_prompt="analyze the result file",
    )
    kwargs.update(overrides)
    return kwargs


# ----- Happy path -----------------------------------------------------------

def test_minimal_inputs_produce_valid_prompt():
    prompt = build_synthetic_turn(**_base_kwargs())
    assert prompt.startswith(f"[bg-task:{TASK_ID}] Background task completed.")
    assert f"manifest: {MANIFEST_PATH}" in prompt
    assert "State: completed" in prompt
    assert "Reason: null" in prompt
    assert "Duration: 42s" in prompt
    assert "Exit code: 0" in prompt
    assert "Signal: null" in prompt
    assert "  - /tmp/out1.json" in prompt
    assert "Stdout tail (last 1024 bytes, UTF-8-safe):\nhello stdout" in prompt
    assert "Original intent:\nanalyze the result file" in prompt
    assert len(prompt.encode("utf-8")) <= MAX_PROMPT_BYTES


def test_state_verbs_cover_terminal_states():
    cases = {
        "completed": "completed",
        "failed": "failed",
        "cancelled": "was cancelled",
        "timeout": "timed out",
        "orphan": "was orphaned",
    }
    for state, verb in cases.items():
        prompt = build_synthetic_turn(**_base_kwargs(state=state))
        assert f"Background task {verb}." in prompt, state


def test_empty_output_paths_render_none_marker():
    prompt = build_synthetic_turn(**_base_kwargs(output_paths=[]))
    assert "Output files:\n  (none)" in prompt


# ----- Step 1: tail truncation to 1024B UTF-8 safe -------------------------

def test_stdout_tail_clamped_to_1024_bytes_with_marker():
    big = (b"A" * 5000) + b"TAIL_MARKER_END"
    prompt = build_synthetic_turn(
        **_base_kwargs(stdout_tail=big, stderr_tail=b""))
    assert TAIL_TRUNCATED_MARKER in prompt
    assert "TAIL_MARKER_END" in prompt
    # First 1000 bytes of 'A' padding must not survive.
    assert "AAAAA" in prompt  # some A's are kept
    # Marker is short; total size stays within budget.
    assert len(prompt.encode("utf-8")) <= MAX_PROMPT_BYTES


def test_stderr_tail_clamped_independently_of_stdout():
    big_out = b"X" * 3000
    big_err = b"Y" * 3000 + b"ERR_END"
    prompt = build_synthetic_turn(
        **_base_kwargs(stdout_tail=big_out, stderr_tail=big_err))
    assert "ERR_END" in prompt
    # Both tails truncated independently; both markers present.
    assert prompt.count(TAIL_TRUNCATED_MARKER) == 2


@pytest.mark.parametrize("filler_len", [1020, 1021, 1022, 1023, 1024])
def test_utf8_multibyte_char_straddling_tail_boundary(filler_len):
    """Emoji is 4 bytes; sweep cut offsets 0..4 around the emoji so every
    possible intra-codepoint cut position is exercised. Output must
    round-trip with no partial bytes and no U+FFFD."""
    filler = b"a" * filler_len
    emoji = "🚀".encode("utf-8")  # 4 bytes
    trailer = b"END"
    data = filler + emoji + trailer
    prompt = build_synthetic_turn(
        **_base_kwargs(stdout_tail=data, stderr_tail=b""))
    assert "END" in prompt
    assert "\ufffd" not in prompt
    assert len(prompt.encode("utf-8")) <= MAX_PROMPT_BYTES


def test_tail_shorter_than_limit_needs_no_marker():
    prompt = build_synthetic_turn(
        **_base_kwargs(stdout_tail=b"short", stderr_tail=b"alsoshort"))
    assert TAIL_TRUNCATED_MARKER not in prompt


# ----- Step 2: output_paths top-5 lexical -----------------------------------

def test_output_paths_sorted_lexically():
    paths = ["/z/z.txt", "/a/a.txt", "/m/m.txt"]
    prompt = build_synthetic_turn(**_base_kwargs(output_paths=paths))
    # Assert lexical order in rendered prompt.
    idx_a = prompt.index("/a/a.txt")
    idx_m = prompt.index("/m/m.txt")
    idx_z = prompt.index("/z/z.txt")
    assert idx_a < idx_m < idx_z


def test_output_paths_truncated_to_top_5_with_omitted_marker():
    paths = [f"/p/file_{i:02d}.json" for i in range(10)]  # 10 paths
    prompt = build_synthetic_turn(**_base_kwargs(output_paths=paths))
    # Only first 5 by lexical sort appear.
    for i in range(OUTPUT_PATHS_MAX):
        assert f"file_{i:02d}.json" in prompt
    for i in range(OUTPUT_PATHS_MAX, 10):
        assert f"file_{i:02d}.json" not in prompt
    assert "... (5 more omitted)" in prompt


def test_output_paths_exactly_limit_no_omission_marker():
    paths = [f"/p/f{i}.json" for i in range(OUTPUT_PATHS_MAX)]
    prompt = build_synthetic_turn(**_base_kwargs(output_paths=paths))
    assert "more omitted" not in prompt


# ----- Step 3: on_done_prompt truncation to remaining budget ---------------

def test_on_done_prompt_truncated_when_over_budget():
    # Force the intent section to exceed remaining budget. 32KB of ASCII.
    huge_prompt = "X" * (32 * 1024)
    result = build_synthetic_turn(**_base_kwargs(on_done_prompt=huge_prompt))
    assert len(result.encode("utf-8")) <= MAX_PROMPT_BYTES
    assert "...[truncated from original 32768 chars]" in result


def test_on_done_prompt_empty_string_is_fine():
    result = build_synthetic_turn(**_base_kwargs(on_done_prompt=""))
    assert result.endswith("Original intent:\n")


# ----- Step 4: preserved invariants under extreme truncation ----------------

def test_all_fields_maxed_stays_under_budget_and_preserves_invariants():
    """Every tunable knob maxed — overall prompt must still <= 16 KiB
    and header+manifest+state lines must still appear."""
    result = build_synthetic_turn(**_base_kwargs(
        state="failed",
        reason="segfault in step 3",
        duration_seconds=3600,
        exit_code=139,
        signal="SIGSEGV",
        output_paths=[f"/out/path_{i:04d}.txt" for i in range(200)],
        stdout_tail=b"\xee\x80\x80" * 50_000,  # >>1024B UTF-8
        stderr_tail=("e" * 100_000).encode("utf-8"),
        on_done_prompt=("p" * 100_000),
    ))
    assert len(result.encode("utf-8")) <= MAX_PROMPT_BYTES
    assert f"[bg-task:{TASK_ID}]" in result
    assert f"manifest: {MANIFEST_PATH}" in result
    assert "State: failed" in result
    assert "Reason: segfault in step 3" in result
    assert "Duration: 3600s" in result
    assert "Exit code: 139" in result
    assert "Signal: SIGSEGV" in result


def test_task_id_and_manifest_present_in_smallest_possible_output():
    """Even with empty tails and empty intent, the header + manifest
    line must show up."""
    result = build_synthetic_turn(**_base_kwargs(
        stdout_tail=b"", stderr_tail=b"", output_paths=[], on_done_prompt=""))
    assert f"[bg-task:{TASK_ID}]" in result
    assert f"manifest: {MANIFEST_PATH}" in result


# ----- Unknown state falls back to "finished" (not a crash) -----------------

def test_unknown_state_uses_fallback_verb():
    result = build_synthetic_turn(**_base_kwargs(state="weird_unmapped"))
    assert "Background task finished." in result


# ----- M1 regression: preserved fields survive multi-KB reason -------------

def test_long_reason_preserves_step4_invariants_and_stays_under_budget():
    """Wrapper error handlers can emit a full stack trace into `reason`.
    Regression for the code-reviewer finding: before the per-field cap,
    a reason this large pushed static prefix past 16 KiB, the fallback
    byte-slice dropped State/Reason/Duration/Exit/Signal from the tail
    and introduced mojibake."""
    huge_reason = "boom " * 5000  # 25_000 chars
    result = build_synthetic_turn(**_base_kwargs(
        state="failed",
        reason=huge_reason,
        exit_code=1,
        signal="SIGSEGV",
    ))
    assert len(result.encode("utf-8")) <= MAX_PROMPT_BYTES
    assert f"[bg-task:{TASK_ID}]" in result
    assert f"manifest: {MANIFEST_PATH}" in result
    assert "State: failed" in result
    assert "Duration: 42s" in result
    assert "Exit code: 1" in result
    assert "Signal: SIGSEGV" in result
    # Reason should show a leading chunk of the original + truncation marker.
    assert "Reason: boom boom" in result
    assert "...[truncated from original 25000 chars]" in result
    # No U+FFFD anywhere — proves UTF-8 safety end-to-end.
    assert "\ufffd" not in result


def test_long_reason_with_multibyte_chars_cuts_cleanly():
    """Multi-byte chars inside reason must not split at the cap boundary."""
    # Emoji every 4 chars so the boundary is guaranteed to land mid-char
    # somewhere within REASON_MAX_BYTES regardless of the exact byte count.
    huge_reason = ("A🚀B" * 2000)  # mixes ASCII + 4-byte emoji
    result = build_synthetic_turn(**_base_kwargs(reason=huge_reason))
    assert "\ufffd" not in result
    assert len(result.encode("utf-8")) <= MAX_PROMPT_BYTES
    # Reason is shown and has the marker (it's larger than REASON_MAX_BYTES).
    assert "Reason: A🚀B" in result  # leading chars intact
    assert "...[truncated from original" in result


def test_reason_under_cap_passed_through_unchanged():
    result = build_synthetic_turn(**_base_kwargs(
        reason="short and sweet"))
    assert "Reason: short and sweet" in result
    assert "...[truncated" not in result.split("Original intent:")[0]


def test_signal_over_cap_still_truncated_not_dropped():
    """Signal is defensively capped; if a bad caller sends a long string,
    it should be truncated with marker, not lost entirely."""
    result = build_synthetic_turn(**_base_kwargs(
        signal="X" * 500))  # well over SIGNAL_MAX_BYTES=64
    assert "Signal: XXXX" in result
    assert "...[truncated from original 500 chars]" in result
