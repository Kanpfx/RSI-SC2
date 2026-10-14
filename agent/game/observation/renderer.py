"""Canonical model observation renderer."""

from __future__ import annotations

from typing import Any


def _section(name: str, content: str | list[str], *, empty: str = "[None]") -> str:
    if isinstance(content, list):
        content = "\n".join(content)
    return f"# {name}\n{content or empty}"


def observation_text(data: dict[str, Any]) -> str:
    """Render the factual observation read by model."""
    overview = "\n".join(filter(None, (
        data["overview"]["match"],
        data["overview"]["resources"],
        data["overview"]["supply"],
    )))
    return "\n\n".join((
        _section("Overview", overview),
        _section("Own Units", data["own_unit_blocks"]),
        _section("Own Structures", data["own_structure_blocks"]),
        _section("Visible Enemy Units", data["enemy_unit_blocks"], empty="[None visible]"),
        _section("Visible Enemy Structures", data["enemy_structure_blocks"], empty="[None visible]"),
        _section("Remembered Enemy Units", data["remembered_enemy_unit_blocks"]),
        _section("Remembered Enemy Structures", data["remembered_enemy_structure_blocks"]),
        _section("Production & Upgrades", data["production_and_technology"]),
        _section("Action History", data["action_history"]),
    ))


class OverviewBuilder:
    """Render direct high-level facts needed to orient each model call."""

    def build(self, bot: Any) -> dict[str, str]:
        resource_fields = self._numeric_fields(bot, ("minerals", "vespene"))
        score = getattr(getattr(bot, "state", None), "score", None)
        mineral_rate = getattr(score, "collection_rate_minerals", None)
        gas_rate = getattr(score, "collection_rate_vespene", None)
        if isinstance(mineral_rate, (int, float)) and isinstance(gas_rate, (int, float)):
            resource_fields.append(f"income={int(mineral_rate)}m/{int(gas_rate)}g")
        supply_fields = self._numeric_fields(
            bot, ("used", "cap", "workers", "army"), prefix="supply_"
        )
        return {
            "match": self._match(bot),
            "resources": "resources: " + ", ".join(resource_fields) if resource_fields else "",
            "supply": "supply: " + ", ".join(supply_fields) if supply_fields else "",
        }

    @staticmethod
    def _numeric_fields(bot: Any, names: tuple[str, ...], prefix: str = "") -> list[str]:
        return [
            f"{name}={int(value)}"
            for name in names
            if isinstance(value := getattr(bot, prefix + name, None), (int, float))
        ]

    @staticmethod
    def _match(bot: Any) -> str:
        own_race = getattr(getattr(bot, "race", None), "name", "[Unknown]")
        enemy_race = getattr(getattr(bot, "enemy_race", None), "name", "[Unknown]")
        fields = [
            f"time={getattr(bot, 'time_formatted', '--:--')}",
            f"matchup={own_race}vs{enemy_race}",
        ]
        map_size = getattr(getattr(bot, "game_info", None), "map_size", None)
        width = getattr(map_size, "x", getattr(map_size, "width", None))
        height = getattr(map_size, "y", getattr(map_size, "height", None))
        if isinstance(width, (int, float)) and isinstance(height, (int, float)):
            fields.append(f"map_size={int(width)}x{int(height)}")
        return "metadata: " + ", ".join(fields)
