from rsi.evolution.state import read_json, save_json


class Archive:
    def __init__(self, path):
        self.path = path
        self.nodes = read_json(path) if path.exists() else []

    def save(self):
        save_json(self.path, self.nodes)

    def add(self, node):
        if any(item["id"] == node["id"] for item in self.nodes):
            raise ValueError(f"Duplicate node: {node['id']}")
        if node["games"] != 5 or sum(node[key] for key in ("wins", "losses", "ties", "crashes")) != 5:
            raise ValueError("Archive requires exactly five evaluated games")
        self.nodes.append(node)
        self.save()

    def mark_expanded(self, node):
        node["expanded"] = True
        self.save()
