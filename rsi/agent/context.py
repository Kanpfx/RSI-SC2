from rsi.tools.edit import Editor


def parent_context(root, node, archive):
    editor = Editor(root)
    sources = {}
    for file in sorted((editor.root / "bot").rglob("*.py")):
        name = file.relative_to(editor.root).as_posix()
        sources[name] = editor.view_file(name)
    lineage = []
    current = node
    by_id = {item["id"]: item for item in archive.nodes}
    while current:
        lineage.append({key: current[key] for key in ("id", "parent_id", "direction", "wins", "crashes")})
        current = by_id.get(current["parent_id"])
    return {"parent": node, "lineage": list(reversed(lineage)), "sources": sources}
