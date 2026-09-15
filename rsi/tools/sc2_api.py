"""Read installed burnysc2 definitions without importing or executing its code."""
import ast
import importlib.metadata
from rsi.tools import STRING, schema


TOOLS = [
    schema("lookup_sc2_api", "Read installed SC2 API signature, docs and source. "
           "Use a qualified symbol; try the method name alone for inherited methods",
           {"symbol": STRING}, ["symbol"], [{"symbol": "BotAI.build"}]),
]


MAX_SOURCE = 12000
MAX_DOCSTRING = 4000
MAX_MATCHES = 20


def definitions(body, prefix):
    for node in body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = f"{prefix}.{node.name}"
            yield name, node
            if isinstance(node, ast.ClassDef):
                yield from definitions(node.body, name)


def signature(node):
    if isinstance(node, ast.ClassDef):
        bases = [ast.unparse(base) for base in node.bases]
        bases.extend(ast.unparse(keyword) for keyword in node.keywords)
        return f"class {node.name}({', '.join(bases)}):"
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({ast.unparse(node.args)}){returns}:"


def lookup_sc2_api(symbol):
    if (not isinstance(symbol, str) or len(symbol) > 200
            or not all(part.isidentifier() for part in symbol.split("."))):
        return {"status": "invalid_query", "error": "Use a dotted symbol such as BotAI.build, or a name such as build"}
    try:
        distribution = importlib.metadata.distribution("burnysc2")
    except importlib.metadata.PackageNotFoundError:
        return {"status": "unavailable", "error": "burnysc2 is not installed in this Python environment"}
    root = distribution.locate_file("sc2").resolve()
    if not root.is_dir():
        return {"status": "unavailable", "error": "Installed sc2 source directory was not found"}
    matches = []
    for path in sorted(root.rglob("*.py")):
        if not path.resolve().is_relative_to(root):
            continue
        source = path.read_text(encoding="utf-8-sig")
        if symbol.rsplit(".", 1)[-1] not in source:
            continue
        relative = path.relative_to(root).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        module = ".".join(["sc2", *parts])
        tree = ast.parse(source, filename=str(path))
        for name, node in definitions(tree.body, module):
            if name != symbol and not name.endswith("." + symbol):
                continue
            entry = {"symbol": name, "module": module, "file": str(path),
                     "line": node.lineno, "signature": signature(node),
                     "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                     "async": isinstance(node, ast.AsyncFunctionDef)}
            matches.append((entry, node, source))
    result = {"query": symbol, "version": distribution.version}
    if not matches:
        return {**result, "status": "not_found",
                "hint": "Try the method name alone; inherited methods are listed under their defining class."}
    if len(matches) > 1:
        return {**result, "status": "ambiguous", "matches": [entry for entry, _, _ in matches[:MAX_MATCHES]],
                "total_matches": len(matches), "truncated": len(matches) > MAX_MATCHES}
    entry, node, source = matches[0]
    text = ast.get_source_segment(source, node) or ""
    docstring = ast.get_docstring(node) or ""
    return {**result, "status": "found", **entry,
            "decorators": [ast.unparse(item) for item in node.decorator_list],
            "docstring": docstring[:MAX_DOCSTRING], "docstring_truncated": len(docstring) > MAX_DOCSTRING,
            "source": text[:MAX_SOURCE], "source_truncated": len(text) > MAX_SOURCE}
