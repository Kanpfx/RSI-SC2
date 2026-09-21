"""Best-effort version discovery and snapshots of loaded research inputs."""
import hashlib
import json
import platform
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


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
    root = Path(__file__).resolve().parents[2]
    packages = {}
    for name in ("burnysc2", "map-analyzer", "cython-extensions-sc2", "numpy"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {"python_version": platform.python_version(), "packages": packages,
            "code": git_state(root), "ares": git_state(root / "ares-sc2"),
            "sc2_version": None, "random_seed": None}


def snapshot_sources(directory, sources):
    hashes = {}
    for name, content in sources.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        path.write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode("utf-8")).hexdigest()
    return {"sha256": digest, "files": hashes}
