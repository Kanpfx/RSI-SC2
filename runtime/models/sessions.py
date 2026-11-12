"""Reuse OpenHands SDK sessions with process, token and observed-cost budgets."""
import json
import os
import subprocess
import sys
import time
import traceback
from contextlib import ExitStack, contextmanager
from importlib import import_module
from pathlib import Path
from urllib.parse import urlsplit

from runtime.workspace.storage import path_reference, project_path, read_json, write_json
from runtime.workspace.versions import SnapshotError
from runtime.console import write_console
from runtime.models.events import tool_progress
from runtime.processes import stop_process


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


class SessionError(RuntimeError):
    """Carry a worker's stop decision without parsing its traceback."""

    def __init__(self, reason, *, fatal=False):
        super().__init__(reason)
        self.fatal = fatal


def fatal_model_error(error):
    """Stop on authentication, billing and invalid API configuration errors."""
    from openhands.sdk.llm.exceptions import LLMAuthenticationError
    while error is not None:
        if (isinstance(error, LLMAuthenticationError)
                or getattr(error, "status_code", None) in {400, 401, 402, 403, 404, 422}):
            return True
        error = error.__cause__ or error.__context__
    return False


class ExperimentRuntime:
    """Reuse one SDK worker process for all sessions in one experiment."""

    def __init__(self, project, root):
        self.project, self.root = Path(project).resolve(), Path(root).resolve()
        self.worker = None
        self.worker_resources = None
        self.counters = {}
        self.resources = ExitStack()
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        """Stop the worker and close its log files."""
        if not self.closed:
            self.closed = True
            self.resources.close()

    def next_name(self, prefix):
        """Keep experiment-wide numbering even after temporary candidates are removed."""
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return f"{prefix}_{self.counters[prefix]:02d}"

    @staticmethod
    def _stop(process):
        """Allow an idle worker to exit on EOF, then enforce process-tree cleanup."""
        try:
            process.stdin.close()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            stop_process(process)

    def _worker(self):
        """Reuse a live worker; replace an exited worker for a subsequent job."""
        if self.worker is not None and self.worker.poll() is not None:
            self.worker_resources.close()
            self.worker = None
        if self.worker is None:
            self.root.mkdir(parents=True, exist_ok=True)
            resources = self.resources.enter_context(ExitStack())
            self.worker_resources = resources
            try:
                command = [sys.executable, "-u", "-B", "-m", "runtime.models.sessions", "--worker"]
                out = resources.enter_context((self.root / "worker.stdout.txt").open("a", encoding="utf-8"))
                err = resources.enter_context((self.root / "worker.stderr.txt").open("a", encoding="utf-8"))
                process = subprocess.Popen(command, cwd=self.project, stdin=subprocess.PIPE,
                    stdout=out, stderr=err, text=True, encoding="utf-8",
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                    start_new_session=sys.platform != "win32",
                    env=dict(os.environ, PYTHONIOENCODING="utf-8"))
                resources.callback(self._stop, process)
            except BaseException:
                resources.close()
                raise
            self.worker = process
        return self.worker

    def call_session(self, job, directory, *, on_sampling=None):
        """Submit one independent conversation and enforce its existing wall deadline."""
        if self.closed:
            raise RuntimeError("Experiment runtime is closed")
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=False)
        write_json(directory / "job.json", job)
        (directory / "stdout.txt").touch()
        (directory / "stderr.txt").touch()
        path = directory / "response.json"
        deadline = time.monotonic() + job["model"]["wall_timeout"]
        overall_deadline = time.monotonic() + job.get("wall_remaining", job["model"]["wall_timeout"])
        process = None
        try:
            job_path = directory / "job.json"
            process = self._worker()
            if process.poll() is not None:
                raise RuntimeError("Model worker exited before submission")
            process.stdin.write(json.dumps(str(job_path)) + "\n")
            process.stdin.flush()
            with (directory / "stdout.txt").open(encoding="utf-8", errors="replace") as stream:
                while True:
                    for line in stream:
                        write_console(line, end="")
                    if (directory / "done").exists():
                        for line in stream:
                            write_console(line, end="")
                        break
                    if process.poll() is not None:
                        raise RuntimeError("Model worker exited without a response")
                    for request_path in (directory / "sampling").glob("*/request.json"):
                        response_path = request_path.with_name("response.json")
                        if response_path.exists():
                            continue
                        if on_sampling is None:
                            raise RuntimeError("Sampling is unavailable in this session")
                        sampling_started = time.monotonic()
                        value = on_sampling(read_json(request_path))
                        write_json(response_path, value)
                        deadline += time.monotonic() - sampling_started
                    remaining = min(deadline, overall_deadline) - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(process.args, job["model"]["wall_timeout"])
                    try:
                        process.wait(timeout=min(0.25, remaining))
                    except subprocess.TimeoutExpired:
                        pass
        except BaseException as exc:
            if process is not None and process.poll() is None:
                stop_process(process)
            response = dict(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                reason="model_session_interrupted" if isinstance(exc, KeyboardInterrupt) else
                       "model_session_wall_timeout" if isinstance(exc, subprocess.TimeoutExpired) else
                       "model_worker_error: " + str(exc), usage=_read_usage(directory),
                fatal=isinstance(exc, (FileNotFoundError, PermissionError, SnapshotError)))
            write_json(path, response)
            if not isinstance(exc, Exception):
                raise
        return read_json(path)


def _create_llm(job):
    """Construct the configured transport with per-request and session budgets."""
    from pydantic import SecretStr
    from runtime.models.budget import BudgetedLLM
    settings = job["model"]
    # SDK 1.41 counts total attempts: the initial request plus two retries.
    return BudgetedLLM(
        token_budget=settings["max_total_tokens"],
        model=settings["model"], api_key=SecretStr(os.environ[settings["api_key_env"]]),
        base_url=settings.get("base_url"), timeout=settings["request_timeout"], num_retries=3,
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
    conversation = stage = llm = None
    selected = []
    tools_attached = False
    response = {}
    try:
        from openhands.sdk import Agent
        from openhands.sdk.conversation import ConversationExecutionStatus, LocalConversation
        from openhands.sdk.event.conversation_error import ConversationErrorEvent
        settings = job["model"]
        prompt = job["prompt"]
        module = import_module(job["session"])
        stage, prompt = module.prepare_session(job, directory, prompt)
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
        conversation = LocalConversation(agent=Agent(llm=llm, tools=[]), workspace=str(workspace),
            persistence_dir=str(directory / "conversation"), max_iteration_per_run=settings["max_iterations"],
            max_budget_per_run=settings["max_cost"],
            callbacks=[record_usage], visualizer=None)
        conversation.send_message(prompt)
        selected = module.create_tools(job, conversation.state, stage)
        conversation.agent.add_runtime_tools(selected)
        tools_attached = True
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
        from openhands.sdk.conversation.response_utils import get_agent_final_response
        text = get_agent_final_response(list(conversation.state.events)).strip()
        value = stage.summary(text) if stage else text
        response = dict(status="ok", value=value, usage=usage)
    except Exception as exc:
        response = dict(status="error", reason=traceback.format_exc(),
                        usage=_read_usage(directory), fatal=fatal_model_error(exc))
    finally:
        if conversation:
            response["execution_status"] = conversation.state.execution_status.value
            try:
                conversation.close()
            except Exception as exc:
                tools_attached = False
                response["cleanup_error"] = str(exc)
                response["fatal"] = True
                if response.get("status") == "ok":
                    response.update(status="error", reason="model_session_cleanup_failed")
        # SDK owns attached executors; failed setup must close unattached ones here.
        for tool in (() if tools_attached else selected):
            try:
                tool.executor.close()
            except Exception as exc:
                response.setdefault("cleanup_error", str(exc))
                response["fatal"] = True
                if response.get("status") == "ok":
                    response.update(status="error", reason="model_session_cleanup_failed")
        if stage:
            if response["status"] != "ok":
                stage.save()
            response["execution_path"] = path_reference(stage.execution_path)
            stage.close()
            if "value" in response:
                response["result_path"] = path_reference(stage.path)
        write_json(directory / "response.json", response)
    return response


@contextmanager
def session_output(directory):
    """Route Python, SDK logging and native output to this session's own files."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved = [os.dup(fd) for fd in (1, 2)]
    try:
        with (directory / "stdout.txt").open("a", encoding="utf-8") as out, \
                (directory / "stderr.txt").open("a", encoding="utf-8") as err:
            os.dup2(out.fileno(), 1)
            os.dup2(err.fileno(), 2)
            try:
                yield
            finally:
                sys.stdout.flush()
                sys.stderr.flush()
    finally:
        for fd, original in zip((1, 2), saved):
            os.dup2(original, fd)
            os.close(original)


def serve():
    """Execute job paths from stdin in order, retaining imports but no live conversation."""
    for line in sys.stdin:
        job_path = Path(json.loads(line))
        with session_output(job_path.parent):
            write_json(job_path.parent / "worker.json", dict(pid=os.getpid()))
            response = execute(job_path)
        (job_path.parent / "done").touch()
        if response.get("fatal"):
            return


if __name__ == "__main__":
    if sys.argv[1] == "--worker":
        serve()
    else:
        execute(Path(sys.argv[1]))
