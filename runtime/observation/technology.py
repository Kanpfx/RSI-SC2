"""Tactic-independent Terran production and technology observation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _type_name(unit: Any) -> str:
    return getattr(getattr(unit, "type_id", None), "name", "UNKNOWN")


DISPLAY_NAMES = {
    "SCV": "SCV",
    "MULE": "MULE",
    "SIEGETANK": "Siege Tank",
    "SIEGETANKSIEGED": "Siege Tank",
    "WIDOWMINE": "Widow Mine",
    "WIDOWMINEBURROWED": "Widow Mine",
    "BARRACKSREACTOR": "Barracks Reactor",
    "BARRACKSTECHLAB": "Barracks Tech Lab",
    "COMMANDCENTER": "Command Center",
    "ENGINEERINGBAY": "Engineering Bay",
    "FACTORYREACTOR": "Factory Reactor",
    "FACTORYTECHLAB": "Factory Tech Lab",
    "FUSIONCORE": "Fusion Core",
    "GHOSTACADEMY": "Ghost Academy",
    "HELLIONTANK": "Hellbat",
    "MISSILETURRET": "Missile Turret",
    "ORBITALCOMMAND": "Orbital Command",
    "PLANETARYFORTRESS": "Planetary Fortress",
    "SENSORTOWER": "Sensor Tower",
    "STARPORTREACTOR": "Starport Reactor",
    "STARPORTTECHLAB": "Starport Tech Lab",
    "SUPPLYDEPOT": "Supply Depot",
    "VIKINGFIGHTER": "Viking",
}


def display_name(name: str, count: int = 1) -> str:
    value = DISPLAY_NAMES.get(name, name.replace("_", " ").title())
    if count != 1 and value not in {"Barracks"}:
        value = {"Refinery": "Refineries"}.get(value, value + "s")
    return value


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
                f"{count} {display_name(name, count)}"
                for name, count in sorted((pending or {}).items())
                if count > 0 and name in PRODUCTION_UNIT_TYPES
            ]
        upgrades = sorted(
            getattr(upgrade, "name", str(upgrade)).replace("_", " ").title()
            for upgrade in getattr(getattr(bot, "state", None), "upgrades", set())
        )
        technology_steps = self._next_technology(ready_types)
        return [
            "Production in progress: "
            + ("; ".join(production) if production else "[None]")
            + ".",
            "Research in progress: "
            + ("; ".join(research) if research else "[None]")
            + ".",
            "Completed upgrades: "
            + (", ".join(upgrades) if upgrades else "[None]")
            + ".",
            "Available technology steps: "
            + (", ".join(technology_steps) if technology_steps else "[None]")
            + ".",
        ]

    def _orders(self, structures: list[Any]) -> tuple[list[str], list[str]]:
        production: list[str] = []
        research: list[str] = []
        for structure in structures:
            for order in getattr(structure, "orders", []):
                name = self._order_name(order)
                if not name:
                    continue
                progress = int(float(getattr(order, "progress", 0.0)) * 100)
                if self._is_research_order(name):
                    research.append(f"{name} at {progress}%")
                elif _type_name(structure) in PRODUCTION_TYPES:
                    production.append(f"{name} at {progress}%")
        return production, research

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
        return [display_name(node.structure) for node in candidates]

    @staticmethod
    def _order_name(order: Any) -> str:
        ability = getattr(order, "ability", None)
        name = getattr(ability, "friendly_name", getattr(ability, "name", ""))
        if not name:
            return ""
        text = str(name)
        for prefix in ("Train ", "Research ", "Upgrade to "):
            if text.startswith(prefix):
                text = text[len(prefix) :]
        return text.replace("_", " ").strip()

    @staticmethod
    def _is_research_order(name: str) -> bool:
        lowered = name.casefold()
        return any(
            token in lowered
            for token in ("level ", "weapons", "armor", "plating", "yamato", "stim", "shield")
        )
