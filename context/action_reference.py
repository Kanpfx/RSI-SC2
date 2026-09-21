"""Prompt builder for the single-model observation contract."""

from __future__ import annotations

from html import escape
from textwrap import indent
from typing import Any

from agent.runtime.actions.execution.persistent import PERSISTENT_ACTION_IDS
from agent.runtime.actions.resolution.loader import load_type_definitions
from agent.runtime.actions.resolution.types import type_names
from agent.runtime.actions.formatting import format_value

# Parameters self-evident from their name and the type legend. The action table
# lists them in the signature only; their meaning and (for Grid) value choices
# are stated once in the argument definitions.
GENERIC_PARAMS = frozenset({"unit", "group", "target", "targets", "grid"})
# Generic params whose bullet line still carries a non-obvious caveat and must stay.
GENERIC_PARAM_KEEP = frozenset({
    ("combat.individual.drop_cargo", "target"),
    ("combat.individual.tumor_spread_creep", "target"),
})

def _section(tag: str, content: str, **attributes: str) -> str:
    """Use XML only for major semantic sections, not every nested field."""
    attrs = "".join(
        f' {key}="{escape(str(value), quote=True)}"'
        for key, value in attributes.items()
    )
    body = content.strip() or "[None]"
    return f"<{tag}{attrs}>\n{indent(body, '  ')}\n</{tag}>"


def _action_card(entry: dict[str, Any]) -> str:
    params = [p for p in entry['params'] if p['input'] == 'model' and p['required']]
    arguments = [f"{p['name']}: {p['type']}" for p in params]
    ongoing = entry['id'] in PERSISTENT_ACTION_IDS or entry['id'] == 'macro.build_workers'
    lifetime = ' [Persistent action]' if ongoing else ''
    lines = [f"- `{entry['name']}({', '.join(arguments)})`{lifetime}",
             f"  {entry['description']}"]
    for p in params:
        if p['name'] in GENERIC_PARAMS and (entry['id'], p['name']) not in GENERIC_PARAM_KEEP:
            continue
        details = p['description']
        choices = p.get('choices')
        if choices is not None:
            details += ' Choices: ' + ', '.join(map(str, choices)) + '.'
        lines.append(f"  - `{p['name']}`: {details}")
    return '\n'.join(lines)


def _type_legend(entries: list[dict[str, Any]], definitions: dict[str, Any]) -> str:
    used = set()
    for entry in entries:
        for p in entry['params']:
            if p['input'] == 'model' and p['required']:
                used.update(type_names(p['type']))
    # Include nested field and container definitions used by compound aliases.
    changed = True
    while changed:
        previous = set(used)
        for name, definition in definitions.items():
            if name.split('[')[0] in used:
                used.update(type_names(definition.get('type', '')))
                for expression in definition.get('fields', {}).values():
                    used.update(type_names(expression))
        changed = previous != used
    BASIC = ("bool", "int", "float", "str", "null")
    CONTAINERS = ("list", "set", "tuple", "dict")
    lines = []
    basic_used = [name for name in BASIC if name in used]
    if basic_used:
        lines.append("- `" + "` `".join(basic_used) + "`: same with Python.")
    container_used = [name for name in definitions
                      if name.split('[')[0] in CONTAINERS and name.split('[')[0] in used]
    if container_used:
        lines.append("- `" + "` `".join(container_used) + "`: Python containers; JSON arrays/objects.")
    for name, definition in definitions.items():
        base = name.split('[')[0]
        if base in BASIC or base in CONTAINERS:
            continue
        if base not in used:
            continue
        line = f"- `{name}`: {definition.get('meaning', '')} Input: {definition['json']}."
        if 'fields' in definition:
            line += ' Fields: ' + ', '.join(f'{k}: {v}' for k, v in definition['fields'].items()) + '.'
        if 'values' in definition:
            line += ' Choices: ' + ', '.join(definition['values']) + '.'
        if 'example' in definition:
            line += f" Example: {format_value(definition['example'])}."
        lines.append(line)
    return "# argument_definitions\n" + "\n".join(lines)


def _available_actions(entries: list[dict[str, Any]]) -> str:
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
        groups[category].append(_action_card(entry))
    body = "\n\n".join(
        f"**{category}**\n\n" + "\n".join(actions)
        for category, actions in groups.items()
        if actions
    ) or "[None]"
    return "# available_actions\n" + body


def _actions_reference(entries: list[dict[str, Any]]) -> str:
    definitions = load_type_definitions()
    content = "\n\n".join((_type_legend(entries, definitions), _available_actions(entries)))
    return _section("actions_reference", content)


