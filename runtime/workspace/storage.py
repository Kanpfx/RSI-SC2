"""Project-relative artifact paths and atomic JSON persistence."""
import json
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[2]


def project_path(path, *, root=PROJECT):
    """Resolve stored project-relative paths independently of the working directory."""
    return (Path(root) / path).resolve()


def path_reference(path, *, root=PROJECT):
    """Store project files as relative POSIX paths; retain external absolute paths."""
    path, root = Path(path).resolve(), Path(root).resolve()
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()


def read_json(path):
    """Read a UTF-8 JSON artifact."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    """Atomically replace a UTF-8 JSON artifact."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
