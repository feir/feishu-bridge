"""Tests for feishu_bridge.updater — background update checker."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from feishu_bridge.updater import (
    UpdateChecker,
    _parse_calver,
    get_pending_version,
    get_update_banner_text,
    init_updater,
)
from feishu_bridge import updater as updater_mod


# ── CalVer parsing ──────────────────────────────────────────────────


def test_parse_calver_basic():
    assert _parse_calver("2026.03.24") == (2026, 3, 24)


def test_parse_calver_with_patch():
    assert _parse_calver("2026.03.24.1") == (2026, 3, 24, 1)


def test_parse_calver_pypi_normalized():
    """PyPI strips leading zeros: '2026.3.24.1' == '2026.03.24.1'."""
    assert _parse_calver("2026.3.24.1") == _parse_calver("2026.03.24.1")


def test_parse_calver_comparison():
    assert _parse_calver("2026.03.25") > _parse_calver("2026.03.24.1")
    assert _parse_calver("2026.03.24.2") > _parse_calver("2026.03.24.1")
    assert _parse_calver("2026.03.24") < _parse_calver("2026.03.24.1")


# ── get_pending_version (no init) ───────────────────────────────────


def test_get_pending_version_no_init():
    old = updater_mod._updater
    updater_mod._updater = None
    try:
        assert get_pending_version() is None
    finally:
        updater_mod._updater = old


def test_get_pending_version_with_value():
    old = updater_mod._updater
    uc = UpdateChecker("git", "/tmp", 3600)
    uc.pending_version = "2026.03.25"
    updater_mod._updater = uc
    try:
        assert get_pending_version() == "2026.03.25"
    finally:
        updater_mod._updater = old


# ── Git mode ────────────────────────────────────────────────────────


@patch("feishu_bridge.updater.subprocess.run")
def test_git_up_to_date(mock_run):
    """No new commits → up_to_date."""
    uc = UpdateChecker("git", "/fake/path", 3600)

    # git fetch succeeds
    mock_run.side_effect = [
        MagicMock(returncode=0),           # git fetch
        MagicMock(returncode=0, stdout=b"0\n"),  # rev-list count = 0
    ]

    result = uc._check_git("2026.03.24.1")
    assert result["status"] == "up_to_date"
    assert uc.pending_version is None


@patch("feishu_bridge.updater.subprocess.run")
def test_git_has_update(mock_run):
    """New commits available → pull and set pending_version."""
    uc = UpdateChecker("git", "/fake/path", 3600)

    mock_run.side_effect = [
        MagicMock(returncode=0),           # git fetch
        MagicMock(returncode=0, stdout=b"3\n"),  # rev-list count = 3
        MagicMock(returncode=0),           # git pull --ff-only
    ]

    # Mock _read_git_version to return new version
    with patch.object(uc, "_read_git_version", return_value="2026.03.25"):
        result = uc._check_git("2026.03.24.1")

    assert result["status"] == "updated"
    assert result["version"] == "2026.03.25"
    assert uc.pending_version == "2026.03.25"


@patch("feishu_bridge.updater.subprocess.run")
def test_git_fetch_failure(mock_run):
    """git fetch fails → error status."""
    uc = UpdateChecker("git", "/fake/path", 3600)
    mock_run.return_value = MagicMock(
        returncode=1, stderr=b"network error")

    result = uc._check_git("2026.03.24.1")
    assert result["status"] == "error"
    assert "git fetch failed" in result["message"]


@patch("feishu_bridge.updater.subprocess.run")
def test_git_pull_failure(mock_run):
    """git pull --ff-only fails → error status, no pending_version."""
    uc = UpdateChecker("git", "/fake/path", 3600)

    mock_run.side_effect = [
        MagicMock(returncode=0),           # git fetch
        MagicMock(returncode=0, stdout=b"2\n"),  # rev-list
        MagicMock(returncode=1, stderr=b"not ff"),  # pull fails
    ]

    result = uc._check_git("2026.03.24.1")
    assert result["status"] == "error"
    assert "git pull failed" in result["message"]
    assert uc.pending_version is None


@patch("feishu_bridge.updater.subprocess.run")
def test_git_no_source_path(mock_run):
    """Git mode without source_path → error."""
    uc = UpdateChecker("git", None, 3600)
    result = uc._check_git("2026.03.24.1")
    assert result["status"] == "error"
    mock_run.assert_not_called()


# ── PyPI mode ───────────────────────────────────────────────────────


@patch("requests.get")
def test_pypi_up_to_date(mock_get):
    """PyPI version == local → up_to_date."""
    uc = UpdateChecker("pypi", None, 3600)
    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: {"info": {"version": "2026.3.24.1"}},
    )
    mock_get.return_value.raise_for_status = MagicMock()

    result = uc._check_pypi("2026.03.24.1")
    assert result["status"] == "up_to_date"


@patch("feishu_bridge.updater.subprocess.run")
@patch("requests.get")
def test_pypi_has_update(mock_get, mock_run):
    """PyPI has newer version → pipx upgrade + set pending_version."""
    uc = UpdateChecker("pypi", None, 3600)
    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: {"info": {"version": "2026.3.25"}},
    )
    mock_get.return_value.raise_for_status = MagicMock()
    mock_run.return_value = MagicMock(returncode=0)  # pipx upgrade

    result = uc._check_pypi("2026.03.24.1")
    assert result["status"] == "updated"
    assert result["version"] == "2026.3.25"
    assert uc.pending_version == "2026.3.25"


@patch("requests.get")
def test_pypi_network_error(mock_get):
    """PyPI request fails → error status."""
    uc = UpdateChecker("pypi", None, 3600)
    mock_get.side_effect = Exception("timeout")

    result = uc._check_pypi("2026.03.24.1")
    assert result["status"] == "error"
    assert "timeout" in result["message"]


@patch("feishu_bridge.updater.subprocess.run")
@patch("requests.get")
def test_pypi_pipx_upgrade_failure(mock_get, mock_run):
    """pipx upgrade fails → error, no pending_version."""
    uc = UpdateChecker("pypi", None, 3600)
    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: {"info": {"version": "2026.3.25"}},
    )
    mock_get.return_value.raise_for_status = MagicMock()
    mock_run.return_value = MagicMock(
        returncode=1, stderr=b"pipx error", stdout=b"")

    result = uc._check_pypi("2026.03.24.1")
    assert result["status"] == "error"
    assert uc.pending_version is None


# ── check_and_update dispatch ───────────────────────────────────────


def test_check_and_update_not_initialized():
    from feishu_bridge.updater import check_and_update
    old = updater_mod._updater
    updater_mod._updater = None
    try:
        result = check_and_update()
        assert result["status"] == "error"
    finally:
        updater_mod._updater = old


def test_check_and_update_early_return_when_pending():
    """Once pending_version is set, check_and_update() returns immediately."""
    uc = UpdateChecker("git", "/fake/path", 3600)
    uc.pending_version = "2026.03.25"
    result = uc.check_and_update()
    assert result["status"] == "updated"
    assert result["version"] == "2026.03.25"


# ── get_update_banner_text ──────────────────────────────────────────


def test_get_update_banner_text_no_update():
    old = updater_mod._updater
    updater_mod._updater = None
    try:
        assert get_update_banner_text() is None
    finally:
        updater_mod._updater = old


def test_get_update_banner_text_with_update():
    old = updater_mod._updater
    uc = UpdateChecker("git", "/tmp", 3600)
    uc.pending_version = "2026.03.25"
    updater_mod._updater = uc
    try:
        text = get_update_banner_text()
        assert "2026.03.25" in text
        assert "orange" in text
        assert "/restart" in text
    finally:
        updater_mod._updater = old


# ── git @{upstream} fallback ────────────────────────────────────────


@patch("feishu_bridge.updater.subprocess.run")
def test_git_upstream_fallback_to_origin_head(mock_run):
    """If @{upstream} fails, falls back to origin/HEAD."""
    uc = UpdateChecker("git", "/fake/path", 3600)

    mock_run.side_effect = [
        MagicMock(returncode=0),                          # git fetch
        MagicMock(returncode=128, stdout=b""),             # @{upstream} fails
        MagicMock(returncode=0, stdout=b"0\n"),            # origin/HEAD succeeds
    ]

    result = uc._check_git("2026.03.24.1")
    assert result["status"] == "up_to_date"
    # Verify the fallback was called
    calls = mock_run.call_args_list
    assert "@{upstream}" in str(calls[1])
    assert "origin/HEAD" in str(calls[2])
