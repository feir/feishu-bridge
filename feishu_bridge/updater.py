"""Background update checker for feishu-bridge.

Detects new versions (git or PyPI), pulls updates without restarting,
and exposes pending_version for card footer notification.
"""

import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("feishu-bridge")


def _installed_version() -> Optional[str]:
    """The on-disk installed version of feishu-bridge, read fresh.

    Unlike the in-memory ``feishu_bridge.__version__`` constant (frozen at
    process start), this reflects a ``pipx upgrade`` that replaced the dist-info
    AFTER startup: ``importlib.metadata.version`` reads METADATA from disk per
    call and its discovery cache keys on the site-packages mtime, which a pipx
    reinstall updates. Returns None if metadata can't be read.
    """
    try:
        import importlib.metadata
        return importlib.metadata.version("feishu-bridge")
    except Exception:
        log.debug("cannot read installed feishu-bridge version", exc_info=True)
        return None


def _purge_pip_ghost_dists() -> int:
    """Remove pip 'tombstone' dirs (names starting with '~') from the bridge's
    own venv site-packages, returning how many were removed.

    pip renames a distribution to '~ame' when it cannot fully remove it during
    an interrupted/failed (un)install. These ghosts trigger 'Ignoring invalid
    distribution' warnings and corrupt pip's uninstall bookkeeping, so a later
    ``pipx upgrade`` (which does ``--force-reinstall``) aborts with a
    missing-file OSError. Pre-cleaning makes upgrades resilient to a prior
    crash. Best-effort; never raises.
    """
    try:
        import feishu_bridge
        site_packages = Path(feishu_bridge.__file__).resolve().parent.parent
        ghosts = list(site_packages.glob("~*"))
    except Exception:
        log.debug("ghost-dist purge: cannot enumerate site-packages",
                  exc_info=True)
        return 0
    removed = 0
    for ghost in ghosts:
        try:
            # Symlink (even one pointing at a dir) must be unlinked, not
            # rmtree'd. Don't suppress errors — a path that survives must not
            # be counted as cleaned, else the upgrade failure mode silently
            # persists and is harder to diagnose.
            if ghost.is_symlink() or not ghost.is_dir():
                ghost.unlink()
            else:
                shutil.rmtree(ghost)
        except OSError as e:
            log.debug("ghost-dist purge: cannot remove %s: %s", ghost, e)
            continue
        removed += 1
    if removed:
        log.info("ghost-dist purge: removed %d pip tombstone(s) before upgrade",
                 removed)
    return removed

# Module-level singleton
_updater: Optional["UpdateChecker"] = None

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_updater(mode: str, source_path: Optional[str] = None,
                 check_interval: int = 6 * 3600):
    """Initialize the global UpdateChecker and start its background thread.

    Args:
        mode: "git" or "pypi" (from _get_install_info).
        source_path: Local git repo path (git mode only).
        check_interval: Seconds between checks (default 6h).
    """
    global _updater
    _updater = UpdateChecker(mode, source_path, check_interval)
    _updater.start()
    log.info("Update checker started (mode=%s, interval=%ds)", mode, check_interval)


def get_pending_version() -> Optional[str]:
    """Return the pending version string if an update is ready, else None."""
    if _updater is None:
        return None
    return _updater.pending_version


def get_update_banner_text() -> Optional[str]:
    """Return formatted update banner text, or None if no update pending."""
    pv = get_pending_version()
    if not pv:
        return None
    return f'<font color="orange">⬆ v{pv} 已就绪，/restart 部署</font>'


def check_and_update() -> dict:
    """Manually trigger a check+update cycle. Returns status dict.

    Returns:
        {"status": "updated", "version": "..."} |
        {"status": "up_to_date", "version": "..."} |
        {"status": "error", "message": "..."}
    """
    if _updater is None:
        return {"status": "error", "message": "Update checker not initialized"}
    return _updater.check_and_update()


# ---------------------------------------------------------------------------
# CalVer comparison
# ---------------------------------------------------------------------------


def _parse_calver(ver: str) -> tuple[int, ...]:
    """Parse CalVer string like '2026.03.24.1' into comparable tuple."""
    return tuple(int(x) for x in ver.split("."))


# ---------------------------------------------------------------------------
# UpdateChecker
# ---------------------------------------------------------------------------


class UpdateChecker:
    """Background thread that checks for and pulls feishu-bridge updates."""

    def __init__(self, mode: str, source_path: Optional[str],
                 check_interval: int):
        self.mode = mode  # "git" or "pypi"
        self.source_path = source_path
        self.check_interval = check_interval
        self.pending_version: Optional[str] = None
        self._lock = threading.Lock()          # guards pending_version
        self._check_lock = threading.Lock()    # serializes the check/upgrade run

    def start(self):
        t = threading.Thread(target=self._loop, daemon=True,
                             name="update-checker")
        t.start()

    def _loop(self):
        # Check immediately on startup (small delay to let main init finish)
        time.sleep(5)
        self._safe_check()
        while True:
            time.sleep(self.check_interval)
            self._safe_check()

    def _safe_check(self):
        try:
            self.check_and_update()
        except Exception:
            log.exception("Update check failed")

    def check_and_update(self) -> dict:
        """Detect new version, pull if available, set pending_version.

        Result contract: ``status`` ∈ {up_to_date, updated, error} (back-compat)
        plus ``action`` ∈ {none, restart_only, upgrade_and_restart}. The whole
        check/upgrade runs under ``_check_lock`` so a background _loop() check and
        a manual /update can't launch concurrent pipx upgrades.
        """
        with self._check_lock:
            with self._lock:
                pv = self.pending_version
            if pv is not None:
                # A prior check already decided a restart applies; just restart.
                return {"status": "updated", "action": "restart_only",
                        "version": pv}

            from feishu_bridge import __version__

            if self.mode == "git":
                return self._check_git(__version__)
            return self._check_pypi(__version__)

    # -- git mode ----------------------------------------------------------

    def _check_git(self, current_version: str) -> dict:
        """Fetch origin, compare, pull if ahead."""
        if not self.source_path:
            return {"status": "error", "message": "No source path for git mode"}

        # Fetch latest from origin
        r = subprocess.run(
            ["git", "fetch", "--quiet"],
            cwd=self.source_path, capture_output=True, timeout=30,
        )
        if r.returncode != 0:
            msg = r.stderr.decode(errors="replace").strip()
            log.warning("git fetch failed: %s", msg)
            return {"status": "error", "message": f"git fetch failed: {msg}"}

        # Check how many commits ahead upstream is
        r = subprocess.run(
            ["git", "rev-list", "HEAD..@{upstream}", "--count"],
            cwd=self.source_path, capture_output=True, timeout=10,
        )
        if r.returncode != 0:
            # Fallback: try origin/HEAD (works even without tracking branch)
            r = subprocess.run(
                ["git", "rev-list", "HEAD..origin/HEAD", "--count"],
                cwd=self.source_path, capture_output=True, timeout=10,
            )
        if r.returncode != 0:
            return {"status": "error", "message": "git rev-list failed"}

        ahead = int(r.stdout.decode().strip())
        if ahead == 0:
            log.info("Update check: up to date (git, v%s)", current_version)
            return {"status": "up_to_date", "action": "none",
                    "version": current_version}

        log.info("Update check: %d new commit(s) available, pulling...", ahead)

        # Pull (fast-forward only)
        r = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=self.source_path, capture_output=True, timeout=60,
        )
        if r.returncode != 0:
            msg = r.stderr.decode(errors="replace").strip()
            log.warning("git pull --ff-only failed: %s", msg)
            return {"status": "error", "message": f"git pull failed: {msg}"}

        # Read new version from __init__.py on disk
        new_version = self._read_git_version()
        if new_version and new_version != current_version:
            with self._lock:
                self.pending_version = new_version
            log.info("Update pulled: v%s → v%s (pending restart)",
                     current_version, new_version)
            return {"status": "updated", "action": "upgrade_and_restart",
                    "version": new_version}

        # Commits pulled but version unchanged (no version bump in commits)
        with self._lock:
            self.pending_version = f"{current_version}+{ahead}"
        log.info("Update pulled: %d commit(s), version unchanged v%s",
                 ahead, current_version)
        return {"status": "updated", "action": "upgrade_and_restart",
                "version": self.pending_version}

    def _read_git_version(self) -> Optional[str]:
        """Read __version__ from the on-disk __init__.py."""
        try:
            init_path = f"{self.source_path}/feishu_bridge/__init__.py"
            with open(init_path) as f:
                for line in f:
                    if line.startswith("__version__"):
                        return line.split('"')[1]
        except Exception:
            log.warning("Failed to read version from disk", exc_info=True)
        return None

    # -- pypi mode ---------------------------------------------------------

    def _check_pypi(self, current_version: str) -> dict:
        """Query PyPI for latest version, upgrade via pipx if newer."""
        import requests

        try:
            resp = requests.get(
                "https://pypi.org/pypi/feishu-bridge/json",
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            latest = data["info"]["version"]
        except Exception as e:
            log.warning("PyPI version check failed: %s", e)
            return {"status": "error", "message": f"PyPI check failed: {e}"}

        # Compare PyPI latest against the ON-DISK installed version, not the
        # in-memory __version__: after a pipx upgrade lands, the running process
        # is stale but disk is current — comparing in-memory would re-trigger an
        # endless re-upgrade loop.
        installed = _installed_version() or current_version

        if _parse_calver(latest) <= _parse_calver(installed):
            # Disk is already at/ahead of PyPI. If the RUNNING process is older
            # than what's on disk, the remedy is a restart, NOT another upgrade.
            if _parse_calver(installed) > _parse_calver(current_version):
                with self._lock:
                    self.pending_version = installed
                log.info("Update: disk v%s ahead of running v%s — restart "
                         "pending (no upgrade needed)", installed, current_version)
                return {"status": "updated", "action": "restart_only",
                        "version": installed}
            log.info("Update check: up to date (pypi, v%s)", installed)
            return {"status": "up_to_date", "action": "none", "version": installed}

        log.info("Update check: v%s available (installed: v%s), upgrading...",
                 latest, installed)

        # Pre-clean pip tombstones so a prior interrupted upgrade doesn't make
        # this --force-reinstall abort on a missing-file OSError.
        _purge_pip_ghost_dists()

        # Run pipx upgrade
        r = subprocess.run(
            ["pipx", "upgrade", "feishu-bridge"],
            capture_output=True, timeout=120,
        )
        if r.returncode != 0:
            msg = (r.stderr.decode(errors="replace").strip()
                   or r.stdout.decode(errors="replace").strip())
            log.warning("pipx upgrade failed: %s", msg)
            return {"status": "error", "action": "none",
                    "message": f"pipx upgrade failed: {msg}"}

        # Post-upgrade verification: a pipx exit code of 0 is NOT proof the new
        # version landed (a partial / ghost-corrupted reinstall can report
        # success yet leave disk stale). Re-read disk; only declare success and
        # arm the restart if the installed version actually advanced to latest.
        new_installed = _installed_version()
        if new_installed is None or _parse_calver(new_installed) < _parse_calver(latest):
            log.warning("pipx upgrade reported success but installed version is "
                        "still v%s (expected v%s)", new_installed, latest)
            return {"status": "error", "action": "none",
                    "message": (f"升级后校验失败：磁盘仍为 v{new_installed}"
                                f"（预期 v{latest}）")}

        with self._lock:
            self.pending_version = new_installed
        log.info("Update installed: v%s → v%s (pending restart)",
                 current_version, new_installed)
        return {"status": "updated", "action": "upgrade_and_restart",
                "version": new_installed}
