"""Reuse OpenHands SDK sessions with process, token and observed-cost budgets."""
import json
import math
import os
import subprocess
import sys
import traceback
from pathlib import Path
from urllib.parse import urlsplit

from runner.artifacts import path_reference, project_path, read_json, write_json
from runner.console import write_console
from .console import tool_progress
from runner.processes import stop_process, wait_process


def load_settings(project, settings):
    """Use explicit role settings with environment defaults; keep credentials out of artifacts."""
    from dotenv import dotenv_values
    values = dotenv_values(Path(project) / ".env")
    settings = dict(settings)
    for name, field in (("LLM_MODEL", "model"), ("LLM_BASE_URL", "base_url")):
        value = os.environ.get(name) or values.get(name)
        if value and not settings.get(field):
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
    from openhands.sdk.conversation import LocalConversation  # noqa: F401
    from openhands.tools.file_editor import FileEditorTool  # noqa: F401
    from openhands.tools.terminal import TerminalTool  # noqa: F401
    _create_llm(dict(model=settings, role="preflight"))


def _read_usage(directory):
    """Read recorded usage, keeping a missing worker record distinct from zero usage."""
    path = directory / "usage.json"
    return read_json(path) if path.exists() else {}


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
                                   stdout=out, stderr=err, creationflags=flags,
                                   env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        try:
            # Relay only worker stdout; SDK diagnostics remain in stderr.txt.
            with (directory / "stdout.txt").open(encoding="utf-8", errors="replace") as stream:
                def relay():
                    """Forward newly written progress while retaining the worker log."""
                    for line in stream:
                        write_console(line, end="")
                wait_process(process, job["model"]["wall_timeout"], on_poll=relay)
        except subprocess.TimeoutExpired:
            stop_process(process)
            response = dict(status="error", reason="model_session_wall_timeout",
                            usage=_read_usage(directory))
            write_json(directory / "response.json", response)
            return response
        except BaseException as exc:
            stop_process(process)
            write_json(directory / "response.json", dict(
                status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                reason="model_session_interrupted" if isinstance(exc, KeyboardInterrupt) else "model_worker_error",
                usage=_read_usage(directory)))
            raise
    path = directory / "response.json"
    if not path.exists():
        write_json(path, dict(status="error", reason="model_worker_missing_response", usage={}))
    response = read_json(path)
    if "result_path" in response:
        response["value"] = read_json(project_path(response["result_path"]))
    return response


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
            if estimate <= 0:
                raise ValueError("Token estimation failed; check SDK token counter and tool schemas")
            if spent + required > settings["max_total_tokens"]:
                raise ValueError("Next request cannot fit the token budget")

        def completion(self, messages, tools=None, add_security_risk_prediction=False, **kwargs):
            """Check the budget before delegating to SDK completion transport."""
            self.check_request(messages, tools, add_security_risk_prediction)
            return super().completion(messages, tools, add_security_risk_prediction, **kwargs)

        def responses(self, messages, tools=None, **kwargs):
            """Apply the same budget to SDK Responses transport."""
            self.check_request(messages, tools, kwargs.get("add_security_risk_prediction", False))
            return super().responses(messages, tools, **kwargs)

    return BudgetedLLM


def _session_tools(job, investigation):
    """Select and register only the tools available to this session role."""
    from openhands.sdk import Tool
    from openhands.sdk.tool import register_tool

    if investigation:
        from ..diagnosis.session import investigation_tools
        definitions = investigation_tools(investigation)
    elif job["role"] == "editor":
        from ..editor import editor_tools
        return editor_tools(job)
    else:
        definitions = []
    selected = []
    for definition in definitions:
        register_tool(definition.name, definition)
        selected.append(Tool(name=definition.name))
    return selected


def _create_llm(job):
    """Construct the configured transport with per-request and session budgets."""
    from pydantic import SecretStr
    settings = job["model"]
    # SDK 1.41 counts total attempts: the initial request plus one retry.
    return budgeted_llm_type(settings)(
        model=settings["model"], api_key=SecretStr(os.environ[settings["api_key_env"]]),
        base_url=settings.get("base_url"), timeout=settings["request_timeout"], num_retries=2,
        max_output_tokens=settings["max_output_tokens"],
        reasoning_effort=settings.get("reasoning_effort", "high"),
        temperature=settings.get("temperature"), top_p=settings.get("top_p"),
        top_k=settings.get("top_k"), seed=settings.get("seed"), usage_id=job["role"])


def _save_usage(llm, directory):
    """Persist observed tokens and costs; unavailable vendor prices remain null."""
    usage = llm.metrics.accumulated_token_usage
    value = dict(tokens=usage.prompt_tokens + usage.completion_tokens if usage else None,
                 cost=llm.metrics.accumulated_cost if llm.metrics.costs else None)
    write_json(directory / "usage.json", value)
    return value


def execute(job_path, llm_factory=None):
    """Worker entry; model transport can be injected for offline SDK verification."""
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
    job_path = Path(job_path)
    job, directory = read_json(job_path), job_path.parent
    if "workspace" in job:
        job["workspace"] = str(project_path(job["workspace"]))
    conversation = investigation = llm = None
    response = {}
    try:
        from openhands.sdk import Agent
        from openhands.sdk.conversation import ConversationExecutionStatus, LocalConversation
        from openhands.sdk.event.conversation_error import ConversationErrorEvent
        settings = job["model"]
        parts = [job["prompt"]]
        if "task" in job:
            parts.append("Task data (JSON):\n" + json.dumps(job["task"], ensure_ascii=False, separators=(",", ":")))
        prompt = "\n\n".join(parts)
        if job["role"] == "diagnosis":
            from ..diagnosis.session import prepare_session
            investigation, prompt = prepare_session(job, directory, prompt)
        selected = _session_tools(job, investigation)
        llm = llm_factory() if llm_factory else _create_llm(job)
        write_json(directory / "llm.json", llm.model_dump(mode="json", include={
            "model", "base_url", "reasoning_effort", "temperature", "top_p", "top_k", "seed",
            "max_output_tokens", "timeout", "num_retries", "api_mode", "drop_params", "caching_prompt"}))

        def record_usage(event=None):
            """Persist observed usage; unknown vendor pricing remains null."""
            value = _save_usage(llm, directory)
            if conversation and (value["tokens"] or 0) >= settings["max_total_tokens"]:
                conversation.pause()
            tool_progress(event, job["role"], job.get("workspace"))
            return value

        workspace = Path(job.get("workspace", directory / "readonly_session"))
        workspace.mkdir(parents=True, exist_ok=True)
        conversation = LocalConversation(agent=Agent(llm=llm, tools=selected), workspace=str(workspace),
            persistence_dir=str(directory / "conversation"), max_iteration_per_run=settings["max_iterations"],
            max_budget_per_run=settings["max_cost"],
            callbacks=[record_usage], visualizer=None)
        conversation.send_message(prompt)
        # SDK discovery skips LLM subclasses, including our token-budgeted transport.
        conversation.llm_registry.add(llm)
        conversation.run()
        usage = record_usage()
        if conversation.state.execution_status != ConversationExecutionStatus.FINISHED:
            error = next((event for event in reversed(list(conversation.state.events))
                          if isinstance(event, ConversationErrorEvent)), None)
            reason = f"{error.code}: {error.detail}" if error else "Session did not finish"
            raise RuntimeError(f"{conversation.state.execution_status.value}: {reason}")
        if (usage["tokens"] or 0) >= settings["max_total_tokens"]:
            raise ValueError("Model session token budget reached")
        if investigation:
            value = investigation.result()
        else:
            from openhands.sdk.conversation.response_utils import get_agent_final_response
            value = get_agent_final_response(list(conversation.state.events)).strip()
        response = dict(status="ok", value=value, usage=usage)
    except Exception:
        response = dict(status="error", reason=traceback.format_exc(),
                        usage=_read_usage(directory))
    finally:
        if conversation:
            response["execution_status"] = conversation.state.execution_status.value
            try:
                conversation.close()
            except Exception as exc:
                response["cleanup_error"] = str(exc)
        saved = dict(response)
        if investigation and "value" in saved:
            saved.pop("value")
            saved["result_path"] = path_reference(directory / "investigation.json")
        write_json(directory / "response.json", saved)
    return response


if __name__ == "__main__":
    execute(Path(sys.argv[1]))
