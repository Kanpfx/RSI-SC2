"""Concise console progress; complete evidence and SDK output stay in artifacts."""
import json
from pathlib import Path
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


def tool_progress(event, role, workspace=None):
    """Isolate all SDK event rendering failures from conversation callbacks."""
    try:
        _tool_progress(event, role, workspace)
    except Exception:
        pass  # Unexpected display payloads must not interrupt SDK error recovery.


def _tool_progress(event, role, workspace=None):
    """Summarize public SDK tool events without printing model thoughts or raw payloads."""
    kind = getattr(event, "kind", None)
    name = getattr(event, "tool_name", "")
    if kind == "ActionEvent":
        if event.action is None:
            return  # SDK emits this before AgentErrorEvent for invalid arguments.
        action = event.action.model_dump(exclude_none=True, by_alias=True,
                                         include=set(type(event.action).model_fields))
        match role, name:
            case "diagnosis", "select_issue":
                if action.get("issue"):
                    progress(role, "Issue: " + action["issue"]["problem"])
                for hypothesis in action.get("hypotheses", []):
                    progress(role, f"Hypothesis {hypothesis['id']}: {hypothesis['explanation']}")
            case "diagnosis", "query_logs":
                params = action.get("params", {})
                target = params.get("path") or params.get("names") or params.get("types") or ""
                if isinstance(target, (list, tuple)):
                    target = ", ".join(map(str, target))
                progress(role, f"Query {action['kind']} {brief(target, 110)}; purpose: {brief(action['purpose'], 180)}")
            case ("diagnosis" | "editor"), "read_reference":
                progress(role, "Read " + brief(action["path"], 140) +
                         ("; key: " + brief(action["key"], 80) if action.get("key") else ""))
            case "diagnosis", "query_memory":
                progress(role, "Query memory: " + brief(action.get("text") or "all available experiences", 140))
            case "diagnosis", "conclude":
                progress(role, "Submitting conclusion" if action.get("conclusion") else "Ending without a proposal: " + action.get("reason", ""))
            case "editor", "file_editor":
                path = Path(action["path"])
                if path.is_absolute() and workspace and path.is_relative_to(workspace):
                    path = path.relative_to(workspace)
                progress(role, f"File {action['command']}: {path.as_posix()}")
            case "editor", "terminal":
                progress(role, "Command: " + brief(action.get("command", ""), 250))
    elif kind == "AgentErrorEvent":
        progress(role, f"Tool rejected ({name}): {event.error}")
    elif kind == "ObservationEvent":
        observation = event.observation
        match role, name:
            case ("diagnosis", "select_issue" | "query_logs" | "read_reference" | "query_memory" | "conclude") | ("editor", "read_reference"):
                value = json.loads(observation.text)
                if value.get("status") in {"invalid_input", "invalid_query"}:
                    progress(role, "Tool rejected: " + value["error"]["message"])
                elif name in {"query_logs", "read_reference", "query_memory"}:
                    progress(role, f"Evidence returned: {len(value.get('items', []))} items" +
                             (" (truncated; further queries available)" if value.get("truncated") else ""))
                elif name == "conclude" and value.get("conclusion"):
                    conclusion = value["conclusion"]
                    progress(role, "Conclusion validated: " + conclusion["change"])
                elif name == "conclude":
                    progress(role, "Unresolved: " + value.get("reason", "No proposal"))
            case "editor", "terminal":
                code = observation.exit_code
                progress(role, f"Command result: exit_code={code}" +
                         ("; " + brief(observation.text[-600:]) if code not in (0, None) else ""))
            case "editor", _:
                if observation.is_error:
                    progress(role, "Tool failed: " + brief(observation.text[-600:]))
