"""Candidate-only code intervention; diagnosis and acceptance stay outside the Editor."""
import json
from pathlib import Path
import sys

from .analyzer import compact_refs, refs as source_refs


def make_job(candidate, diagnosis, feedback=None):
    """Give the coding session its fixed target, cited evidence and optional repair errors."""
    conclusion = diagnosis["conclusion"]
    refs = set(conclusion["evidence_refs"])
    evidence, omitted_refs, size = [], set(), 0
    for query in diagnosis["queries"]:
        for item in query["result"]["items"]:
            cited = refs & set(source_refs(item))
            if not cited:
                continue
            item = compact_refs(item)
            if isinstance(item.get("source"), str) and len(item["source"]) > 6000:
                item = dict(item, source=item["source"][:6000], excerpt_truncated=True)
            length = len(json.dumps(item, ensure_ascii=False))
            if size + length > 16000:
                omitted_refs.update(cited)
                continue
            evidence.append(dict(channel=query["channel"], kind=query["kind"], item=item))
            size += length
    hypothesis = next((h for h in diagnosis.get("hypotheses", []) if h["id"] == conclusion.get("hypothesis_id")), None)
    if hypothesis:
        hypothesis = {k: v for k, v in hypothesis.items() if k != "updates"}
    failures = None if feedback is None else dict(passed=feedback["passed"], checks=[
        {k: (v[-4000:] if k == "output" else v) for k, v in check.items()}
        for check in feedback["checks"] if not check["passed"]])
    prompt = """Implement this single diagnosis in the candidate workspace. Edit only Python files under
bot/components/behaviors/, bot/components/tasks/, bot/components/tactics/. Read other Bot files as needed.
Keep the diagnosis target and expected effects fixed. Do not change tests, core, spatial, telemetry or RSI.
Modules implement async execute(frame, explicit_business_inputs) -> bool and are called through frame.run.
Read native observations via frame.game; propose commands via frame.actions. True means some control intent
is handled, not arbitration or engine success. Do not issue Unit/BotAI commands or add manual tracking/logging.
Keep changes small and modular. Include concise English docstrings for new/changed functions.
Use the official file editor and terminal to inspect, edit, run offline checks, read failures and repair in this session.
The terminal starts in the candidate workspace; Python resolves candidate modules through PYTHONPATH.
Use the provided Python interpreter. Run relevant imports and the protected Bot regression tests with:
python -B -m unittest discover -s <tests_directory> -p test_bot.py -v
Use platform-appropriate quoting; Windows uses PowerShell. Keep tests and all files outside the candidate unchanged.
Do not install dependencies or start SC2, model calls or RSI iterations. External validation and fixed matches still run afterward.
"""
    return dict(role="editor", workspace=str(candidate), prompt=prompt + "\n" + json.dumps(
        dict(python=sys.executable, tests_directory=str(Path(__file__).resolve().parents[1] / "scripts/test"),
             issue=diagnosis.get("issue"), hypothesis=hypothesis, proposal=conclusion, evidence=evidence,
             omitted_evidence_refs=sorted(omitted_refs), validator_feedback=failures), ensure_ascii=False))
