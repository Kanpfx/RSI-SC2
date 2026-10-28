"""Freeze reference inputs and read scoped source/API/Knowledge files."""
import importlib.util
import json
import shutil
import sys
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from runner.artifacts import project_path
from runner.versions import hash_files, inventory


def freeze_context(project, directory):
    """Freeze references in a verified content-addressed snapshot."""
    directory = Path(directory)
    files = {"knowledge/" + name: data for name, data in inventory(Path(project) / "knowledge").items()}
    spec = importlib.util.find_spec("sc2")
    if spec and spec.origin:
        api_root = Path(spec.origin).parent
        for path in api_root.rglob("*.py"):
            files["api/" + path.relative_to(api_root).as_posix()] = path.read_bytes()
    files["reference.json"] = json.dumps(dict(burnysc2=version("burnysc2")), sort_keys=True).encode("utf-8")

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


class References:
    def __init__(self, source_root, context, max_chars=16000):
        """Bind explicit source and frozen reference roots without a version identity."""
        context = project_path(context)
        self.roots = dict(source=project_path(source_root) / "bot",
                          knowledge=context / "knowledge", api=context / "api")
        self.max_chars = max_chars

    def read_reference(self, path, lines=None, key=None):
        """Read frozen text through the SDK or retrieve a JSON entry by key."""
        kind, _, relative = path.partition("/")
        if kind not in self.roots:
            raise ValueError("Reference paths start with source/, api/ or knowledge/")
        root = self.roots[kind].resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file() or target.suffix not in {".py", ".md", ".json"}:
            raise ValueError("Reference path exceeds scope or is unavailable")
        if target.suffix == ".json":
            if lines is not None:
                raise ValueError("Use key for JSON entries; lines is for text files")
            return self._read_json_reference(path, target.read_bytes(), key)
        if key is not None:
            raise ValueError("Use lines for text files; key is for JSON entries")
        return self._read_text_reference(path, target, lines)

    def _read_json_reference(self, path, raw, key):
        """List JSON entry names or read one dot-separated entry."""
        value = json.loads(raw)
        if key is None:
            item = dict(path=path, keys=list(value))
        else:
            for part in key.split("."):
                value = value[part]
            item = dict(path=path, key=key, value=value)
        return self._reference_result(item)

    def _read_text_reference(self, path, target, lines):
        """Read at most 120 lines with the SDK file reader."""
        lines = [1, 120] if lines is None else lines
        if len(lines) != 2 or not all(isinstance(n, int) for n in lines) or not 1 <= lines[0] <= lines[1]:
            raise ValueError("lines must be [start_line, end_line], starting at 1")
        start, end = lines[0], min(lines[1], lines[0] + 119)
        from openhands.tools.file_editor.editor import FileEditor
        from openhands.tools.file_editor.exceptions import ToolError
        try:
            source = FileEditor().read_file(target, start_line=start, end_line=end + 1).splitlines()
        except ToolError as exc:
            raise ValueError(str(exc)) from exc
        count = end - start + 1
        clipped = len(source) > count
        if not source:
            return dict(path=path, reason="No text in the requested line range")
        end = start + min(len(source), count) - 1
        item = dict(path=path, start_line=start, end_line=end, source="\n".join(source[:count]))
        if clipped:
            item["next_line"] = end + 1
        return self._reference_result(item)

    def _reference_result(self, item):
        """Keep a readable location when reference content exceeds the response limit."""
        if len(json.dumps(item, ensure_ascii=False)) <= self.max_chars:
            return item
        result = {key: item[key] for key in ("path", "key", "start_line", "end_line") if key in item}
        result.update(truncated=True, reason="Content exceeds output limit; request fewer lines or a smaller JSON key")
        return result

