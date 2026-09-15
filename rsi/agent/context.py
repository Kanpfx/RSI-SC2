from rsi.tools.edit import Editor


def parent_context(root, parent, archive, failures):
    return {
        "entrypoint": "bot/main.py",
        "files": Editor(root).search(),
        "parent": parent,
        "lineage": archive.lineage(parent),
        "siblings": [
            {key: node[key] for key in ("direction", "wins", "losses", "ties", "crashes")}
            for node in archive.nodes if node["parent_id"] == parent["id"]],
        "failed_attempts": [item for item in failures if item["parent_id"] == parent["id"]],
    }
