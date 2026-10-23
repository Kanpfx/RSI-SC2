"""Reuse OpenHands SDK sessions with process, token and observed-cost budgets."""
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from urllib.parse import urlsplit

from ..console import tool_progress, write_console
from .matches import stop_process
from .versions import project_path, read_json, write_json


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
                                   stdout=out, stderr=err, creationflags=flags,
                                   env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        try:
            # Relay only worker stdout; SDK diagnostics remain in stderr.txt.
            deadline = time.monotonic() + job["model"]["wall_timeout"]
            with (directory / "stdout.txt").open(encoding="utf-8", errors="replace") as stream:
                while True:
                    for line in stream:
                        write_console(line, end="")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(process.args, job["model"]["wall_timeout"])
                    try:
                        process.wait(timeout=min(0.25, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        pass
                for line in stream:
                    write_console(line, end="")
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
            if estimate <= 0:
                raise ValueError("Token estimation failed; check SDK token counter and tool schemas")
            if spent + required > settings["max_total_tokens"]:
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
        from pydantic import SecretStr
        from openhands.sdk import Agent, Conversation, Tool
        from openhands.sdk.tool import register_tool
        from openhands.sdk.conversation.response_utils import get_agent_final_response
        settings = job["model"]
        prompt = job["prompt"]
        if job["role"] == "diagnosis":
            from ..diagnosis.investigate import Investigation
            from ..diagnosis.session import investigation_tools
            investigation = Investigation(job, directory)
            if job["diagnosis"]["query_policy"] == "fixed":
                prompt += "\nPredeclared evidence pack:\n" + json.dumps(investigation.fixed_pack(), ensure_ascii=False)
            definitions = investigation_tools(investigation)
        elif job["role"] == "editor":
            from openhands.tools.terminal import TerminalTool
            from ..editor import candidate_editor
            definitions = [candidate_editor(job["workspace"])]
            if job.get("context"):
                from ..retriever import Retriever, reference_tool
                references = Retriever(dict(source_root=job["workspace"]), job["context"])
                definitions.append(reference_tool(references.read_reference))
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
            tool_progress(event, job["role"], job.get("workspace"))
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
                value.setdefault("reason", "Session ended without an actionable conclusion")
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
