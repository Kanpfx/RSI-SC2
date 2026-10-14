"""Ares/python-sc2 state -> one compact model observation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.game.actions.resolution.resolver import EntityContext
from agent.game.observation.action_history import ActionHistory
from agent.game.observation.entities import EntityRenderer
from agent.game.observation.execution_context import ExecutionContextBuilder, TagIdMapper
from agent.game.observation.renderer import OverviewBuilder, observation_text
from agent.game.observation.technology import ProductionTechnologyBuilder


@dataclass
class Observation:
    iteration: int
    text: str
    context: EntityContext


class ObservationBuilder:
    """Build the canonical observation and preserve only useful cross-frame state."""

    def __init__(self, ids: TagIdMapper):
        self.context_builder = ExecutionContextBuilder(ids)
        self.entities = EntityRenderer(ids)
        self.overview_builder = OverviewBuilder()
        self.technology_builder = ProductionTechnologyBuilder()
        self.action_history = ActionHistory()

    def record_submitted_actions(
        self, actions: list[dict[str, Any]], time: str = "--:--"
    ) -> None:
        for action in actions:
            self.action_history.record(action, time, "submitted")

    def sync_active_actions(
        self, actions: list[dict[str, Any]], time: str = "--:--"
    ) -> None:
        self.action_history.sync_active(actions, time)
        self.entities.sync_active_actions(actions)

    def build(self, bot: Any, iteration: int, *, history_cycles: int = 2) -> Observation:
        own_units = list(bot.units)
        structures = list(bot.structures)
        enemies, remembered_enemies = [], []
        for unit in bot.enemy_units:
            if getattr(unit, "is_memory", False):
                remembered_enemies.append(unit)
            elif getattr(unit, "is_visible", True):
                enemies.append(unit)
        enemy_structures, remembered_structures = [], []
        for structure in bot.enemy_structures:
            if getattr(structure, "is_visible", True) and not getattr(structure, "is_memory", False):
                enemy_structures.append(structure)
            else:
                remembered_structures.append(structure)
        context = self.execution_context(bot)
        data = {
            "overview": self.overview_builder.build(bot),
            "own_unit_blocks": self.entities.own_unit_blocks(own_units, bot),
            "own_structure_blocks": self.entities.structure_blocks(structures, bot, own=True),
            "enemy_unit_blocks": self.entities.enemy_unit_blocks(enemies, bot),
            "enemy_structure_blocks": self.entities.structure_blocks(enemy_structures, bot, own=False),
            "remembered_enemy_unit_blocks": self.entities.remembered_enemy_unit_blocks(
                remembered_enemies, bot
            ),
            "remembered_enemy_structure_blocks": self.entities.remembered_enemy_structure_blocks(
                remembered_structures, bot
            ),
            "production_and_technology": self.technology_builder.build(bot, structures),
            "action_history": self.action_history.render(history_cycles),
        }
        return Observation(iteration, observation_text(data), context)

    def execution_context(self, bot: Any) -> EntityContext:
        """Refresh visible building memory and build the current-frame context."""
        self.entities.update_structure_memory(bot)
        return self.context_builder.build(bot)

