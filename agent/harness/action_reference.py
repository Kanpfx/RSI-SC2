"""Render action capabilities and DSL references for model prompts."""

from __future__ import annotations

from collections.abc import Callable
from html import escape
from textwrap import indent
from typing import Any

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
    lifetime = ' [Persistent]' if entry['persistent'] else ''
    return f"- `{entry['name']}({', '.join(arguments)})`:{lifetime} {entry['description']}"


def _actions_reference(entries: list[dict[str, Any]]) -> str:
    legend = "# argument_definitions\n" + (PROMPTS_ROOT / "argument_definitions.md").read_text(encoding="utf-8").strip()
    actions = _grouped_actions(entries, _action_card)
    return _section("actions_reference", legend + "\n\n# available_actions\n" + (actions or "[None]"))


def _action_capabilities(entries: list[dict[str, Any]]) -> str:
    return _section("action_capabilities", _grouped_actions(entries, _capability_card))


def _capability_card(entry: dict[str, Any]) -> str:
    marker = ' [Persistent]' if entry['persistent'] else ''
    return f"- {entry['name']}:{marker} {entry['description']}"


def _grouped_actions(entries: list[dict[str, Any]], render: Callable[[dict[str, Any]], str]) -> str:
    groups = {'Individual Combat': [], 'Group Combat': [], 'Macro': []}
    for entry in entries:
        groups[_action_category(entry)].append(render(entry))
    return '\n\n'.join(
        f"**{category}**\n\n" + '\n'.join(cards)
        for category, cards in groups.items() if cards
    )


def _action_category(entry: dict[str, Any]) -> str:
    parts = entry['import'].split('.')
    if parts[2] == 'macro':
        return 'Macro'
    return 'Individual Combat' if parts[3] == 'individual' else 'Group Combat'
