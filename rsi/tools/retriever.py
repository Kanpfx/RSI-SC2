"""Bounded retrieval of frozen source, installed API references, knowledge and memory."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

from .versions import project_path, source_paths, read_json, write_json


def freeze_context(project, directory, experiences):
    """Freeze reference inputs once so later queries retain stable provenance."""
    directory = Path(directory)
    shutil.copytree(Path(project) / "knowledge", directory / "knowledge")
    spec = importlib.util.find_spec("sc2")
    if spec and spec.origin:
        api_root = Path(spec.origin).parent
        for path in api_root.rglob("*.py"):
            target = directory / "api" / path.relative_to(api_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    write_json(directory / "memory.json", experiences)


class Retriever:
    def __init__(self, version, context, max_chars=16000):
        """Bind exactly one parent snapshot and frozen non-log context."""
        self.version, self.context, self.max_chars = version, project_path(context), max_chars

    def index(self):
        """Expose names only; reference catalogues are retrieved on demand."""
        roots = dict(source=project_path(self.version["source_root"]) / "bot",
                     knowledge=self.context / "knowledge", api=self.context / "api")
        return dict({kind: sorted(path.relative_to(root).as_posix() for path in source_paths(root))
                     for kind, root in roots.items()},
                    memory_count=len(read_json(self.context / "memory.json")))

    def query(self, kind, path="", symbol=None, start_line=1, text=""):
        """Read one source symbol, reference entry or bounded experience result."""
        clipped = False
        if kind == "memory":
            values = read_json(self.context / "memory.json")
            items = [dict(v, source_ref=f"memory:{v['iteration_id']}") for v in values
                     if not text or text.casefold() in json.dumps(v, ensure_ascii=False).casefold()]
        else:
            roots = dict(source=project_path(self.version["source_root"]) / "bot",
                         knowledge=self.context / "knowledge", api=self.context / "api")
            if kind not in roots:
                raise ValueError("Retriever kinds: source, api, knowledge, memory")
            root = roots[kind].resolve()
            target = (root / path).resolve()
            if not target.is_relative_to(root) or not target.is_file() or target.suffix not in {".py", ".md", ".json"}:
                raise ValueError("Reference path exceeds scope or is unavailable")
            raw = target.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            ref = f"{kind}:{digest}:{path}"
            if target.suffix == ".json":
                value = json.loads(raw)
                if not symbol:
                    items = [dict(path=path, keys=list(value), source_ref=ref)]
                else:
                    for key in symbol.split("."):
                        value = value[key]
                    items = [dict(path=path, symbol=symbol, value=value, source_ref=ref + ":" + symbol)]
            else:
                source = raw.decode("utf-8")
                lines = source.splitlines()
                if not isinstance(start_line, int) or start_line < 1:
                    raise ValueError("start_line must be positive")
                start, end = start_line, min(len(lines), start_line + 119)
                clipped = end < len(lines)
                if symbol and target.suffix == ".py":
                    found = next((node for name, node in symbols(ast.parse(source)) if name == symbol), None)
                    if found is None:
                        raise ValueError("Symbol unavailable")
                    start, end = found.lineno, min(found.end_lineno, found.lineno + 119)
                    clipped = end < found.end_lineno
                items = [dict(path=path, start_line=start, end_line=end,
                              source="\n".join(lines[start - 1:end]), source_ref=f"{ref}:{start}-{end}")]
        kept, size = [], 0
        for item in items:
            length = len(json.dumps(item, ensure_ascii=False))
            if size + length > self.max_chars:
                break
            kept.append(item)
            size += length
        return dict(items=kept, source_refs=[i["source_ref"] for i in kept],
                    coverage={"scope": "frozen references, not match observations"},
                    truncated=clipped or len(kept) < len(items), missing_reason=None if kept else "No match or item exceeds budget")


def symbols(node, prefix=""):
    """Yield qualified Python class/function names without importing candidate code."""
    for child in getattr(node, "body", []):
        if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = prefix + child.name
            yield name, child
            yield from symbols(child, name + ".")
