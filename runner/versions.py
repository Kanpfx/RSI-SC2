"""Content-addressed Bot snapshots and isolated candidate workspaces."""
import difflib
import hashlib
import shutil
from pathlib import Path

from .artifacts import path_reference, project_path


def source_paths(root):
    """Enumerate retained files without reading contents; reject links and caches."""
    # iterdir propagates access errors; rglob can silently return an empty inventory.
    for path in sorted(Path(root).iterdir()):
        if path.is_symlink():
            raise ValueError(f"Source links are unsupported: {path}")
        if path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_dir():
            yield from source_paths(path)
        elif path.is_file():
            yield path


def inventory(root):
    """Read retained file contents for hashing, validation and diffs."""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in source_paths(root)}


def source_hash(root):
    """Hash every retained snapshot file, including names and boundaries."""
    return hash_files(inventory(root))


def hash_files(files):
    """Hash sorted relative paths and byte contents using the existing version format."""
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode() + b"\0" + content + b"\0")
    return digest.hexdigest()


def write_diff(before, after, changed, output):
    """Write a patch from the inventories already read for static validation."""
    lines = []
    for name in changed:
        old, new = (files.get(name, b"").decode("utf-8", errors="replace")
                    .replace("\r\n", "\n").replace("\r", "\n").splitlines(True) for files in (before, after))
        lines.extend(difflib.unified_diff(
            old, new, fromfile="a/" + name, tofile="b/" + name))
    Path(output).write_text("".join(lines), encoding="utf-8", newline="\n")


class Versions:
    def __init__(self, project, root):
        """Bind the project and the isolated RSI artifact directory."""
        self.project, self.root = Path(project).resolve(), Path(root).resolve()

    def freeze(self, source):
        """Freeze Bot files without importing them; verify existing snapshots before reuse."""
        files = bot_inventory(source)
        version = hash_files(files)
        target = self.root / "versions" / version
        if not target.exists():
            for name, data in files.items():
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        record = dict(version=version, source_root=path_reference(target))
        self.verify(record)
        return record

    def verify(self, version):
        """Reject modified snapshots before reading, editing or running them."""
        verify_version(version)

    def candidate(self, parent, directory):
        """Copy one verified parent into a fresh writable candidate directory."""
        self.verify(parent)
        shutil.copytree(project_path(parent["source_root"]), directory)
        return Path(directory)


def bot_inventory(source):
    """Collect the Bot tree with canonical project-relative names."""
    files = inventory(Path(source) / "bot")
    if "entry.py" not in files:
        raise ValueError("Snapshot requires bot/entry.py")
    return {"bot/" + name: data for name, data in files.items()}


def verify_version(version):
    """Verify all retained snapshot files against the declared content hash."""
    if source_hash(project_path(version["source_root"])) != version["version"]:
        raise ValueError("Snapshot content does not match its version")
