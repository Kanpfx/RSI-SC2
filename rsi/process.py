import os
import subprocess

import psutil

from rsi.evolution.state import redact


def child_env():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("LLM_", "OPENAI_", "PYTHON", "PYTEST"))}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    return env


def terminate_tree(process):
    try:
        children = process.children(recursive=True)
    except psutil.Error:
        children = []
    for child in reversed(children):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    try:
        process.kill()
    except psutil.NoSuchProcess:
        pass
    psutil.wait_procs(children, timeout=5)


def run_process(argv, cwd, timeout, env=None):
    process = psutil.Popen(
        [str(arg) for arg in argv], cwd=cwd, env=env or child_env(),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_tree(process)
        stdout, stderr = process.communicate(timeout=10)
    except BaseException:
        terminate_tree(process)
        process.communicate(timeout=10)
        raise
    return {"stdout": redact(stdout), "stderr": redact(stderr),
            "exit_code": process.returncode, "timed_out": timed_out}
