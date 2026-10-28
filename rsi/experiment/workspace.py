"""Short runtime paths and explicitly owned temporary Editor workspaces."""
import shutil
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from runner.artifacts import path_reference, write_json


def allocate(root, kind, *, name=None):
    """Choose a readable unused path; timestamps use local time to the second."""
    directory = Path(root) / kind
    directory.mkdir(parents=True, exist_ok=True)
    if name is None and kind in {"experiments", "runtimes"}:
        name = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = {"work": "candidate", "batches": "batch"}.get(kind, kind.rstrip("s"))
    index = 1
    while True:
        leaf = name if name is not None and index == 1 else f"{name or prefix}_{index:02d}"
        path = directory / leaf
        if not path.exists():
            return path
        index += 1


def remove_workspace(root, directory):
    """Remove only a direct, non-linked child of this runtime's work directory."""
    directory = Path(directory)
    parent = (Path(root) / "work").resolve()
    if directory.is_symlink() or directory.is_junction() or directory.resolve().parent != parent:
        raise ValueError("Refusing to remove a workspace outside runtime/work")
    shutil.rmtree(directory)


def preflight_storage(root):
    """Exercise the SDK's actual event-file writer before matches or model requests."""
    from openhands.sdk.io.local import LocalFileStore
    directory = allocate(root, "sessions")
    event = "conversation/" + "0" * 32 + "/events/event-00000-" + str(uuid4()) + ".json"
    try:
        path = directory / event
        if sys.platform == "win32" and len(str(path.resolve())) >= 260:
            raise ValueError("RSI session path is too long; use a shorter project path")
        store = LocalFileStore(str(directory))
        store.write(event, "{}")
        if store.read(event) != "{}":
            raise OSError("SDK event storage check failed")
        work = allocate(root, "work")
        work.mkdir()
        try:
            write_json(work / "workspace.json", dict(status="probe"))
        finally:
            remove_workspace(root, work)
    finally:
        if directory.exists():
            shutil.rmtree(directory)


def create_workspace(versions, parent, iteration, task_id, *, root, name):
    """Copy one parent and record ownership outside the candidate's source tree."""
    directory = allocate(root, "work", name=name)
    directory.mkdir()
    write_json(directory / "workspace.json", dict(iteration=path_reference(iteration), task_id=task_id,
                                                 parent=parent["version"]))
    return versions.candidate(parent, directory / "code")
