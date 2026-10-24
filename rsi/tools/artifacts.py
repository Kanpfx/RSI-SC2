"""Project-relative artifact paths and atomic JSON persistence."""
import json
from pathlib import Path


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
