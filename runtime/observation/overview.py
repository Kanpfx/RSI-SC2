"""Compact, tactic-independent overview of the current game state."""

from __future__ import annotations

from typing import Any


class OverviewBuilder:
    """Render direct high-level facts needed to orient each model call."""

    def build(self, bot: Any) -> dict[str, str]:
        bases = list(getattr(bot, "townhalls", []))
        resources = (
            f"minerals={int(getattr(bot, 'minerals', 0))}, "
            f"vespene={int(getattr(bot, 'vespene', 0))}"
        )
        score = getattr(getattr(bot, "state", None), "score", None)
        mineral_rate = getattr(score, "collection_rate_minerals", None)
        gas_rate = getattr(score, "collection_rate_vespene", None)
        if isinstance(mineral_rate, (int, float)) and isinstance(gas_rate, (int, float)):
            resources += f", income/min={int(mineral_rate)}m/{int(gas_rate)}g"
        supply = (
            f"supply: used={int(getattr(bot, 'supply_used', 0))}, "
            f"cap={int(getattr(bot, 'supply_cap', 0))}, "
            f"workers={int(getattr(bot, 'supply_workers', 0))}, "
            f"army={int(getattr(bot, 'supply_army', 0))}"
        )
        economy = (
            f"economy: bases={len(bases)}, "
            f"refineries={len(getattr(bot, 'gas_buildings', []))}"
        )
        saturation = self._saturation(bot, bases)
        if saturation:
            economy += ", saturation=" + " ".join(saturation)
        return {
            "match": self._match(bot),
            "resources": resources,
            "supply": supply,
            "economy": economy,
            "military": self._combat_totals(bot),
        }

    @staticmethod
    def _match(bot: Any) -> str:
        own_race = getattr(getattr(bot, "race", None), "name", "Terran")
        enemy_race = getattr(getattr(bot, "enemy_race", None), "name", "[Unknown]")
        fields = [
            f"time={getattr(bot, 'time_formatted', '00:00')}",
            f"matchup={own_race}vs{enemy_race}",
        ]
        map_size = getattr(getattr(bot, "game_info", None), "map_size", None)
        width = getattr(map_size, "x", getattr(map_size, "width", None))
        height = getattr(map_size, "y", getattr(map_size, "height", None))
        if isinstance(width, (int, float)) and isinstance(height, (int, float)):
            fields.append(f"map_size={int(width)}x{int(height)}")
        return ", ".join(fields)

    @staticmethod
    def _is_ready(entity: Any) -> bool:
        return bool(
            getattr(
                entity,
                "is_ready",
                float(getattr(entity, "build_progress", 1.0)) >= 1.0,
            )
        )

    @classmethod
    def _saturation(cls, bot: Any, townhalls: list[Any]) -> list[str]:
        entries: list[str] = []
        ready_bases = [base for base in townhalls if cls._is_ready(base)]
        for index, base in enumerate(ready_bases):
            label = ("main", "natural")[index] if index < 2 else f"base {index + 1}"
            assigned = getattr(base, "assigned_harvesters", None)
            ideal = getattr(base, "ideal_harvesters", None)
            if not isinstance(assigned, int) or not isinstance(ideal, int) or ideal <= 0:
                continue
            entries.append(f"{label} {assigned}/{ideal}")

        gas_buildings = [
            gas
            for gas in getattr(bot, "gas_buildings", [])
            if cls._is_ready(gas)
        ]
        gas_assigned = sum(
            int(getattr(gas, "assigned_harvesters", 0)) for gas in gas_buildings
        )
        gas_ideal = sum(
            int(getattr(gas, "ideal_harvesters", 0)) for gas in gas_buildings
        )
        if gas_ideal:
            entries.append(f"refineries {gas_assigned}/{gas_ideal}")
        return entries

    @staticmethod
    def _combat_totals(bot: Any) -> str:
        score = getattr(getattr(bot, "state", None), "score", None)
        names = (
            "killed_value_units",
            "killed_value_structures",
            "lost_minerals_army",
            "lost_vespene_army",
        )
        values = [getattr(score, name, None) for name in names]
        if not all(isinstance(value, (int, float)) for value in values):
            return ""
        killed_units, killed_structures, lost_minerals, lost_vespene = map(
            int, values
        )
        return (
            f"combat: killed_value={killed_units} units {killed_structures} structures, "
            f"army_lost={lost_minerals}m/{lost_vespene}g"
        )
