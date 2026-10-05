"""Collect runtime versions and repository state for match metadata."""

import platform
import subprocess
from importlib.metadata import PackageNotFoundError, version
from agent.paths import PROJECT_ROOT


def git_state(root):
    def git(*args):
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *args], capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=3,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    status = git("status", "--porcelain", "--untracked-files=normal")
    return {"commit": git("rev-parse", "HEAD"),
            "dirty": bool(status) if status is not None else None}


def runtime_manifest():
    root = PROJECT_ROOT
    packages = {}
    for name in ("burnysc2", "map-analyzer", "cython-extensions-sc2", "numpy"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {"python_version": platform.python_version(), "packages": packages,
            "code": git_state(root), "ares": git_state(root / "ares-sc2"),
            "sc2_version": None, "random_seed": None}
