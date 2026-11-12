"""File-backed experience; hypotheses are never promoted to facts by storage."""
from pathlib import Path

from runtime.workspace.storage import read_json, write_json


def record(root, report):
    """Store compact experience and artifact paths, without nesting past investigations."""
    item = {key: report.get(key) for key in
            ("iteration_id", "status", "reason", "conditions")}
    item["parent"] = (report.get("parent") or {}).get("version")
    item["issue"] = report.get("problem")
    item["diagnosis"] = report.get("diagnosis")
    if "branches" in report:
        item["branches"] = [{key: branch[key] for key in
                             ("task_id", "status", "reason", "validation_passed", "scores", "selection_weight", "patch", "batch")
                             if key in branch}
                            for branch in report["branches"]]
        item["changed_files"] = sorted({name for branch in report["branches"]
                                         for name in branch.get("changed_files", [])})
    item["artifacts"] = {key: value for key, value in report.get("artifacts", {}).items()
                         if (key in {"problem", "hypotheses", "diagnosis", "resolution", "research"}
                             or key.startswith("validation_"))}
    write_json(Path(root) / "memory" / f"{report['iteration_id']}.json", item)


def search(root, conditions, limit=5):
    """Find recent experiences matching the current match conditions."""
    matches = []
    paths = sorted((Path(root) / "memory").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        item = read_json(path)
        if all(item.get("conditions", {}).get(k) == v for k, v in conditions.items()):
            matches.append(item)
        if len(matches) >= limit:
            break
    return matches
