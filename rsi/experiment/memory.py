"""File-backed experience; hypotheses are never promoted to facts by storage."""
import json
from pathlib import Path

from runner.artifacts import read_json, write_json


def record(root, report):
    """Store compact experience and artifact paths, without nesting past investigations."""
    if report.get("purpose") == "holdout":
        raise ValueError("Holdout data cannot enter development memory")
    item = {key: report.get(key) for key in
            ("iteration_id", "status", "reason", "conditions")}
    item["parent"] = (report.get("parent") or {}).get("version")
    diagnosis = report.get("diagnosis", {})
    item["issue"] = diagnosis.get("issue")
    item["conclusion"] = diagnosis.get("conclusion")
    if "branches" in report:
        item["branches"] = [{key: branch[key] for key in
                             ("task_id", "status", "reason", "validation_passed", "promoted", "patch", "evaluation", "promotion")
                             if key in branch}
                            for branch in report["branches"]]
        item["changed_files"] = sorted({name for branch in report["branches"]
                                         for name in branch.get("changed_files", [])})
    branch_paths = {branch[key] for branch in item.get("branches", [])
                    for key in ("patch", "evaluation", "promotion") if key in branch}
    item["artifacts"] = {key: value for key, value in report.get("artifacts", {}).items()
                         if (key == "diagnosis"
                             or key.startswith(("validation_", "evaluation_", "promotion_")))
                         and value not in branch_paths}
    write_json(Path(root) / "memory" / f"{report['iteration_id']}.json", item)


def search(root, conditions=None, text="", limit=5):
    """Find recent experiences matching exact conditions and optional text."""
    matches = []
    paths = sorted((Path(root) / "memory").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        item = read_json(path)
        if all(item.get("conditions", {}).get(k) == v for k, v in (conditions or {}).items()):
            if not text or text.casefold() in json.dumps(item, ensure_ascii=False).casefold():
                matches.append(item)
        if len(matches) >= limit:
            break
    return matches
