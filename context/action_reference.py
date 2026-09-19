"""Prompt builder for the single-model observation contract."""

from __future__ import annotations

from html import escape
from textwrap import indent
from typing import Any

from agent.runtime.actions.persistent import PERSISTENT_ACTION_IDS
from agent.runtime.actions.loader import load_type_definitions, model_type_names

def _section(tag: str, content: str, **attributes: str) -> str:
    """Use XML only for major semantic sections, not every nested field."""
    attrs = "".join(
        f' {key}="{escape(str(value), quote=True)}"'
        for key, value in attributes.items()
    )
    body = content.strip() or "[None]"
    return f"<{tag}{attrs}>\n{indent(body, '  ')}\n</{tag}>"


def _action_card(entry: dict[str, Any], definitions: dict[str, Any]) -> str:
    params = [
        param
        for param in entry["params"]
        if param["input"] == "model"
        and param["required"]
    ]
    arguments = ", ".join(
        f'{param["name"]}: {definitions["types"][param["type"]]["model_type"]}{" | null" if param.get("nullable") else ""}'
        for param in params
    )
    description = str(entry["description"]).strip()
    ongoing = entry["id"] in PERSISTENT_ACTION_IDS or entry["id"] == "macro.build_workers"
    lifetime = " [Persistent action]" if ongoing else ""
    lines = [f'- `{entry["name"]}({arguments})`{lifetime}', f'  {description}']
    lines.extend(
        _parameter_note(param, definitions)
        for param in params
        if param.get("prompt_detail") or definitions["types"][param["type"]].get("allowed_values")
    )
    return "\n".join(lines)


def _parameter_note(param: dict[str, Any], definitions: dict[str, Any]) -> str:
    """Keep only explicit special semantics and permitted enum values."""
    definition = definitions["types"][param["type"]]
    description = str(param.get("description", "")).strip() if param.get("prompt_detail") else ""
    if choices := definition.get("allowed_values"):
        description = (description + " Choices: " + ", ".join(choices) + ".").strip()
    return f'  - `{param["name"]}`: {description}'


def _type_legend(entries: list[dict[str, Any]], definitions: dict[str, Any]) -> str:
    used = set()
    for entry in entries:
        for param in entry["params"]:
            if param["input"] == "model" and param["required"]:
                used.update(model_type_names(definitions["types"][param["type"]]["model_type"]))
    lines = [f"- `{name}`: {description}"
             for name, description in definitions["model_types"].items() if name in used]
    return _section("argument_definitions", "\n".join(lines))


def _available_actions(entries: list[dict[str, Any]], definitions: dict[str, Any]) -> str:
    groups: dict[str, list[str]] = {
        "Group Combat Behaviors": [],
        "Individual Combat Behaviors": [],
        "Macro Behaviors": [],
    }
    for entry in entries:
        if entry["id"].startswith("combat.group."):
            category = "Group Combat Behaviors"
        elif entry["id"].startswith("macro."):
            category = "Macro Behaviors"
        else:
            category = "Individual Combat Behaviors"
        groups[category].append(_action_card(entry, definitions))
    body = "\n\n".join(
        f"**{category}**\n\n" + "\n".join(actions)
        for category, actions in groups.items()
        if actions
    ) or "[None]"
    return _section("available_actions", body)


def _actions_reference(entries: list[dict[str, Any]]) -> str:
    definitions = load_type_definitions()
    content = "\n\n".join((_type_legend(entries, definitions), _available_actions(entries, definitions)))
    return _section("actions_reference", content)


