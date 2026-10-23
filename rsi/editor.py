"""Candidate-only code intervention; diagnosis and acceptance stay outside the Editor."""
import json
import os
from pathlib import Path
import sys

from .retriever import Retriever
from .tools.versions import editable, path_reference, project_path, write_json


def make_job(candidate, diagnosis, feedback=None, context=None):
    """Pass the proposal and hypotheses with query records available for selective checks."""
    candidate = project_path(candidate)

    def workspace_path(path):
        """Give the Editor paths relative to its terminal and file-editor workspace."""
        path = project_path(path)
        try:
            return Path(os.path.relpath(path, candidate)).as_posix()
        except ValueError:
            return path.as_posix()

    conclusion = diagnosis["conclusion"]
    hypotheses = [{key: h[key] for key in ("id", "explanation", "predictions", "code_targets") if key in h}
                  for h in diagnosis.get("hypotheses", [])]
    evidence_path = candidate.parent / "editor_evidence.json"
    write_json(evidence_path, diagnosis["queries"])
    failures = None if feedback is None else dict(passed=feedback["passed"], checks=[
        {k: (v[-4000:] if k == "output" else v) for k, v in check.items()}
        for check in feedback["checks"] if not check["passed"]])
    prompt = """Implement this single diagnosis in the candidate workspace. Edit only Python files under
bot/components/behaviors/, bot/components/tasks/, bot/components/tactics/. Read other Bot files as needed.
Keep the diagnosis target and verification advice fixed. Do not change tests, core, spatial, telemetry or RSI.
Modules implement async execute(frame, explicit_business_inputs) -> bool and are called through frame.run.
Read native observations via frame.game; propose commands via frame.actions. True means some control intent
is handled, not arbitration or engine success. Do not issue Unit/BotAI commands or add manual tracking/logging.
Keep changes small and modular. Include concise English docstrings for new/changed functions.
The issue contains natural-language evidence descriptions. Read evidence_path selectively to check them;
it contains all recorded queries, their parameters and results. Avoid loading the whole file into context.
Use the proposal verification advice to choose relevant offline checks and report remaining uncertainty.
When a references index is provided, use read_reference to inspect API definitions and Knowledge entries.
source/ reads the current candidate's bot/ tree; api/ and knowledge/ read this iteration's frozen references.
Use lines=[start,end] for text, or key for JSON entries; omit key to list entry names.
These supporting files are read-only; do not change the diagnosis or verification advice.
Use the official file editor and terminal to inspect, edit, run offline checks, read failures and repair in this session.
The terminal starts in the candidate workspace; Python resolves candidate modules through PYTHONPATH.
All relative paths in the task are relative to this candidate workspace.
Use the provided Python interpreter. Run relevant imports and the protected Bot regression tests with:
python -B -m unittest discover -s <tests_directory> -p test_bot.py -v
Use platform-appropriate quoting; Windows uses PowerShell. Keep tests and all files outside the candidate unchanged.
Do not install dependencies or start SC2, model calls or RSI iterations. External validation and fixed matches still run afterward.
"""
    return dict(role="editor", workspace=path_reference(candidate), context=path_reference(context) if context else None,
        prompt=prompt + "\n" + json.dumps(
        dict(python=Path(sys.executable).as_posix(), tests_directory=workspace_path(Path(__file__).resolve().parents[1] / "scripts/test"),
             issue=diagnosis.get("issue"), hypotheses=hypotheses, proposal=conclusion,
             evidence_path=workspace_path(evidence_path), reference_directory=workspace_path(context) if context else None,
             references=Retriever(dict(source_root=candidate), context).index() if context else None,
             validator_feedback=failures), ensure_ascii=False))


def candidate_editor(workspace):
    """Wrap the official file editor with the exact candidate write boundary."""
    from openhands.sdk.tool import ToolExecutor
    from openhands.tools.file_editor import FileEditorTool, FileEditorObservation
    root = Path(workspace).resolve()

    class Executor(ToolExecutor):
        def __init__(self, delegate):
            """Keep the SDK's standard editing implementation."""
            self.delegate = delegate

        def __call__(self, action, conversation=None):
            """Permit views inside the candidate and writes only to editable components."""
            path = (root / action.path).resolve()
            if not path.is_relative_to(root) or (action.command != "view" and not editable(path.relative_to(root).as_posix())):
                return FileEditorObservation.from_text(command=action.command, path=str(path),
                    text="Path exceeds candidate component scope", is_error=True)
            return self.delegate(action.model_copy(update={"path": str(path)}), conversation)

    class Editor(FileEditorTool):
        name = FileEditorTool.name

        @classmethod
        def create(cls, conv_state, **params):
            """Construct the official editor with a scoped executor."""
            return [tool.model_copy(update={"executor": Executor(tool.executor)})
                    for tool in super().create(conv_state)]

    return Editor
