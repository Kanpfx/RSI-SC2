"""Native production orders, research orders, and completed upgrades."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


class ProductionTechnologyBuilder:
    """Summarize current facts using the game's ability mappings."""

    def __init__(self) -> None:
        self._production_abilities: dict[Any, str] | None = None
        self._research_abilities: dict[Any, str] = {}

    def _load_abilities(self, game_data: Any) -> None:
        from sc2.data import Attribute
        from sc2.ids.upgrade_id import UpgradeId

        production_abilities = {}
        for data in game_data.units.values():
            ability = data.creation_ability
            if ability is not None and Attribute.Structure.value not in data.attributes:
                production_abilities[ability.exact_id] = data.id.name
        self._production_abilities = production_abilities
        self._research_abilities = {
            ability.exact_id: UpgradeId(upgrade_id).name
            for upgrade_id, data in game_data.upgrades.items()
            if (ability := data.research_ability) is not None
        }

    def build(self, bot: Any, structures: list[Any]) -> list[str]:
        if self._production_abilities is None:
            self._load_abilities(bot.game_data)
        production: dict[str, list[int]] = defaultdict(list)
        research: dict[str, list[int]] = defaultdict(list)
        for structure in structures:
            if structure.build_progress < 1.0:
                continue
            for order in structure.orders:
                ability = order.ability.exact_id
                progress = int(order.progress * 100)
                if ability in self._research_abilities:
                    research[self._research_abilities[ability]].append(progress)
                elif ability in self._production_abilities:
                    production[self._production_abilities[ability]].append(progress)
        upgrades = sorted(upgrade.name for upgrade in bot.state.upgrades)
        return [
            "producting:\n" + ("\n".join(self._format_orders(production)) or "[None]"),
            "researching:\n" + ("\n".join(self._format_orders(research)) or "[None]"),
            "upgraded:\n" + ("; ".join(upgrades) or "[None]"),
        ]

    @staticmethod
    def _format_orders(orders: dict[str, list[int]]) -> list[str]:
        return [
            f"- {name}=[" + ", ".join(
                f"{progress}%" for progress in sorted(progresses)
            ) + "]"
            for name, progresses in sorted(orders.items())
        ]
