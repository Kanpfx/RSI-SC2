import json
from pathlib import Path


class Feedback:
    def __init__(self, directory=None):
        self.root = None if directory is None else Path(directory).resolve()
        self.files = {} if self.root is None else {
            "feedback/" + p.name: p for p in sorted(self.root.glob("*.json"))
            if p.name == "metadata.json" or p.name.startswith("game_")}
        if self.root is not None and (self.root.parent.parent / "tree.json").is_file():
            tree = json.loads((self.root.parent.parent / "tree.json").read_text(encoding="utf-8"))
            for item in tree["nodes"]:
                node_id = item["id"]
                if not isinstance(node_id, str) or not node_id.isalnum():
                    continue
                directory = self.root.parent / node_id
                for p in directory.glob("*"):
                    if p.name in ("node.json", "changes.patch") or (p.name.startswith("game_") and p.suffix == ".json"):
                        self.files[f"feedback/history/{node_id}/{p.name}"] = p
        self.metadata = None
        if self.root is not None:
            node_file = self.root / "node.json"
            if node_file.is_file():
                node = json.loads(node_file.read_text(encoding="utf-8"))
                self.metadata = json.dumps({key: value for key, value in node.items()
                                            if key != "agent"}, ensure_ascii=False, indent=2)

    def names(self):
        return sorted(set(self.files) | ({"feedback/metadata.json"} if self.metadata is not None else set()))

    def path(self, name):
        target = self.files[name]
        if target.is_symlink() or not target.resolve().is_relative_to(self.root.parent if name.startswith("feedback/history/") else self.root):
            raise ValueError("Symlinks/junctions are not allowed")
        return target

    def read(self, name):
        if name == "feedback/metadata.json" and self.metadata is not None:
            return self.metadata
        return self.path(name).read_text(encoding="utf-8")
