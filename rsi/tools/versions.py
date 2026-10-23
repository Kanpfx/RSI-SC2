"""Content-addressed Bot snapshots and isolated candidate workspaces."""
import difflib
import hashlib
import json
from pathlib import Path
import shutil

PROJECT = Path(__file__).resolve().parents[2]


def project_path(path):
    """Resolve stored project-relative paths independently of the working directory."""
    return (PROJECT / path).resolve()


def path_reference(path):
    """Store project files as relative POSIX paths; retain external absolute paths."""
    path = Path(path).resolve()
    return path.relative_to(PROJECT).as_posix() if path.is_relative_to(PROJECT) else path.as_posix()


def read_json(path):
    """Read a UTF-8 JSON artifact."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    """Atomically replace a complete JSON artifact."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def editable(name):
    """Allow only Python business components; reject ambiguous relative paths."""
    return (name.startswith(tuple(f"bot/components/{part}/" for part in
                                 ("behaviors", "tasks", "tactics")))
            and name.endswith(".py") and ".." not in name.split("/") and "\\" not in name)


def source_paths(root):
    """Enumerate retained files without reading contents; reject links and caches."""
    for path in sorted(Path(root).rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Source links are unsupported: {path}")
        if "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"} and path.is_file():
            yield path


def inventory(root):
    """Read retained file contents for hashing, validation and diffs."""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in source_paths(root)}


def source_hash(root):
    """Hash every retained snapshot file, including names and boundaries."""
    digest = hashlib.sha256()
    for name, content in inventory(root).items():
        digest.update(name.encode() + b"\0" + content + b"\0")
    return digest.hexdigest()


class Versions:
    def __init__(self, project, root):
        """Bind the project and the isolated RSI artifact directory."""
        self.project, self.root = Path(project).resolve(), Path(root).resolve()

    def freeze(self, source):
        """Freeze Bot files without importing them; verify existing snapshots before reuse."""
        files = inventory(Path(source) / "bot")
        if "entry.py" not in files:
            raise ValueError("Snapshot requires bot/entry.py")
        digest = hashlib.sha256()
        for name, data in files.items():
            digest.update(("bot/" + name).encode() + b"\0" + data + b"\0")
        version = digest.hexdigest()
        target = self.root / "versions" / version
        if not target.exists():
            for name, data in files.items():
                path = target / "bot" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        record = dict(version=version, source_root=path_reference(target))
        self.verify(record)
        return record

    def verify(self, version):
        """Reject modified snapshots before reading, editing or running them."""
        if source_hash(project_path(version["source_root"])) != version["version"]:
            raise ValueError("Snapshot content does not match its version")

    def current(self):
        """Load the accepted parent, or snapshot the workspace on first use."""
        pointer = self.root / "current_version.json"
        record = read_json(pointer) if pointer.exists() else self.freeze(self.project)
        self.verify(record)
        return record

    def candidate(self, parent, directory):
        """Copy one verified parent into a fresh writable candidate directory."""
        self.verify(parent)
        shutil.copytree(project_path(parent["source_root"]), directory)
        return Path(directory)

    def diff(self, parent, candidate, output):
        """Save the complete candidate diff and return changed paths."""
        self.verify(parent)
        before, after = inventory(project_path(parent["source_root"])), inventory(candidate)
        changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
        lines = []
        for name in changed:
            lines.extend(difflib.unified_diff(
                before.get(name, b"").decode("utf-8", errors="replace").splitlines(True),
                after.get(name, b"").decode("utf-8", errors="replace").splitlines(True),
                fromfile="a/" + name, tofile="b/" + name))
        Path(output).write_text("".join(lines), encoding="utf-8")
        return changed

    def accept(self, candidate):
        """Move the accepted-version pointer only after evaluation accepts."""
        self.verify(candidate)
        write_json(self.root / "current_version.json", candidate)
