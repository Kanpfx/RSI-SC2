"""Small, failure-tolerant console output shared by command-line runners."""
import time


def brief(value, limit=320):
    """Keep one console entry on one bounded line."""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def write_console(message, end="\n"):
    """Keep console write failures outside the research execution path."""
    try:
        print(message, end=end, flush=True)
    except Exception:
        pass  # Display is optional; artifacts and research execution must continue.


def progress_line(component, message):
    """Format a progress line without changing its content or output policy."""
    return f"[{time.strftime('%H:%M:%S')}] [{component}] {message}"


def progress(component, message):
    """Flush useful progress immediately, including from model workers."""
    write_console(progress_line(component, brief(message)))


def error_summary(reason):
    """Prefer the underlying exception to traceback and SDK help boilerplate."""
    for line in str(reason).splitlines():
        if "Traceback" in line:
            continue
        if line == line.lstrip() and line.split(":", 1)[0].endswith(("Error", "Exception")):
            return brief(line)
    return brief(reason)


