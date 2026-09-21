"""Canonical model observation renderer."""

from __future__ import annotations

from typing import Any


def _domain(name: str, content: str) -> str:
    """Render a top-level observation domain as a Markdown heading."""
    return f"# {name}\n{content or '[None]'}"


def _section(name: str, content: str | list[str], *, empty: str = "[None]") -> str:
    if isinstance(content, list):
        content = "\n".join(content) if content else empty
    return f"## {name}\n{content or empty}"


def observation_text(data: dict[str, Any]) -> str:
    """Render the factual observation read by model."""
    hint_sections = data["situational_hints"]
    alert_lines = [
        f"- {item}"
        for items in hint_sections.values()
        for item in items
    ]
    situation_alerts = "alerts: " + " | ".join(dict.fromkeys(line[2:] for line in alert_lines)) if alert_lines else ""
    overview = "\n".join(filter(None, (
        data["overview"]["resources"],
        data["overview"]["economy"],
        data["overview"]["match"],
        data["overview"]["military"],
        situation_alerts,
    )))
    technology = "\n".join(data["production_and_technology"])
    own_state = "\n\n".join(
        (
            _section("units", data["own_unit_blocks"]),
            _section("structures", data["own_structure_blocks"]),
            _section("production_and_technology", technology),
        )
    )
    visible_enemy_state = "\n\n".join(
        (
            _section(
                "visible_units",
                data["enemy_unit_blocks"],
                empty="[None visible]",
            ),
            _section(
                "visible_structures",
                data["enemy_structure_blocks"],
                empty="[None visible]",
            ),
        )
    )
    memory_enemy_state = "\n\n".join(
        (
            _section("Recently seen units", data["remembered_enemy_unit_blocks"]),
            _section("Known structures", data["remembered_enemy_structure_blocks"]),
        )
    )
    recent_history = "\n\n".join(
        (
            _section("state_changes", data["recent_changes"]),
            _section("action_history", data["action_history"]),
        )
    )
    return "\n\n".join(
        (
            _domain("overview", overview),
            _domain("own_state", own_state),
            _domain("visible_enemy_state", visible_enemy_state),
            _domain("memory_enemy_state", memory_enemy_state),
            _domain("recent_history", recent_history),
        )
    )
