"""Tests for feishu_bridge.updater — background update checker."""

from unittest.mock import MagicMock, patch


from feishu_bridge.updater import (
    UpdateChecker,
    _parse_calver,
    get_pending_version,
    get_update_banner_text,
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
    assert result["action"] == "none"
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
    assert result["action"] == "upgrade_and_restart"
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


@patch("feishu_bridge.updater._installed_version")
@patch("requests.get")
def test_pypi_up_to_date(mock_get, mock_installed):
    """PyPI latest == installed-on-disk == running → up_to_date/none."""
    uc = UpdateChecker("pypi", None, 3600)
    mock_get.return_value = MagicMock(
        json=lambda: {"info": {"version": "2026.3.24.1"}})
    mock_get.return_value.raise_for_status = MagicMock()
    mock_installed.return_value = "2026.03.24.1"

    result = uc._check_pypi("2026.03.24.1")
    assert result["status"] == "up_to_date"
    assert result["action"] == "none"


@patch("feishu_bridge.updater.subprocess.run")
@patch("feishu_bridge.updater._installed_version")
@patch("requests.get")
def test_pypi_has_update(mock_get, mock_installed, mock_run):
    """PyPI newer than disk → pipx upgrade, post-verify passes → upgrade_and_restart."""
    uc = UpdateChecker("pypi", None, 3600)
    mock_get.return_value = MagicMock(
        json=lambda: {"info": {"version": "2026.3.25"}})
    mock_get.return_value.raise_for_status = MagicMock()
    # before upgrade disk is old; after upgrade disk advanced to latest
    mock_installed.side_effect = ["2026.03.24.1", "2026.3.25"]
    mock_run.return_value = MagicMock(returncode=0)

    result = uc._check_pypi("2026.03.24.1")
    assert result["status"] == "updated"
    assert result["action"] == "upgrade_and_restart"
    assert result["version"] == "2026.3.25"
    assert uc.pending_version == "2026.3.25"
    mock_run.assert_called_once()


@patch("feishu_bridge.updater.subprocess.run")
@patch("feishu_bridge.updater._installed_version")
@patch("requests.get")
def test_pypi_disk_ahead_is_restart_only_no_pipx(mock_get, mock_installed, mock_run):
    """Disk already at latest but running process is older → restart_only, NO pipx.
    This is the loop-breaker: must not re-run pipx when disk is already current."""
    uc = UpdateChecker("pypi", None, 3600)
    mock_get.return_value = MagicMock(
        json=lambda: {"info": {"version": "2026.3.25"}})
    mock_get.return_value.raise_for_status = MagicMock()
    mock_installed.return_value = "2026.3.25"          # disk already latest

    result = uc._check_pypi("2026.03.24.1")            # running process is old
    assert result["status"] == "updated"
    assert result["action"] == "restart_only"
    assert result["version"] == "2026.3.25"
    assert uc.pending_version == "2026.3.25"
    mock_run.assert_not_called()


@patch("feishu_bridge.updater.subprocess.run")
@patch("feishu_bridge.updater._installed_version")
@patch("requests.get")
def test_pypi_post_upgrade_verification_fail(mock_get, mock_installed, mock_run):
    """pipx exits 0 but disk version did not advance → error, no pending_version."""
    uc = UpdateChecker("pypi", None, 3600)
    mock_get.return_value = MagicMock(
        json=lambda: {"info": {"version": "2026.3.25"}})
    mock_get.return_value.raise_for_status = MagicMock()
    # before old; after pipx STILL old (partial/corrupted install)
    mock_installed.side_effect = ["2026.03.24.1", "2026.03.24.1"]
    mock_run.return_value = MagicMock(returncode=0)

    result = uc._check_pypi("2026.03.24.1")
    assert result["status"] == "error"
    assert uc.pending_version is None
    assert "校验失败" in result["message"]


@patch("requests.get")
def test_pypi_network_error(mock_get):
    """PyPI request fails → error status."""
    uc = UpdateChecker("pypi", None, 3600)
    mock_get.side_effect = Exception("timeout")

    result = uc._check_pypi("2026.03.24.1")
    assert result["status"] == "error"
    assert "timeout" in result["message"]


@patch("feishu_bridge.updater.subprocess.run")
@patch("feishu_bridge.updater._installed_version")
@patch("requests.get")
def test_pypi_pipx_upgrade_failure(mock_get, mock_installed, mock_run):
    """pipx upgrade returncode != 0 → error, no pending_version."""
    uc = UpdateChecker("pypi", None, 3600)
    mock_get.return_value = MagicMock(
        json=lambda: {"info": {"version": "2026.3.25"}})
    mock_get.return_value.raise_for_status = MagicMock()
    mock_installed.return_value = "2026.03.24.1"       # disk behind → upgrade tried
    mock_run.return_value = MagicMock(
        returncode=1, stderr=b"pipx error", stdout=b"")

    result = uc._check_pypi("2026.03.24.1")
    assert result["status"] == "error"
    assert uc.pending_version is None


def test_check_and_update_serializes_concurrent_runs(monkeypatch):
    """_check_lock serializes the whole run: while one check is mid-upgrade,
    a concurrent check blocks, then returns restart_only WITHOUT launching a
    second pipx — and the nested pending_version _lock does not deadlock."""
    import threading
    import time as _time

    uc = UpdateChecker("pypi", None, 3600)
    entered = threading.Event()
    release = threading.Event()
    run_calls = []

    def blocking_run(*a, **k):
        run_calls.append(a)
        entered.set()          # we're inside pipx, holding _check_lock
        release.wait(5)         # block until the test lets us finish
        return MagicMock(returncode=0)

    monkeypatch.setattr("feishu_bridge.updater.subprocess.run", blocking_run)
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"info": {"version": "2026.3.25"}}))
    monkeypatch.setattr("feishu_bridge.__version__", "2026.03.24.1")
    monkeypatch.setattr(
        updater_mod, "_installed_version",
        MagicMock(side_effect=["2026.03.24.1", "2026.3.25"]))

    results = {}

    def call(key):
        results[key] = uc.check_and_update()

    t1 = threading.Thread(target=call, args=("first",))
    t1.start()
    assert entered.wait(5)             # first is mid-upgrade, holds _check_lock
    t2 = threading.Thread(target=call, args=("second",))
    t2.start()
    _time.sleep(0.2)
    assert "second" not in results     # second is blocked on _check_lock
    release.set()
    t1.join(5)
    t2.join(5)

    assert results["first"]["action"] == "upgrade_and_restart"
    assert results["second"]["action"] == "restart_only"
    assert len(run_calls) == 1         # second did NOT launch a second pipx


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
    assert result["action"] == "restart_only"
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


# ── pip ghost-dist purge (pre-upgrade resilience) ───────────────────


def test_purge_pip_ghost_dists_removes_tombstones(tmp_path, monkeypatch):
    import feishu_bridge
    from feishu_bridge.updater import _purge_pip_ghost_dists

    sp = tmp_path / "site-packages"
    (sp / "feishu_bridge").mkdir(parents=True)
    (sp / "feishu_bridge" / "__init__.py").write_text("")
    # pip tombstones (must be removed) + real installs (must survive)
    (sp / "~ark_oapi-1.6.5.dist-info").mkdir()
    (sp / "~-rk_oapi-1.6.7.dist-info").mkdir()
    (sp / "lark_oapi-1.6.7.dist-info").mkdir()
    (sp / "lark_oapi").mkdir()
    monkeypatch.setattr(
        feishu_bridge, "__file__",
        str(sp / "feishu_bridge" / "__init__.py"),
    )

    assert _purge_pip_ghost_dists() == 2
    assert not (sp / "~ark_oapi-1.6.5.dist-info").exists()
    assert not (sp / "~-rk_oapi-1.6.7.dist-info").exists()
    assert (sp / "lark_oapi-1.6.7.dist-info").exists()
    assert (sp / "lark_oapi").exists()


def test_purge_pip_ghost_dists_noop_when_clean(tmp_path, monkeypatch):
    import feishu_bridge
    from feishu_bridge.updater import _purge_pip_ghost_dists

    sp = tmp_path / "sp"
    (sp / "feishu_bridge").mkdir(parents=True)
    (sp / "feishu_bridge" / "__init__.py").write_text("")
    monkeypatch.setattr(
        feishu_bridge, "__file__",
        str(sp / "feishu_bridge" / "__init__.py"),
    )
    assert _purge_pip_ghost_dists() == 0


def test_purge_pip_ghost_dists_unlinks_symlink_without_following(
    tmp_path, monkeypatch,
):
    import feishu_bridge
    from feishu_bridge.updater import _purge_pip_ghost_dists

    sp = tmp_path / "sp"
    (sp / "feishu_bridge").mkdir(parents=True)
    (sp / "feishu_bridge" / "__init__.py").write_text("")
    # A '~' ghost that is a symlink to a real dir must be unlinked, never
    # rmtree'd through (which would delete the target's contents).
    target = tmp_path / "real_dir"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    ghost_link = sp / "~ghost-link"
    ghost_link.symlink_to(target)
    monkeypatch.setattr(
        feishu_bridge, "__file__",
        str(sp / "feishu_bridge" / "__init__.py"),
    )

    assert _purge_pip_ghost_dists() == 1
    assert not ghost_link.exists()           # symlink removed
    assert target.exists()                   # target NOT followed/deleted
    assert (target / "keep.txt").exists()
