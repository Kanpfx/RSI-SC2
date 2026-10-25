"""Concise console progress; complete evidence and SDK output stay in artifacts."""
import json
from pathlib import Path
from runner.console import brief, progress


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
        _action_progress(role, name, action, workspace)
    elif kind == "AgentErrorEvent":
        progress(role, f"Tool rejected ({name}): {event.error}")
    elif kind == "ObservationEvent":
        observation = event.observation
        _observation_progress(role, name, observation)


def _action_progress(role, name, action, workspace):
    """Display diagnosis queries and editor operations."""
    match role, name:
        case "diagnosis", "select_issue":
            if action.get("issue"):
                progress(role, "Issue: " + action["issue"]["problem"])
            for hypothesis in action.get("hypotheses", []):
                progress(role, f"Hypothesis {hypothesis['id']}: {hypothesis['explanation']}")
        case "diagnosis", "query_logs":
            params = action.get("params", {})
            target = params.get("names") or params.get("types") or ""
            if isinstance(target, (list, tuple)):
                target = ", ".join(map(str, target))
            query = " ".join(part for part in (f"Query {action['kind']}", brief(target, 110)) if part)
            progress(role, f"{query}; purpose: {brief(action['purpose'], 180)}")
        case ("diagnosis" | "editor"), "read_reference":
            parts = ["Read " + brief(action["path"], 140)]
            if action.get("key"):
                parts.append("key: " + brief(action["key"], 80))
            progress(role, "; ".join(parts))
        case "diagnosis", "query_memory":
            progress(role, "Query memory: " + brief(action.get("text") or "all", 140))
        case "diagnosis", "conclude":
            progress(role, "Submitting conclusion" if action.get("conclusion") else "No proposal: " + action.get("reason", ""))
        case "editor", "file_editor":
            path = Path(action["path"])
            if path.is_absolute() and workspace and path.is_relative_to(workspace):
                path = path.relative_to(workspace)
            progress(role, f"File {action['command']}: {path.as_posix()}")
        case "editor", "terminal":
            progress(role, "Command: " + brief(action.get("command", ""), 250))


def _observation_progress(role, name, observation):
    """Display returned evidence, tool errors and command results."""
    match role, name:
        case ("diagnosis", "select_issue" | "query_logs" | "read_reference" | "query_memory" | "conclude") | ("editor", "read_reference"):
            value = json.loads(observation.text)
            if value.get("error"):
                progress(role, "Tool rejected: " + value["error"]["message"])
            elif name in {"query_logs", "read_reference", "query_memory"}:
                count = len(value["items"]) if "items" in value else int("source" in value or "value" in value or "keys" in value)
                parts = [f"Results: {count}"]
                if "next_offset" in value:
                    parts.append(f"next_offset={value['next_offset']}")
                if value.get("truncated"):
                    parts.append("truncated")
                if value.get("reason"):
                    parts.append(brief(value["reason"]))
                progress(role, "; ".join(parts))
            elif name == "conclude" and value.get("conclusion"):
                conclusion = value["conclusion"]
                progress(role, "Conclusion validated: " + conclusion["change"])
            elif name == "conclude":
                progress(role, "Unresolved: " + value.get("reason", "No proposal"))
        case "editor", "terminal":
            code = observation.exit_code
            parts = [f"Command result: exit_code={code}"]
            if code not in (0, None):
                parts.append(brief(observation.text[-600:]))
            progress(role, "; ".join(parts))
        case "editor", _:
            if observation.is_error:
                progress(role, "Tool failed: " + brief(observation.text[-600:]))
