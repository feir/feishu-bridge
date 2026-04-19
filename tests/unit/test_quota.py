"""Tests for feishu_bridge.quota and quota integration in worker/commands."""

import time

import pytest

from feishu_bridge.quota import (
    CodexQuotaSnapshot,
    QuotaSnapshot,
    QuotaWindow,
    WINDOW_LABELS,
    _load_session_key,
    _parse_iso_to_epoch,
    _parse_response,
    fetch_codex_quota,
)
from feishu_bridge import worker as bridge_worker


# ── _parse_iso_to_epoch ──────────────────────────────────────────────


def test_parse_iso_to_epoch_valid():
    epoch = _parse_iso_to_epoch("2026-03-22T21:00:00.629766+00:00")
    assert epoch > 1_700_000_000  # sanity


def test_parse_iso_to_epoch_valid_utc():
    epoch = _parse_iso_to_epoch("2026-01-01T00:00:00+00:00")
    assert epoch == 1767225600.0


def test_parse_iso_to_epoch_invalid():
    assert _parse_iso_to_epoch("not-a-date") == 0.0


def test_parse_iso_to_epoch_empty():
    assert _parse_iso_to_epoch("") == 0.0


# ── _parse_response ─────────────────────────────────────────────────


_SAMPLE_API_RESPONSE = {
    "five_hour": {
        "utilization": 2.0,
        "resets_at": "2026-03-22T21:00:00.629766+00:00",
    },
    "seven_day": {
        "utilization": 18.0,
        "resets_at": "2026-03-28T04:00:00.629785+00:00",
    },
    "seven_day_opus": None,
    "seven_day_sonnet": {
        "utilization": 1.0,
        "resets_at": "2026-03-28T15:00:00.629792+00:00",
    },
    "seven_day_oauth_apps": None,
    "seven_day_cowork": None,
    "iguana_necktie": None,
    "extra_usage": {
        "is_enabled": False,
        "monthly_limit": None,
        "used_credits": None,
        "utilization": None,
    },
}


def test_parse_response_valid():
    snap = _parse_response(_SAMPLE_API_RESPONSE)
    assert snap.available
    assert not snap.error
    assert "five_hour" in snap.windows
    assert "seven_day" in snap.windows
    assert "seven_day_sonnet" in snap.windows
    assert "seven_day_opus" not in snap.windows  # None → skipped
    assert snap.windows["five_hour"].utilization == 2.0
    assert snap.windows["seven_day"].utilization == 18.0
    assert snap.windows["five_hour"].resets_at_epoch > 0


def test_parse_response_empty():
    snap = _parse_response({})
    assert not snap.available  # no windows
    assert not snap.error


def test_parse_response_extra_usage_enabled():
    data = {**_SAMPLE_API_RESPONSE, "extra_usage": {"is_enabled": True}}
    snap = _parse_response(data)
    assert snap.extra_usage_enabled


def test_parse_response_preserves_poll_interval():
    snap = _parse_response(_SAMPLE_API_RESPONSE, poll_interval=600)
    assert snap.poll_interval == 600


# ── QuotaSnapshot properties ────────────────────────────────────────


def test_snapshot_stale_fresh():
    snap = QuotaSnapshot(timestamp=time.time(), windows={"x": QuotaWindow(0, "")})
    assert not snap.stale


def test_snapshot_stale_old():
    snap = QuotaSnapshot(
        timestamp=time.time() - 700,  # > 2 * 300
        windows={"x": QuotaWindow(0, "")},
    )
    assert snap.stale


def test_snapshot_stale_respects_poll_interval():
    snap = QuotaSnapshot(
        timestamp=time.time() - 700,
        windows={"x": QuotaWindow(0, "")},
        poll_interval=600,  # 2 * 600 = 1200 > 700
    )
    assert not snap.stale  # not stale with longer interval


def test_snapshot_available_with_windows():
    snap = QuotaSnapshot(
        timestamp=time.time(),
        windows={"five_hour": QuotaWindow(5.0, "")},
    )
    assert snap.available


def test_snapshot_not_available_on_error():
    snap = QuotaSnapshot(
        timestamp=time.time(),
        windows={"five_hour": QuotaWindow(5.0, "")},
        error="auth failed",
    )
    assert not snap.available


def test_snapshot_not_available_empty():
    snap = QuotaSnapshot(timestamp=time.time())
    assert not snap.available


def test_cookie_expiry_warning_soon():
    snap = QuotaSnapshot(
        timestamp=time.time(),
        cookie_expires_at=time.time() + 86400,  # 1 day from now
    )
    assert snap.cookie_expiry_warning is not None
    assert "1.0 天" in snap.cookie_expiry_warning


def test_cookie_expiry_warning_expired():
    snap = QuotaSnapshot(
        timestamp=time.time(),
        cookie_expires_at=time.time() - 3600,  # 1 hour ago
    )
    assert "已过期" in snap.cookie_expiry_warning


def test_cookie_expiry_no_warning():
    snap = QuotaSnapshot(
        timestamp=time.time(),
        cookie_expires_at=time.time() + 86400 * 10,  # 10 days out
    )
    assert snap.cookie_expiry_warning is None


def test_cookie_expiry_unknown():
    snap = QuotaSnapshot(timestamp=time.time(), cookie_expires_at=0.0)
    assert snap.cookie_expiry_warning is None


# ── _load_session_key ───────────────────────────────────────────────


def test_load_session_key_valid(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".claude.ai\tTRUE\t/\tTRUE\t1900000000\tsessionKey\tsk-ant-test-123\n"
    )
    key, expires = _load_session_key(cookie_file)
    assert key == "sk-ant-test-123"
    assert expires == 1900000000.0


def test_load_session_key_missing_file(tmp_path):
    key, expires = _load_session_key(tmp_path / "nonexistent.txt")
    assert key is None
    assert expires == 0.0


def test_load_session_key_no_session_key(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".claude.ai\tTRUE\t/\tTRUE\t1900000000\tother_cookie\tvalue\n"
    )
    key, expires = _load_session_key(cookie_file)
    assert key is None
    assert expires == 0.0


# ── WINDOW_LABELS ───────────────────────────────────────────────────


def test_window_labels_keys():
    assert "five_hour" in WINDOW_LABELS
    assert "seven_day" in WINDOW_LABELS
    assert WINDOW_LABELS["five_hour"] == "5h"


# ── _build_quota_alert ──────────────────────────────────────────────


def _make_snap(windows: dict[str, tuple[float, float]]) -> QuotaSnapshot:
    """Helper: windows = {"five_hour": (utilization, resets_in_seconds)}."""
    ws = {}
    for k, (util, resets_in) in windows.items():
        ws[k] = QuotaWindow(
            utilization=util,
            resets_at="",
            resets_at_epoch=time.time() + resets_in,
        )
    return QuotaSnapshot(timestamp=time.time(), windows=ws)


def test_build_quota_alert_rejected_stream_event():
    result = {
        "rate_limit_info": {
            "status": "rejected",
            "rateLimitType": "five_hour",
            "resetsAt": time.time() + 3600,
            "utilization": 1.0,
        }
    }
    alert = bridge_worker._build_quota_alert(result)
    assert "🚫" in alert
    assert "5 小时" in alert
    assert "100%" in alert


def test_build_quota_alert_api_snapshot_high():
    result = {"rate_limit_info": {"status": "allowed"}}
    snap = _make_snap({"five_hour": (85.0, 3600), "seven_day": (60.0, 86400)})
    alert = bridge_worker._build_quota_alert(result, snap)
    assert "🔴" in alert
    assert "5h: 85%" in alert
    assert "🟡" in alert
    assert "7d: 60%" in alert


def test_build_quota_alert_api_snapshot_low():
    result = {"rate_limit_info": {"status": "allowed"}}
    snap = _make_snap({"five_hour": (10.0, 3600), "seven_day": (20.0, 86400)})
    alert = bridge_worker._build_quota_alert(result, snap)
    assert alert == ""  # all below 50% threshold


def test_build_quota_alert_no_data():
    result = {}
    alert = bridge_worker._build_quota_alert(result)
    assert alert == ""


def test_build_quota_alert_allowed_warning_with_reset_time():
    result = {
        "rate_limit_info": {
            "status": "allowed_warning",
            "rateLimitType": "five_hour",
            "resetsAt": time.time() + 5400,  # 1h30m
            "utilization": 0.85,
        }
    }
    alert = bridge_worker._build_quota_alert(result)
    assert "⚠️" in alert
    assert "5 小时" in alert
    assert "85%" in alert
    assert "后重置" in alert


def test_build_quota_alert_stale_snapshot_ignored():
    result = {"rate_limit_info": {"status": "allowed"}}
    snap = _make_snap({"five_hour": (90.0, 3600)})
    snap.timestamp = time.time() - 1000  # make stale
    alert = bridge_worker._build_quota_alert(result, snap)
    assert alert == ""  # stale snapshot ignored


def test_build_quota_alert_warning_and_snapshot_same_window_deduped():
    """Stream allowed_warning + API snapshot for same window → single line (no dup)."""
    result = {
        "rate_limit_info": {
            "status": "allowed_warning",
            "rateLimitType": "five_hour",
            "resetsAt": time.time() + 840,  # 14m
            "utilization": 0.97,
        }
    }
    snap = _make_snap({"five_hour": (98.0, 840), "seven_day": (30.0, 86400)})
    alert = bridge_worker._build_quota_alert(result, snap)
    # Branch 2 (stream warning, Chinese label) should fire
    assert "⚠️" in alert
    assert "5 小时配额 97%" in alert
    # Branch 3 must skip five_hour since it is already covered
    assert "5h:" not in alert
    # 7d window below 50% threshold → not reported
    assert "7d:" not in alert
    # Exactly one line
    assert alert.count("\n") == 0


# ── _context_health_alert with quota ────────────────────────────────


def test_context_health_alert_with_quota_no_context_alert():
    """Low context + high quota → quota alert only."""
    result = {
        "last_call_usage": {"input_tokens": 30000, "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0},
        "modelUsage": {"claude-opus-4-7": {"contextWindow": 200_000}},
        "rate_limit_info": {"status": "allowed"},
    }
    snap = _make_snap({"five_hour": (85.0, 3600)})
    alert = bridge_worker._context_health_alert(result, quota_snapshot=snap)
    assert alert is not None
    assert "🔴" in alert
    assert "5h: 85%" in alert
    assert "Context" not in alert  # only 15% context


def test_context_health_alert_with_quota_and_context_alert():
    """High context + high quota → both alerts."""
    result = {
        "last_call_usage": {"input_tokens": 170000, "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0},
        "modelUsage": {"claude-opus-4-7": {"contextWindow": 200_000}},
        "rate_limit_info": {"status": "allowed"},
    }
    snap = _make_snap({"seven_day": (75.0, 86400)})
    alert = bridge_worker._context_health_alert(result, quota_snapshot=snap)
    assert "Context 85%" in alert
    assert "7d: 75%" in alert


def test_context_health_alert_no_quota():
    """Works without quota snapshot (backward compat)."""
    result = {
        "last_call_usage": {"input_tokens": 150000, "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0},
        "modelUsage": {"claude-opus-4-7": {"contextWindow": 200_000}},
    }
    alert = bridge_worker._context_health_alert(result, quota_snapshot=None)
    assert alert is not None
    assert "Context 75%" in alert


# ── Codex quota ──────────────────────────────────────────────────────


def test_codex_snapshot_available():
    snap = CodexQuotaSnapshot(timestamp=time.time(), plan_type="plus")
    assert snap.available


def test_codex_snapshot_error():
    snap = CodexQuotaSnapshot(timestamp=time.time(), error="no auth")
    assert not snap.available


def test_codex_snapshot_stale():
    snap = CodexQuotaSnapshot(timestamp=time.time() - 700)
    assert snap.stale


def test_fetch_codex_quota_no_auth(tmp_path):
    snap = fetch_codex_quota(auth_path=tmp_path / "nonexistent.json")
    assert not snap.available
    assert "not found" in snap.error


def test_fetch_codex_quota_empty_token(tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"tokens": {}}')
    snap = fetch_codex_quota(auth_path=auth_file)
    assert not snap.available
    assert "no access_token" in snap.error
