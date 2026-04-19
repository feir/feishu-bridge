from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def make_short_home(prefix: str = "fb-bg-it-") -> tuple[Path, Path]:
    root = Path(tempfile.mkdtemp(dir="/tmp", prefix=prefix))
    return root, root / ".feishu-bridge"


def cleanup_home(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)


def cli_env(home_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = str(home_root)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(REPO_ROOT) if not existing else f"{REPO_ROOT}{os.pathsep}{existing}"
    )
    return env


def run_cli(
    home_root: Path,
    args: list[str],
    *,
    timeout: float = 20.0,
    expect_ok: bool = True,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "feishu_bridge.cli", *args],
        cwd=str(REPO_ROOT),
        env=cli_env(home_root),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if expect_ok:
        assert proc.returncode == 0, (
            f"rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc


def run_cli_json(home_root: Path, args: list[str], *, timeout: float = 20.0) -> dict:
    return json.loads(run_cli(home_root, args, timeout=timeout).stdout)


def wait_until(pred, *, timeout: float = 5.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(interval)
    return pred()
