"""Reuse OpenHands SDK sessions with process, token and observed-cost budgets."""
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import traceback
from urllib.parse import urlsplit

from .matches import stop_process
from .versions import editable, read_json, write_json


def load_settings(project, settings):
    """Load standard LLM settings; credentials stay in the process environment."""
    from dotenv import dotenv_values
    values = dotenv_values(Path(project) / ".env")
    settings = dict(settings)
    for name, field in (("LLM_MODEL", "model"), ("LLM_BASE_URL", "base_url")):
        value = os.environ.get(name) or values.get(name)
        if value:
            settings[field] = value
    key_name = settings["api_key_env"]
    key = os.environ.get(key_name) or values.get(key_name)
    if key:
        os.environ[key_name] = key
    if urlsplit(settings.get("base_url") or "").hostname == "openrouter.ai" and settings.get("model"):
        if not settings["model"].startswith("openrouter/"):
            settings["model"] = "openrouter/" + settings["model"]
    return settings


def preflight(settings):
    """Fail before collecting games when an iterative run has no usable model settings."""
    if not settings.get("model") or not os.environ.get(settings["api_key_env"]):
        raise ValueError("Configure LLM_MODEL and LLM_API_KEY in .env")
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
    from openhands.sdk import Conversation  # noqa: F401
    from openhands.tools.file_editor import FileEditorTool  # noqa: F401
    from openhands.tools.terminal import TerminalTool  # noqa: F401


def call_session(project, job, directory):
    """Run one model conversation in a bounded worker and retain its complete artifacts."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "job.json", job)
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    with (directory / "stdout.txt").open("w", encoding="utf-8") as out, \
            (directory / "stderr.txt").open("w", encoding="utf-8") as err:
        process = subprocess.Popen([sys.executable, "-B", "-m", "rsi.tools.llm",
                                    str(directory / "job.json")], cwd=project,
                                   stdout=out, stderr=err, creationflags=flags)
        try:
            process.wait(timeout=job["model"]["wall_timeout"])
        except subprocess.TimeoutExpired:
            stop_process(process)
            return dict(status="error", reason="model_session_wall_timeout",
                        usage=read_json(directory / "usage.json") if (directory / "usage.json").exists() else {})
        except BaseException:
            stop_process(process)
            raise
    path = directory / "response.json"
    return read_json(path) if path.exists() else dict(status="error", reason="model_worker_missing_response", usage={})


def budgeted_llm_type(settings):
    """Reserve estimated input plus maximum output before sending each SDK request."""
    from openhands.sdk import LLM

    class BudgetedLLM(LLM):
        def check_request(self, messages, tools, security=False):
            """Reject requests that cannot fit the remaining session token allowance."""
            usage = self.metrics.accumulated_token_usage
            spent = usage.prompt_tokens + usage.completion_tokens if usage else 0
            estimate = self.get_token_count(messages, tools, security)
            required = math.ceil(estimate * 1.15) + 1024 + self.max_output_tokens
            if estimate <= 0 or spent + required > settings["max_total_tokens"]:
                raise ValueError("Next request cannot fit the token budget")
            if self.metrics.costs and self.metrics.accumulated_cost >= settings["max_cost"]:
                raise ValueError("Observed model cost budget reached")

        def completion(self, messages, tools=None, add_security_risk_prediction=False, **kwargs):
            """Check the budget before delegating to SDK completion transport."""
            self.check_request(messages, tools, add_security_risk_prediction)
            return super().completion(messages, tools, add_security_risk_prediction, **kwargs)

        def responses(self, messages, tools=None, **kwargs):
            """Apply the same budget to SDK Responses transport."""
            self.check_request(messages, tools, kwargs.get("add_security_risk_prediction", False))
            return super().responses(messages, tools, **kwargs)

    return BudgetedLLM


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


def investigation_tools(investigation):
    """Expose ordinary investigation functions as tools within one SDK conversation."""
    from typing import Any, Literal
    from pydantic import BaseModel, ConfigDict, Field
    from openhands.sdk.tool import Action, Observation, ToolDefinition, ToolExecutor, ToolAnnotations

    class Issue(BaseModel):
        model_config = ConfigDict(extra="forbid")
        symptom: str
        runs: list[str]
        window: tuple[float, float]
        why_selected: str

    class Hypothesis(BaseModel):
        model_config = ConfigDict(extra="forbid")
        id: str
        explanation: str
        predictions: dict[Literal["support", "refute"], str]
        code_targets: list[str] = Field(default_factory=list)

    class EvidenceUpdate(BaseModel):
        model_config = ConfigDict(extra="forbid")
        id: str
        reason: str
        support_refs: list[str] = Field(default_factory=list)
        against_refs: list[str] = Field(default_factory=list)
        unknowns: list[str] = Field(default_factory=list)
        status: Literal["open", "supported", "weakened", "refuted", "unresolved"] | None = None

    class Conclusion(BaseModel):
        model_config = ConfigDict(extra="forbid")
        status: Literal["repair_ready", "unresolved"]
        hypothesis_id: str | None = None
        change: str = ""
        preserve: str = ""
        code_targets: list[str] = Field(default_factory=list)
        evidence_refs: list[str] = Field(default_factory=list)
        expected_effect: list[dict[str, Any]] = Field(default_factory=list)
        risks: str = ""
        uncertainty: str = ""
        reason: str = ""

    class SelectInput(Action):
        model_config = ConfigDict(extra="forbid")
        issue: Issue
        hypotheses: list[Hypothesis]

    class QueryInput(Action):
        model_config = ConfigDict(extra="forbid")
        channel: Literal["analyzer", "retriever"]
        query_kind: Literal["overview", "metrics", "events", "trace", "compare", "source", "api", "knowledge", "memory"] = Field(alias="kind")
        params: dict[str, Any]
        purpose: str
        distinguishes: str
        updates: list[EvidenceUpdate] = Field(default_factory=list)

    class ConcludeInput(Action):
        model_config = ConfigDict(extra="forbid")
        conclusion: Conclusion
        updates: list[EvidenceUpdate] = Field(default_factory=list)

    class Output(Observation):
        pass

    definitions = []
    descriptions = {
        "select_issue": "Select one issue and competing hypotheses before investigation. Times are game seconds; source code targets start with bot/.",
        "investigate": "Query evidence and explain what distinguishes hypotheses. Updates require a reason; evidence references and unknowns accumulate. Review the previous valid result first. Analyzer params: batch_id,runs,time,detail=summary/raw,offset; metrics/compare add names, events adds types, trace adds module/call_id/request_id/status, compare adds reference_batch. Default summaries; raw retrieves samples or trace snapshots. Retriever params: path (source relative to bot/),symbol,start_line,text. JSON symbols use dot keys. Invalid queries may be corrected without hypothesis updates.",
        "conclude": "Conclude with repair_ready or unresolved. For repair_ready choose hypothesis_id, describe change and preserve behavior, cite evidence and include improvement/regression expected_effect checks. Each effect: kind=metric/event,name,statistic,time:[start,end],direction=increase/decrease/stable,role=improvement/regression, optional where for event details. State samples support mean/min/max/first/last; counters support count/first_time; events support count/first_time/duration. Use unresolved when evidence cannot distinguish causes.",
    }
    functions = dict(select_issue=investigation.update, investigate=investigation.query, conclude=investigation.conclude)
    inputs = dict(select_issue=SelectInput, investigate=QueryInput, conclude=ConcludeInput)
    for name, description in descriptions.items():
        def make_executor(function):
            """Bind each executor to its own investigation operation."""
            class Executor(ToolExecutor):
                def __call__(self, action, conversation=None):
                    """Return validation failures as tool observations for correction."""
                    try:
                        value = function(**action.model_dump(exclude_none=True, by_alias=True,
                                                             include=set(type(action).model_fields)))
                    except (ValueError, KeyError, TypeError, OSError) as exc:
                        value = dict(status="invalid_input", error=dict(code="invalid_input", message=str(exc)))
                    return Output.from_text(json.dumps(value, ensure_ascii=False))
            return Executor()

        def make_definition(tool_name, help_text, executor, input_type):
            """Bind SDK tool construction to one operation and its executor."""
            class Definition(ToolDefinition):
                @classmethod
                def create(cls, conv_state=None, **params):
                    """Construct the bound SDK-native tool definition."""
                    return [cls(description=help_text, action_type=input_type, observation_type=Output,
                                executor=executor, annotations=ToolAnnotations(
                                    readOnlyHint=True, destructiveHint=False, openWorldHint=False))]
            tool = type("Rsi" + tool_name.title().replace("_", ""), (Definition,), {"name": tool_name})
            return tool.create()[0]

        definitions.append(make_definition(name, description, make_executor(functions[name]), inputs[name]))
    return definitions


def execute(job_path, llm_factory=None):
    """Worker entry; model transport can be injected for offline SDK verification."""
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
    job_path = Path(job_path)
    job, directory = read_json(job_path), job_path.parent
    conversation = investigation = llm = None
    response = {}
    try:
        from pydantic import SecretStr
        from openhands.sdk import Agent, Conversation, Tool
        from openhands.sdk.tool import register_tool
        from openhands.sdk.conversation.response_utils import get_agent_final_response
        settings = job["model"]
        prompt = job["prompt"]
        if job["role"] == "diagnosis":
            from ..diagnosis.investigate import Investigation
            investigation = Investigation(job, directory)
            if job["diagnosis"]["query_policy"] == "fixed":
                prompt += "\nPredeclared evidence pack:\n" + json.dumps(investigation.fixed_pack(), ensure_ascii=False)
            definitions = investigation_tools(investigation)
        elif job["role"] == "editor":
            from openhands.tools.terminal import TerminalTool
            definitions = [candidate_editor(job["workspace"])]
            terminal = Tool(name=TerminalTool.name, params=dict(env=dict(
                PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
                PYTHONPATH=str(Path(job["workspace"]).resolve()), PYTHONDONTWRITEBYTECODE="1")))
        else:
            definitions = []
        selected = []
        for definition in definitions:
            register_tool(definition.name, definition)
            selected.append(Tool(name=definition.name))
        if job["role"] == "editor":
            selected.append(terminal)
        llm = llm_factory() if llm_factory else budgeted_llm_type(settings)(
            model=settings["model"], api_key=SecretStr(os.environ[settings["api_key_env"]]),
            base_url=settings.get("base_url"), timeout=settings["request_timeout"], num_retries=0,
            max_output_tokens=settings["max_output_tokens"], temperature=settings.get("temperature"), usage_id=job["role"])

        def record_usage(event=None):
            """Persist observed usage; unknown vendor pricing remains null."""
            usage = llm.metrics.accumulated_token_usage
            value = dict(tokens=usage.prompt_tokens + usage.completion_tokens if usage else None,
                         cost=llm.metrics.accumulated_cost if llm.metrics.costs else None)
            write_json(directory / "usage.json", value)
            if conversation and ((value["tokens"] or 0) >= settings["max_total_tokens"] or
                                 (value["cost"] or 0) >= settings["max_cost"]):
                conversation.pause()
            return value

        workspace = Path(job.get("workspace", directory / "readonly_session"))
        workspace.mkdir(parents=True, exist_ok=True)
        conversation = Conversation(agent=Agent(llm=llm, tools=selected), workspace=str(workspace),
            persistence_dir=str(directory / "conversation"), max_iteration_per_run=settings["max_iterations"],
            callbacks=[record_usage], visualizer=None)
        conversation.send_message(prompt)
        conversation.run()
        usage = record_usage()
        if (usage["tokens"] or 0) >= settings["max_total_tokens"] or (usage["cost"] or 0) >= settings["max_cost"]:
            raise ValueError("Model session budget reached")
        if investigation:
            value = investigation.state
            if not value["conclusion"]:
                value["conclusion"] = dict(status="unresolved", reason="Session ended without a validated conclusion")
                investigation.save()
        else:
            value = get_agent_final_response(list(conversation.state.events)).strip()
        response = dict(status="ok", value=value, usage=usage)
    except Exception:
        response = dict(status="error", reason=traceback.format_exc(),
                        usage=read_json(directory / "usage.json") if (directory / "usage.json").exists() else {})
    finally:
        if conversation:
            try:
                conversation.close()
            except Exception as exc:
                response["cleanup_error"] = str(exc)
        write_json(directory / "response.json", response)
    return response


if __name__ == "__main__":
    execute(Path(sys.argv[1]))
