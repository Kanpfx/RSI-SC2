"""Freeze API references; static knowledge adaptation is pending."""
import json
import shutil
import sys
import tomllib
from pathlib import Path
from uuid import uuid4

from runtime.workspace.storage import PROJECT
from runtime.workspace.versions import hash_files, inventory


def knowledge_files(project):
    """TODO: adapt the new knowledge format before exposing it to agents."""
    return {}


def freeze_context(project, directory):
    """Freeze API sources in a verified content-addressed snapshot."""
    directory = Path(directory)
    files = knowledge_files(project)
    api_root = PROJECT / "knowledge/python-sc2/sc2"
    if not api_root.is_dir():
        raise ValueError("Fixed API reference source is unavailable: knowledge/python-sc2/sc2")
    for path in api_root.rglob("*.py"):
        files["api/" + path.relative_to(api_root).as_posix()] = path.read_bytes()
    metadata = tomllib.loads((PROJECT / "knowledge/python-sc2/pyproject.toml").read_text(encoding="utf-8"))
    files["reference.json"] = json.dumps(dict(burnysc2=metadata["project"]["version"],
        api_source="knowledge/python-sc2/sc2"), sort_keys=True).encode("utf-8")

    def write(target):
        """Write only the bytes included in the reference identity."""
        for name, data in files.items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    identity = hash_files(files)
    target = directory / identity
    directory.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        staging = directory / (".ref-" + uuid4().hex)
        # Shared snapshots must inherit project ACLs, not tempfile's owner-only ACL.
        staging.mkdir()
        try:
            write(staging)
            try:
                staging.rename(target)
            except FileExistsError:
                pass  # Another experiment published the same immutable snapshot.
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    stored = inventory(target)
    actual = hash_files(stored)
    if actual != identity:
        missing = sorted(files.keys() - stored.keys())
        extra = sorted(stored.keys() - files.keys())
        changed = sorted(name for name in files.keys() & stored.keys() if files[name] != stored[name])
        raise ValueError("Reference snapshot content does not match its identity; "
                         f"path={target}; python={sys.executable}; expected={identity}; actual={actual}; "
                         f"missing={missing}; extra={extra}; changed={changed}")
    return target
