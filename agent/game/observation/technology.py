"""Native production orders, research orders, and completed upgrades."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


class ProductionTechnologyBuilder:
    """Summarize current facts using the game's ability mappings."""

    def build(self, bot: Any, structures: list[Any]) -> list[str]:
        from sc2.data import Attribute
        from sc2.ids.upgrade_id import UpgradeId

        production_abilities = {}
        for data in bot.game_data.units.values():
            ability = data.creation_ability
            if ability is not None and Attribute.Structure.value not in data.attributes:
                production_abilities[ability.exact_id] = data.id.name
        research_abilities = {
            data.research_ability.exact_id: UpgradeId(upgrade_id).name
            for upgrade_id, data in bot.game_data.upgrades.items()
            if data.research_ability is not None
        }
        production: dict[str, Counter[int]] = defaultdict(Counter)
        research: dict[str, Counter[int]] = defaultdict(Counter)
        for structure in structures:
            if structure.build_progress < 1.0:
                continue
            for order in structure.orders:
                ability = order.ability.exact_id
                progress = int(order.progress * 100)
                if ability in research_abilities:
                    research[research_abilities[ability]][progress] += 1
                elif ability in production_abilities:
                    production[production_abilities[ability]][progress] += 1
        upgrades = sorted(upgrade.name for upgrade in bot.state.upgrades)
        return [
            "production: " + ("; ".join(self._format_orders(production)) or "[None]"),
            "research: " + ("; ".join(self._format_orders(research)) or "[None]"),
            "upgrades: " + ("; ".join(upgrades) or "[None]"),
        ]

    @staticmethod
    def _format_orders(orders: dict[str, Counter[int]]) -> list[str]:
        return [
            f"{name}: progress=[" + ", ".join(
                f"{progress}%" + (f"?{count}" if count > 1 else "")
                for progress, count in sorted(progresses.items())
            ) + "]"
            for name, progresses in sorted(orders.items())
        ]
