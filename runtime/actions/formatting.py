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
    for index, item in enumerate(feedback, 1):
        if "action" in item:
            action = item["action"]
            submitted = format_action(action) if isinstance(action, dict) else str(action)
        else:
            submitted = item.get(
                "submitted_action",
                item.get("submitted_output", item.get("submitted_phase")),
            )
        submitted_lines = str(submitted if submitted is not None else "[None]").splitlines()
        if len(submitted_lines) <= 1:
            lines = [f"{index}. Action: {submitted_lines[0] if submitted_lines else '[None]'}"]
        else:
            lines = [f"{index}. Action: "]
            lines.extend(f"     {line}" for line in submitted_lines)
        lines.append(f"   Error: {item.get('error', 'Unknown error')}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "[None]"
