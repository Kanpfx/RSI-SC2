"""Concise official-tool progress; complete events stay in SDK artifacts."""
import json
from pathlib import Path
from runtime.console import brief, progress


def tool_progress(event, role, workspace=None):
    """Keep display failures separate from execution and budget enforcement."""
    try:
        name = getattr(event, "tool_name", "")
        if getattr(event, "kind", None) == "ActionEvent" and event.action is not None:
            action = event.action
            if name == "terminal":
                progress(role, "Command: " + brief(action.command, 250))
            elif name == "file_editor":
                path = Path(action.path)
                if path.is_absolute() and workspace and path.is_relative_to(workspace):
                    path = path.relative_to(workspace)
                progress(role, f"File {action.command}: {path.as_posix()}")
            elif name == "run_sampling":
                progress(role, f"Sampling {action.matches} matches")
        elif getattr(event, "kind", None) == "AgentErrorEvent":
            progress(role, f"Tool rejected ({name}): {event.error}")
        elif getattr(event, "kind", None) == "ObservationEvent":
            observation = event.observation
            if name == "terminal":
                progress(role, f"Command result: exit_code={observation.exit_code}")
            elif name == "run_sampling":
                value = json.loads(observation.text)
                progress(role, "Sampling failed: " + brief(value["error"]) if value.get("error")
                    else f"Sampling finished; remaining={value['sampling_remaining']}")
            elif observation.is_error:
                progress(role, "Tool failed: " + brief(observation.text[-600:]))
    except Exception:
        pass  # Display payloads must not interrupt tools or budget enforcement.
