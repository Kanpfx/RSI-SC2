"""Supervise subprocesses without interpreting their domain results."""
import os
import signal
import subprocess
import sys
import time


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


def wait_process(process, timeout, *, cancel=None, on_poll=None):
    """Wait with a deadline and optional cancellation/output polling; return False on cancellation."""
    deadline = time.monotonic() + timeout
    while True:
        if on_poll:
            on_poll()
        if cancel is not None and cancel.is_set():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            process.wait(timeout=min(0.25, remaining) if cancel is not None or on_poll else remaining)
            if on_poll:
                on_poll()
            return True
        except subprocess.TimeoutExpired:
            if cancel is None and on_poll is None:
                raise
