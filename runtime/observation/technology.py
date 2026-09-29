"""Tactic-independent Terran production and technology observation."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any


def _type_name(unit: Any) -> str:
    return getattr(getattr(unit, "type_id", None), "name", "UNKNOWN")


@dataclass(frozen=True)
class TechNode:
    structure: str
    requires: frozenset[str]


TERRAN_TECH_FRONTIER = (
    TechNode(
        "SUPPLYDEPOT",
        frozenset({"COMMANDCENTER"}),
    ),
    TechNode(
        "REFINERY",
        frozenset({"COMMANDCENTER"}),
    ),
    TechNode("BARRACKS", frozenset({"SUPPLYDEPOT"})),
    TechNode("FACTORY", frozenset({"BARRACKS"})),
    TechNode("STARPORT", frozenset({"FACTORY"})),
    TechNode(
        "ENGINEERINGBAY",
        frozenset({"COMMANDCENTER"}),
    ),
    TechNode("GHOSTACADEMY", frozenset({"BARRACKS"})),
    TechNode("ARMORY", frozenset({"FACTORY"})),
    TechNode("FUSIONCORE", frozenset({"STARPORT"})),
    TechNode("SENSORTOWER", frozenset({"ENGINEERINGBAY"})),
    TechNode("MISSILETURRET", frozenset({"ENGINEERINGBAY"})),
)

PRODUCTION_TYPES = frozenset({"BARRACKS", "FACTORY", "STARPORT"})
PRODUCTION_UNIT_TYPES = frozenset(
    {
        "MARINE",
        "REAPER",
        "MARAUDER",
        "GHOST",
        "HELLION",
        "WIDOWMINE",
        "CYCLONE",
        "SIEGETANK",
        "THOR",
        "VIKINGFIGHTER",
        "MEDIVAC",
        "LIBERATOR",
        "BANSHEE",
        "RAVEN",
        "BATTLECRUISER",
    }
)
BASE_TYPES = {"COMMANDCENTER", "ORBITALCOMMAND", "PLANETARYFORTRESS"}


class ProductionTechnologyBuilder:
    """Provide a small heuristic summary not duplicated by entity sections."""

    def build(
        self,
        bot: Any,
        structures: list[Any],
        pending: dict[str, int] | None = None,
    ) -> list[str]:
        ready = [
            item
            for item in structures
            if float(getattr(item, "build_progress", 1.0)) >= 1.0
        ]
        ready_types = {_type_name(item) for item in ready}
        production, research = self._orders(ready)
        if not production:
            production = [
                f"{name}×{count}"
                for name, count in sorted((pending or {}).items())
                if count > 0 and name in PRODUCTION_UNIT_TYPES
            ]
        upgrades = sorted(
            getattr(upgrade, "name", str(upgrade))
            for upgrade in getattr(getattr(bot, "state", None), "upgrades", set())
        )
        technology_steps = self._next_technology(ready_types)
        return [
            "production: " + ("; ".join(production) if production else "[None]"),
            "research: " + ("; ".join(research) if research else "[None]"),
            "upgrade: " + (", ".join(upgrades) if upgrades else "[None]"),
            "Available technology: " + (", ".join(technology_steps) if technology_steps else "[None]"),
        ]

    def _orders(self, structures: list[Any]) -> tuple[list[str], list[str]]:
        production: dict[str, Counter[int]] = defaultdict(Counter)
        research: dict[str, Counter[int]] = defaultdict(Counter)
        for structure in structures:
            for order in getattr(structure, "orders", []):
                name = self._order_name(order)
                if not name:
                    continue
                progress = int(float(getattr(order, "progress", 0.0)) * 100)
                if self._is_research_order(name):
                    research[name][progress] += 1
                elif _type_name(structure) in PRODUCTION_TYPES:
                    unit_name = next(
                        (unit for unit in PRODUCTION_UNIT_TYPES if name.endswith(unit)), name
                    )
                    production[unit_name][progress] += 1
        return self._format_orders(production), self._format_orders(research)

    @staticmethod
    def _format_orders(orders: dict[str, Counter[int]]) -> list[str]:
        return [
            f"{name} " + ",".join(
                f"{progress}%" + (f"×{count}" if count > 1 else "")
                for progress, count in sorted(progresses.items())
            )
            for name, progresses in sorted(orders.items())
        ]

    @staticmethod
    def _next_technology(ready_types: set[str]) -> list[str]:
        # Command Center morphs still satisfy Command Center prerequisites.
        effective = set(ready_types)
        if effective & BASE_TYPES:
            effective.add("COMMANDCENTER")
        candidates = [
            node
            for node in TERRAN_TECH_FRONTIER
            if node.structure not in effective and node.requires.issubset(effective)
        ][:3]
        return [node.structure for node in candidates]

    @staticmethod
    def _order_name(order: Any) -> str:
        ability = getattr(order, "ability", None)
        return str(getattr(ability, "name", "") or "")

    @staticmethod
    def _is_research_order(name: str) -> bool:
        lowered = name.casefold()
        return "research" in lowered or any(
            token in lowered
            for token in ("level ", "weapons", "armor", "plating", "yamato", "stim", "shield")
        )
