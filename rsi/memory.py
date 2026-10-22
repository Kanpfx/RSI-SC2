"""File-backed experience; hypotheses are never promoted to facts by storage."""
from pathlib import Path
from .tools.versions import read_json, write_json


def record(root, report):
    """Store compact experience and artifact paths, without nesting past investigations."""
    if report.get("purpose") == "holdout":
        raise ValueError("Holdout data cannot enter development memory")
    item = {key: report.get(key) for key in
            ("iteration_id", "parent", "status", "reason", "conditions", "changed_files")}
    diagnosis = report.get("diagnosis", {})
    conclusion = diagnosis.get("conclusion", {})
    item["issue"] = diagnosis.get("issue")
    item["proposal"] = {key: conclusion.get(key) for key in
                        ("hypothesis_id", "change", "preserve", "code_targets", "expected_effect", "risks", "uncertainty")}
    item["hypotheses"] = [{key: h.get(key) for key in ("id", "explanation", "status", "unknowns")}
                          for h in diagnosis.get("hypotheses", [])]
    validation, evaluation = report.get("validation", {}), report.get("evaluation", {})
    item["validation"] = dict(passed=validation.get("passed"),
                              checks=[dict(name=c["name"], passed=c["passed"]) for c in validation.get("checks", [])])
    item["evaluation"] = {key: evaluation.get(key) for key in ("decision", "reason", "comparison")}
    item["evaluation"]["effects"] = [{key: effect.get(key) for key in ("effect", "difference", "result")}
                                      for effect in evaluation.get("expected_effects", [])]
    item["artifacts"] = dict(report.get("artifacts", {}))
    item["interpretation"] = "Validation, local effects and formal acceptance are separate; diagnosis remains a hypothesis."
    write_json(Path(root) / "memory" / f"{report['iteration_id']}.json", item)


def search(root, conditions=None, text="", limit=5):
    """Find recent experiences matching exact conditions and optional text."""
    import json
    matches = []
    paths = sorted((Path(root) / "memory").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        item = read_json(path)
        if all(item.get("conditions", {}).get(k) == v for k, v in (conditions or {}).items()):
            if not text or text.casefold() in json.dumps(item, ensure_ascii=False).casefold():
                matches.append(dict(item, source_ref=f"memory:{path.stem}"))
        if len(matches) >= limit:
            break
    return matches
