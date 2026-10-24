"""File-backed experience; hypotheses are never promoted to facts by storage."""
import json
from pathlib import Path

from .tools.artifacts import read_json, write_json


def record(root, report):
    """Store compact experience and artifact paths, without nesting past investigations."""
    if report.get("purpose") == "holdout":
        raise ValueError("Holdout data cannot enter development memory")
    item = {key: report.get(key) for key in
            ("iteration_id", "status", "reason", "conditions", "changed_files")}
    item["parent"] = (report.get("parent") or {}).get("version")
    diagnosis = report.get("diagnosis", {})
    item["issue"] = diagnosis.get("issue")
    item["conclusion"] = diagnosis.get("conclusion")
    validation, evaluation = report.get("validation", {}), report.get("evaluation", {})
    item["validation"] = dict(passed=validation.get("passed"))
    item["evaluation"] = {key: evaluation.get(key) for key in ("decision", "reason")}
    item["artifacts"] = {key: value for key, value in report.get("artifacts", {}).items()
                         if key in {"diagnosis", "patch", "evaluation"} or key.startswith("validation_")}
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
