"""Official OpenHands basic tools and the single RSI sampling operation."""
import json
import os
from pathlib import Path
import sys

from openhands.sdk.tool import DeclaredResources, Observation, ToolDefinition, ToolExecutor
from openhands.tools.file_editor import FileEditorObservation, FileEditorTool

from runtime.workspace.checks import editable
from runtime.workspace.storage import path_reference, project_path, write_json


class CandidateExecutor(ToolExecutor):
    def __init__(self, delegate, workspace, allowed=None, analysis=None):
        self.delegate = delegate
        self.root = Path(workspace).resolve()
        self.allowed = allowed
        self.analysis = Path(analysis).resolve() if analysis else None

    def __call__(self, action, conversation=None):
        """Use the official editor with scoped writes and ordinary reference reads."""
        path = (self.root / action.path).resolve()
        permitted = self.analysis is not None and path.is_relative_to(self.analysis)
        if path.is_relative_to(self.root):
            name = path.relative_to(self.root).as_posix()
            permitted = permitted or (name in self.allowed if self.allowed is not None else editable(name))
        if action.command != "view" and not permitted:
            return FileEditorObservation.from_text(command=action.command, path=str(path),
                text="Path exceeds the allowed write scope", is_error=True)
        return self.delegate(action.model_copy(update={"path": str(path)}), conversation)

    def close(self):
        """Forward cleanup to the official executor once."""
        if self.delegate is not None:
            delegate, self.delegate = self.delegate, None
            delegate.close()


class CandidateEditor(FileEditorTool):
    name = FileEditorTool.name

    def declared_resources(self, action):
        """Lock the actual workspace path, including when the model uses relative paths."""
        path = (self.executor.root / action.path).resolve()
        return DeclaredResources(keys=(f"file:{path}",), declared=True)

    @classmethod
    def create(cls, conv_state, *, workspace, allowed=None, analysis=None):
        """Keep the official schema and editor implementation."""
        return [tool.model_copy(update={"executor": CandidateExecutor(
            tool.executor, workspace, allowed, analysis)})
            for tool in super().create(conv_state)]


class ResearchTerminalExecutor(ToolExecutor):
    def __init__(self, delegate, stage):
        self.delegate, self.stage = delegate, stage
        self.pending = None

    def __call__(self, action, conversation=None):
        """Count commands and retain results without replacing SDK execution."""
        from openhands.tools.terminal.definition import TerminalObservation
        fresh = bool(action.command.strip()) and not action.is_input
        if fresh:
            if len(self.stage.state["analyses"]) >= self.stage.job["max_analyses"]:
                return TerminalObservation.from_text(text="Analysis command budget exhausted",
                    command=action.command, exit_code=1, is_error=True)
            self.pending = dict(command=action.command, returncode=None)
            self.stage.state["analyses"].append(self.pending)
            self.pending["artifact"] = path_reference(self.stage.directory / "terminal" /
                f"command_{len(self.stage.state['analyses']):02d}.json")
            self.stage.save()
        result = self.delegate(action, conversation)
        if self.pending is not None:
            write_json(project_path(self.pending["artifact"]), result.model_dump(mode="json"))
            self.pending["returncode"] = None if result.exit_code == -1 else result.exit_code
            self.stage.save()
            if result.exit_code != -1:
                self.pending = None
        return result

    def close(self):
        """Let the SDK terminate its terminal resources."""
        if self.delegate is not None:
            delegate, self.delegate = self.delegate, None
            delegate.close()


def basic_tools(job, state, *, stage=None, allowed=None):
    """Attach official file, search and terminal tools with explicit workspace paths."""
    from openhands.tools.glob import GlobTool
    from openhands.tools.grep import GrepTool
    from openhands.tools.terminal import TerminalTool
    workspace = job.get("workspace") or state.workspace.working_dir
    module_root = job.get("workspace") or project_path(job["parent"]["source_root"])
    analysis = stage.directory / "analysis" if stage else None
    if analysis:
        analysis.mkdir(parents=True, exist_ok=True)
    definitions = CandidateEditor.create(state, workspace=workspace, allowed=allowed, analysis=analysis)
    definitions.extend(GlobTool.create(state))
    definitions.extend(GrepTool.create(state))
    terminals = TerminalTool.create(state, env=dict(
        PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
        PYTHONPATH=os.pathsep.join((str(Path(module_root).resolve()), str(Path(__file__).resolve().parents[2]))),
        PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"))
    for tool in terminals:
        if stage:
            tool = tool.model_copy(update={"executor": ResearchTerminalExecutor(tool.executor, stage)})
        definitions.append(tool)
    return definitions


class SamplingOutput(Observation):
    pass


class SamplingExecutor(ToolExecutor):
    def __init__(self, function):
        self.function = function

    def __call__(self, action, conversation=None):
        """Return correctable sampling errors as ordinary tool observations."""
        try:
            value = self.function(**action.model_dump(exclude_none=True,
                include=set(type(action).model_fields)))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            value = dict(error=str(exc))
        return SamplingOutput.from_text(json.dumps(value, ensure_ascii=False))

    def close(self):
        self.function = None


class RunSamplingTool(ToolDefinition):
    workspace: str

    def declared_resources(self, action):
        """Serialize requests against the shared sampling workspace and budget."""
        logger = (Path(self.workspace) / "bot/telemetry/logger.py").resolve()
        return DeclaredResources(keys=("rsi:sampling", f"file:{logger}"), declared=True)

    @classmethod
    def create(cls, conv_state=None, *, function, input_type, workspace):
        """Expose the RSI operation using the SDK tool lifecycle."""
        return [cls(description="Validate the edited collector and run sampling matches. "
            "Returns evidence paths and remaining allowance; continue analysis in this conversation. "
            "Use reobserve for new fields, repeat for independent seeds; optionally target ai_build.",
            action_type=input_type, observation_type=SamplingOutput, executor=SamplingExecutor(function),
            workspace=str(workspace))]
