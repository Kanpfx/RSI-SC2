from rsi.evolution.state import read_json, save_json


class Archive:
    def __init__(self, path=None):
        self.path = path
        self.nodes = read_json(path) if path is not None and path.exists() else []

    def save(self):
        if self.path is not None:
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

    def ranked(self, unexpanded=False):
        return sorted(
            (node for node in self.nodes
             if node["crashes"] == 0 and (not unexpanded or not node["expanded"])),
            key=lambda node: (-node["wins"], node["generation"], node["created_order"]),
        )

    def select_parent(self, rng, exploration_rate=0.2):
        candidates = self.ranked(unexpanded=True)
        if not candidates:
            return None
        if rng.random() >= exploration_rate:
            candidates = [node for node in candidates if node["wins"] == candidates[0]["wins"]]
        return rng.choice(candidates)

    def lineage(self, node):
        by_id = {item["id"]: item for item in self.nodes}
        history = []
        while node:
            history.append({key: node[key] for key in
                            ("id", "parent_id", "direction", "wins", "crashes")})
            node = by_id.get(node["parent_id"])
        return list(reversed(history))
