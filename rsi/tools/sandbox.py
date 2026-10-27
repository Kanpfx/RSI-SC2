"""Run the existing Editor worker in an explicitly selected Docker environment."""
import copy
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

from runner.artifacts import project_path, write_json


def _docker(arguments):
    """Bound Docker control commands without starting visible Windows helper windows."""
    return subprocess.run(["docker", *arguments], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=30,
                          creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)


def preflight_editor(execution):
    """Reject an unavailable Docker daemon or unbuilt image before collecting games."""
    if execution["mode"] == "local":
        return
    try:
        result = _docker(["version", "--format", "{{.Server.Version}}"])
        if result.returncode:
            raise ValueError("Editor Docker daemon unavailable: " + result.stderr[-1000:])
        result = _docker(["image", "inspect", execution["image"]])
        if result.returncode:
            raise ValueError("Editor image unavailable; build rsi/Dockerfile first: " + execution["image"])
        name = "rsi-check-" + uuid4().hex[:12]
        try:
            result = _docker(["run", "--rm", "--name", name, "--network", "none",
                              "-e", "LITELLM_LOCAL_MODEL_COST_MAP=True", "-e", "OPENHANDS_SUPPRESS_BANNER=1",
                              execution["image"], "python", "-B", "-c",
                              "from importlib.metadata import version; "
                              "assert version('openhands-sdk') == '1.41.0'; "
                              "assert version('openhands-tools') == '1.41.0'; "
                              "assert version('burnysc2') == '7.3.0'; "
                              "from openhands.tools.terminal import TerminalTool"])
        except BaseException:
            _docker(["rm", "--force", name])
            raise
        if result.returncode:
            raise ValueError("Editor image dependency check failed: " + result.stderr[-1000:])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Editor Docker unavailable; install/start Docker and build rsi/Dockerfile") from exc


def prepare_editor_job(job, root, directory):
    """Translate one job into the experiment's fixed mounts and freeze its evidence."""
    root, directory = Path(root).resolve(), Path(directory).resolve()
    candidate = project_path(job["workspace"])
    workspace = "/work/" + candidate.relative_to(root / "work").as_posix()
    session = "/sessions/" + directory.relative_to(root / "sessions").as_posix()
    evidence = project_path(candidate / job["task"]["paths"]["evidence"])
    inputs = root / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(evidence, inputs / (directory.name + ".json"))
    container_job = copy.deepcopy(job)
    container_job.update(workspace=workspace, context="/refs")
    container_job["task"]["paths"] = dict(evidence=f"/inputs/{directory.name}.json", references="/refs",
                                          tests="/tests", python="/usr/local/bin/python")
    write_json(directory / "container_job.json", container_job)
    return session + "/container_job.json"


def docker_command(project, job, root, name):
    """Mount only this experiment's mutable files and shared read-only dependencies."""
    project, root = Path(project).resolve(), Path(root).resolve()
    for kind in ("work", "sessions", "inputs"):
        (root / kind).mkdir(parents=True, exist_ok=True)
    command = ["docker", "run", "--rm", "-i", "--name", name, "--init", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--workdir", "/app",
               "-e", job["model"]["api_key_env"], "-e", "PYTHONPATH=/app", "-e", "PYTHONIOENCODING=utf-8"]
    mounts = [(root / "work", "/work", False), (root / "sessions", "/sessions", False),
              (root / "inputs", "/inputs", True), (project_path(job["context"]), "/refs", True),
              (project / "rsi", "/app/rsi", True), (project / "runner", "/app/runner", True),
              (project / "scripts/test", "/tests", True)]
    for source, target, readonly in mounts:
        if not source.exists():
            raise ValueError(f"Editor mount unavailable: {source}")
        command.extend(["--mount", f"type=bind,source={source},target={target}" + (",readonly" if readonly else "")])
    command.extend([job["execution"]["image"], "python", "-u", "-B", "-m", "rsi.tools.llm", "--worker"])
    return command


@contextmanager
def editor_container(project, job, root):
    """Always remove this worker's container, including cancellation and timeout paths."""
    name = "rsi-editor-" + uuid4().hex[:12]
    command = docker_command(project, job, root, name)
    try:
        yield command
    finally:
        # --rm may already have removed a normally exited container.
        try:
            result = _docker(["rm", "--force", name])
            if result.returncode and "No such container" not in result.stderr:
                write_json(Path(root) / "cleanup.json", dict(container=name, error=result.stderr[-2000:]))
        except (OSError, subprocess.TimeoutExpired) as exc:
            write_json(Path(root) / "cleanup.json", dict(container=name, error=str(exc)))
