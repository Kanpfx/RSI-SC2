"""Readable DSL and feedback; structured records remain dictionaries."""

from __future__ import annotations

import json
import keyword
from typing import Any


def format_value(value: Any) -> str:
    """Quote literal strings safely while keeping enums and landmarks bare."""
    if isinstance(value, str):
        if (
            value.isidentifier()
            and not keyword.iskeyword(value)
            and value.casefold() not in {"true", "false", "null", "none"}
        ):
            return value
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        fields = ", ".join(
            f"{format_value(key)}: {format_value(item)}"
            for key, item in value.items()
        )
        return "{" + fields + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(format_value(item) for item in value) + "]"
    return str(value)


def format_action(action: dict[str, Any]) -> str:
    """Render a normalized action as Name(arg=value)."""
    action_id = str(action.get("id", "UnknownAction"))
    args = action.get("args")
    if not isinstance(args, dict):
        return f"{action_id}()"
    arguments = ", ".join(f"{name}={format_value(value)}" for name, value in args.items())
    return f"{action_id}({arguments})"


def format_indexed_actions(actions: list[dict[str, Any]]) -> list[str]:
    """Render one action per line for chat and console output."""
    return [
        f"Action {index}: {format_action(action)}"
        for index, action in enumerate(actions, 1)
    ] or ["No actions."]


def format_feedback(feedback: list[dict[str, Any]]) -> str:
    """Keep multiline submitted text readable without JSON serialization."""
    blocks = []
    for item in feedback:
        if "submitted_action" in item:
            submitted = item["submitted_action"]
        elif "action" in item:
            action = item["action"]
            submitted = format_action(action) if isinstance(action, dict) else str(action)
        else:
            submitted = item.get(
                "submitted_action",
                item.get("submitted_output", item.get("submitted_phase")),
            )
        submitted = str(submitted if submitted is not None else "[None]").replace("\r", "\\r").replace("\n", "\\n")
        notice = item.get("kind") == "action_notice"
        index = item.get("action_index")
        label = f"Action {index}" if index is not None else "Action"
        label = f"Notice — {label}" if notice else f"Error — {label}"
        reason = " ".join(str(item.get('error', 'Unknown error')).splitlines())
        blocks.append(f"{label} — {submitted}: {reason}")
    return "\n".join(blocks) or "[None]"
