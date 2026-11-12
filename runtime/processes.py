"""Supervise subprocesses without interpreting their domain results."""
import os
import signal
import subprocess
import sys


def stop_process(process):
    """Terminate only a worker process tree created by this runner."""
    if sys.platform == "win32":
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        # Workers start their own sessions; retain this group ID even if the leader exits.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=15)
