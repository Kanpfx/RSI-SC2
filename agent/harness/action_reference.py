"""Prompt builder for the single-model observation contract."""

from __future__ import annotations

from html import escape
from textwrap import indent
from typing import Any

from agent.game.actions.execution.lifecycle import PERSISTENT_ACTION_IDS
from agent.paths import PROMPTS_ROOT

def _section(tag: str, content: str, **attributes: str) -> str:
    """Use XML only for major semantic sections, not every nested field."""
    attrs = "".join(
        f' {key}="{escape(str(value), quote=True)}"'
        for key, value in attributes.items()
    )
    body = content.strip() or "[None]"
    return f"<{tag}{attrs}>\n{indent(body, '  ')}\n</{tag}>"


def _action_card(entry: dict[str, Any]) -> str:
    params = [p for p in entry['params'] if p['source'] == 'model']
    arguments = [f"{p['name']}: {p['type']}" + ("" if p['required'] else " = null") for p in params]
    ongoing = entry['name'] in PERSISTENT_ACTION_IDS or entry['name'] == 'BuildWorkers'
    lifetime = ' [Persistent action]' if ongoing else ''
    return f"- `{entry['name']}({', '.join(arguments)})`:{lifetime} {entry['description']}"


def _actions_reference(entries: list[dict[str, Any]]) -> str:
    groups = {}
    for entry in entries:
        category = entry['import'].split('.')[2]
        groups.setdefault(category, []).append(_action_card(entry))
    legend = "# argument_definitions\n" + (PROMPTS_ROOT / "argument_definitions.md").read_text(encoding="utf-8").strip()
    actions = '\n\n'.join(f"**{category}**\n\n" + '\n'.join(cards) for category, cards in groups.items())
    return _section("actions_reference", legend + "\n\n# available_actions\n" + (actions or "[None]"))


def _action_capabilities(entries: list[dict[str, Any]]) -> str:
    cards = []
    for entry in entries:
        persistent = entry['name'] in PERSISTENT_ACTION_IDS or entry['name'] == 'BuildWorkers'
        marker = ' [Persistent]' if persistent else ''
        cards.append(f"- {entry['name']}:{marker} {entry['description']}")
    return _section("action_capabilities", '\n'.join(cards))
