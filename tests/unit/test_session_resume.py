"""Tests for feishu_bridge.session_resume (Section 5.4a)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

import pytest

from feishu_bridge.session_resume import (
    FRESH_FALLBACK_NOTE_TEMPLATE,
    STALE_THRESHOLD_MS,
    SessionsIndex,
    build_fresh_fallback_prefix,
    resolve_resume_status,
    sentinel_probe,
)


# ---------- SessionsIndex: persistence & reload ----------

def test_touch_persists_json_readable_by_fresh_instance(tmp_path: Path):
    p = tmp_path / "sessions.json"
    idx = SessionsIndex(p)
    idx.touch("uuid-a", chat_id="oc_1", now_ms=1_700_000_000_000)
    idx.touch("uuid-b", chat_id="oc_2", now_ms=1_700_000_000_500)

    # Reopen from disk — fresh instance, must see both entries.
    reopened = SessionsIndex(p)
    assert reopened.lookup("uuid-a") == {
        "last_seen_at_ms": 1_700_000_000_000, "chat_id": "oc_1",
    }
    assert reopened.lookup("uuid-b") == {
        "last_seen_at_ms": 1_700_000_000_500, "chat_id": "oc_2",
    }


def test_touch_uses_atomic_replace_leaves_no_temp_files(tmp_path: Path):
    p = tmp_path / "sessions.json"
    idx = SessionsIndex(p)
    for i in range(5):
        idx.touch(f"uuid-{i}", chat_id="oc_x", now_ms=1_000 + i)
    siblings = list(p.parent.iterdir())
    # Only the final sessions.json should remain — no orphaned .tmp files.
    assert siblings == [p], siblings


def test_corrupt_json_does_not_crash_startup(tmp_path: Path):
    p = tmp_path / "sessions.json"
    p.write_text("not valid json {", encoding="utf-8")
    idx = SessionsIndex(p)  # must not raise
    assert idx.lookup("anything") is None
    # Subsequent touch still works and overwrites the corrupt file.
    idx.touch("uuid-new", chat_id="oc_new", now_ms=42)
    assert json.loads(p.read_text(encoding="utf-8"))["uuid-new"]["chat_id"] == "oc_new"


def test_lookup_returns_none_for_unknown_and_empty(tmp_path: Path):
    idx = SessionsIndex(tmp_path / "sessions.json")
    assert idx.lookup("never-seen") is None
    assert idx.lookup("") is None


def test_touch_ignores_empty_session_id(tmp_path: Path):
    p = tmp_path / "sessions.json"
    idx = SessionsIndex(p)
    idx.touch("", chat_id="oc_x", now_ms=1)
    # No file written, because nothing to persist.
    assert not p.exists()


def test_touch_refreshes_existing_entry(tmp_path: Path):
    idx = SessionsIndex(tmp_path / "sessions.json")
    idx.touch("uuid-a", chat_id="oc_1", now_ms=1_000)
    idx.touch("uuid-a", chat_id="oc_1", now_ms=9_999)
    assert idx.lookup("uuid-a")["last_seen_at_ms"] == 9_999


def test_load_drops_malformed_entries_silently(tmp_path: Path):
    p = tmp_path / "sessions.json"
    # Mix of good and malformed entries (value not a dict).
    p.write_text(json.dumps({
        "good": {"last_seen_at_ms": 1, "chat_id": "oc_1"},
        "bad_list": ["x", "y"],
        "bad_str": "garbage",
    }), encoding="utf-8")
    idx = SessionsIndex(p)
    assert idx.lookup("good") is not None
    assert idx.lookup("bad_list") is None
    assert idx.lookup("bad_str") is None


# ---------- SessionsIndex: concurrency ----------

def test_concurrent_touch_from_many_threads(tmp_path: Path):
    idx = SessionsIndex(tmp_path / "sessions.json")
    errors: list[BaseException] = []

    def worker(n: int):
        try:
            for i in range(20):
                idx.touch(f"uuid-{n}-{i}", chat_id=f"oc_{n}", now_ms=1_000 + i)
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert errors == []
    # 10 threads × 20 sessions = 200 entries, all present.
    data = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
    assert len(data) == 200


# ---------- sentinel_probe: classification matrix ----------

def _fake_completed(rc: int, stderr: str = "") -> CompletedProcess:
    return CompletedProcess(args=["x"], returncode=rc, stdout="", stderr=stderr)


def test_probe_timeout_maps_to_fresh_fallback():
    with patch("feishu_bridge.session_resume.subprocess.run",
               side_effect=TimeoutExpired(cmd="x", timeout=5)):
        assert sentinel_probe("uuid-x", timeout_sec=0.01) == \
            ("fresh_fallback", "probe_timeout")


def test_probe_zero_exit_maps_to_resumed():
    with patch("feishu_bridge.session_resume.subprocess.run",
               return_value=_fake_completed(0)):
        assert sentinel_probe("uuid-x") == ("resumed", "probe_ok")


def test_probe_session_not_found_maps_to_fresh_fallback():
    with patch("feishu_bridge.session_resume.subprocess.run",
               return_value=_fake_completed(1, stderr="Error: Session not found")):
        assert sentinel_probe("uuid-x") == ("fresh_fallback", "session_not_found")


def test_probe_other_nonzero_maps_to_resume_failed():
    with patch("feishu_bridge.session_resume.subprocess.run",
               return_value=_fake_completed(2, stderr="rate limit exceeded")):
        assert sentinel_probe("uuid-x") == ("resume_failed", "probe_error")


def test_probe_claude_not_installed_maps_to_resume_failed():
    with patch("feishu_bridge.session_resume.subprocess.run",
               side_effect=FileNotFoundError("claude")):
        assert sentinel_probe("uuid-x") == ("resume_failed", "claude_not_found")


def test_probe_matches_compacted_stderr():
    with patch("feishu_bridge.session_resume.subprocess.run",
               return_value=_fake_completed(1, stderr="Session was compacted")):
        # "compacted" is treated as session-gone → fresh_fallback.
        assert sentinel_probe("uuid-x") == ("fresh_fallback", "session_not_found")


# ---------- resolve_resume_status: policy branches ----------

def test_resolve_no_session_id_is_fresh_fallback(tmp_path: Path):
    idx = SessionsIndex(tmp_path / "sessions.json")
    probe_calls = []
    def probe(sid): probe_calls.append(sid); return ("resumed", "probe_ok")
    assert resolve_resume_status(None, idx, 0, probe) == \
        ("fresh_fallback", "not_in_index")
    assert resolve_resume_status("", idx, 0, probe) == \
        ("fresh_fallback", "not_in_index")
    assert probe_calls == []  # never probes for missing id


def test_resolve_unknown_session_id_is_fresh_fallback_no_probe(tmp_path: Path):
    idx = SessionsIndex(tmp_path / "sessions.json")
    probe_calls = []
    def probe(sid): probe_calls.append(sid); return ("resumed", "probe_ok")
    assert resolve_resume_status("never-seen", idx, 1_000, probe) == \
        ("fresh_fallback", "not_in_index")
    assert probe_calls == []  # no probe for never-seen sessions


def test_resolve_recent_activity_skips_probe(tmp_path: Path):
    idx = SessionsIndex(tmp_path / "sessions.json")
    idx.touch("uuid-recent", chat_id="oc_x", now_ms=1_000_000)
    probe_calls = []
    def probe(sid): probe_calls.append(sid); return ("resumed", "probe_ok")

    # Within 24h window.
    assert resolve_resume_status(
        "uuid-recent", idx, 1_000_000 + STALE_THRESHOLD_MS - 1, probe,
    ) == ("resumed", "recent_activity")
    assert probe_calls == []


def test_resolve_stale_entry_triggers_probe(tmp_path: Path):
    idx = SessionsIndex(tmp_path / "sessions.json")
    idx.touch("uuid-stale", chat_id="oc_x", now_ms=1_000)
    probe_calls = []

    def probe(sid):
        probe_calls.append(sid)
        return ("fresh_fallback", "session_not_found")

    # Past 24h threshold → must probe.
    assert resolve_resume_status(
        "uuid-stale", idx, 1_000 + STALE_THRESHOLD_MS + 1, probe,
    ) == ("fresh_fallback", "session_not_found")
    assert probe_calls == ["uuid-stale"]


def test_resolve_stale_entry_probe_success_returns_resumed(tmp_path: Path):
    idx = SessionsIndex(tmp_path / "sessions.json")
    idx.touch("uuid-stale-ok", chat_id="oc_x", now_ms=1_000)
    def probe(sid): return ("resumed", "probe_ok")
    assert resolve_resume_status(
        "uuid-stale-ok", idx, 1_000 + STALE_THRESHOLD_MS + 5_000, probe,
    ) == ("resumed", "probe_ok")


def test_resolve_stale_entry_probe_failure_bubbles_resume_failed(tmp_path: Path):
    idx = SessionsIndex(tmp_path / "sessions.json")
    idx.touch("uuid-stale-err", chat_id="oc_x", now_ms=1_000)
    def probe(sid): return ("resume_failed", "probe_error")
    assert resolve_resume_status(
        "uuid-stale-err", idx, 1_000 + STALE_THRESHOLD_MS + 5_000, probe,
    ) == ("resume_failed", "probe_error")


def test_resolve_future_timestamp_falls_through_to_probe(tmp_path: Path):
    """Clock rollback / NTP jump / corrupt JSON can produce a future
    timestamp (last_seen > now). Fail-closed: probe instead of blindly
    trusting the bogus timestamp and skipping revalidation forever."""
    idx = SessionsIndex(tmp_path / "sessions.json")
    idx.touch("uuid-future", chat_id="oc_x", now_ms=2_000_000)
    probe_calls = []
    def probe(sid):
        probe_calls.append(sid)
        return ("resumed", "probe_ok")
    # now_ms < last_seen → age is negative
    assert resolve_resume_status(
        "uuid-future", idx, 1_000_000, probe,
    ) == ("resumed", "probe_ok")
    assert probe_calls == ["uuid-future"]


def test_resolve_wraps_probe_exceptions_into_resume_failed(tmp_path: Path):
    """probe_fn injection is a public contract. A raising probe must
    not escape into the delivery watcher — dropping a bg-task turn
    because a probe crashed would mean the user never sees the result."""
    idx = SessionsIndex(tmp_path / "sessions.json")
    idx.touch("uuid-x", chat_id="oc_x", now_ms=1_000)

    def angry_probe(sid):
        raise RuntimeError("simulated probe blowup")

    result = resolve_resume_status(
        "uuid-x", idx, 1_000 + STALE_THRESHOLD_MS + 1, angry_probe,
    )
    assert result == ("resume_failed", "probe_exception")


def test_resolve_entry_missing_timestamp_treated_as_stale(tmp_path: Path):
    """Defensive: if someone hand-edits JSON to drop last_seen_at_ms,
    we should probe rather than crash."""
    idx = SessionsIndex(tmp_path / "sessions.json")
    # Bypass touch() to inject a malformed entry directly on disk.
    (tmp_path / "sessions.json").write_text(json.dumps({
        "uuid-malformed": {"chat_id": "oc_x"},  # no last_seen_at_ms
    }), encoding="utf-8")
    idx = SessionsIndex(tmp_path / "sessions.json")

    probe_calls = []
    def probe(sid): probe_calls.append(sid); return ("resumed", "probe_ok")
    assert resolve_resume_status("uuid-malformed", idx, 9_999_999_999, probe) == \
        ("resumed", "probe_ok")
    assert probe_calls == ["uuid-malformed"]


# ---------- NOTE prefix renderer ----------

def test_build_fresh_fallback_prefix_substitutes_reason():
    out = build_fresh_fallback_prefix("probe_timeout")
    assert "(reason: probe_timeout)" in out
    assert "fresh-context bg-task completion" in out
    # Template surface is verbatim from design.md — regression guard.
    assert out.startswith("[NOTE: original session no longer resumable")
    assert out.endswith("read output files for ground truth.]")


def test_build_fresh_fallback_prefix_missing_reason_falls_back_to_unknown():
    out = build_fresh_fallback_prefix("")
    assert "(reason: unknown)" in out


def test_template_constant_unchanged():
    """Regression guard: do not silently reword the NOTE template —
    spec pins this string."""
    assert "{reason}" in FRESH_FALLBACK_NOTE_TEMPLATE
    assert "Previously-discussed context is NOT available" in \
        FRESH_FALLBACK_NOTE_TEMPLATE
