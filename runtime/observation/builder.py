"""Ares/python-sc2 state -> one compact model observation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.runtime.observation.action_history import ActionHistory
from agent.runtime.actions.resolution.resolver import EntityContext
from agent.runtime.observation.hints import SituationHintBuilder
from agent.runtime.observation.overview import OverviewBuilder
from agent.runtime.observation.renderer import observation_text
from agent.runtime.observation.state import TagIdMapper
from agent.runtime.observation.technology import ProductionTechnologyBuilder
from agent.runtime.observation.changes import ChangeTracker
from agent.runtime.observation.entities import EntityRenderer, count_entities
from agent.runtime.observation.execution_context import ExecutionContextBuilder

PENDING_NAMES = (
    "COMMANDCENTER",
    "ORBITALCOMMAND",
    "SUPPLYDEPOT",
    "REFINERY",
    "BARRACKS",
    "FACTORY",
    "STARPORT",
    "FUSIONCORE",
    "STARPORTTECHLAB",
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
)


@dataclass
class Observation:
    iteration: int
    counts: dict[str, int]
    text: str
    context: EntityContext


class ObservationBuilder:
    """Build the canonical observation and preserve only useful cross-frame state."""

    def __init__(self, ids: TagIdMapper):
        self.ids = ids
        self.context_builder = ExecutionContextBuilder(ids)
        self.entities = EntityRenderer(ids)
        self.change_tracker = ChangeTracker()
        self.overview_builder = OverviewBuilder()
        self.hint_builder = SituationHintBuilder()
        self.technology_builder = ProductionTechnologyBuilder()
        self.action_history = ActionHistory()

    def record_registered_actions(
        self, actions: list[dict[str, Any]], time: str = "--:--"
    ) -> None:
        for action in actions:
            self.action_history.record(action, time, "accepted")

    def sync_active_actions(
        self, actions: list[dict[str, Any]], time: str = "--:--"
    ) -> None:
        self.action_history.sync_active(actions, time)
        self.entities.sync_active_actions(actions, time)

    def record_failed_actions(
        self, actions: list[Any], time: str = "--:--", reason: str = ""
    ) -> None:
        for action in actions:
            self.action_history.record(action, time, "failed", reason)

    def collect_frame(self, bot: Any) -> None:
        """Collect short-lived facts even when no model observation is due."""
        self.hint_builder.collect_frame(bot)

    def build(self, bot: Any, iteration: int) -> Observation:
        own_units = list(bot.units)
        structures = list(bot.structures)
        enemies = [
            unit
            for unit in bot.enemy_units
            if getattr(unit, "is_visible", True)
            and not getattr(unit, "is_memory", False)
        ]
        remembered_enemies = [
            unit for unit in bot.enemy_units if getattr(unit, "is_memory", False)
        ]
        enemy_structures = [
            unit for unit in bot.enemy_structures if getattr(unit, "is_visible", True)
        ]
        remembered_structures = [
            unit
            for unit in bot.enemy_structures
            if not getattr(unit, "is_visible", True)
        ]
        own_counts = count_entities(own_units)
        structure_counts = count_entities(structures)
        pending = self._pending_counts(bot)
        counts = dict(own_counts)
        counts.update(structure_counts)
        counts.update({f"pending:{name}": value for name, value in pending.items()})

        context = self.execution_context(bot)
        own_unit_blocks = self.entities.own_unit_blocks(own_units, context, bot)
        own_structure_blocks = self.entities.structure_blocks(
            structures, context, bot, own=True
        )
        enemy_unit_blocks = self.entities.enemy_unit_blocks(enemies, context, bot)
        enemy_structure_blocks = self.entities.structure_blocks(
            enemy_structures, context, bot, own=False
        )
        facts = self.change_tracker.facts(own_counts, structures, enemies, enemy_structures, bot)
        overview = self.overview_builder.build(
            bot,
            structures,
            own_units,
            enemies,
            enemy_structures,
        )
        data = {
            "overview": overview,
            "situational_hints": self.hint_builder.build(bot),
            "own_unit_blocks": own_unit_blocks,
            "own_structure_blocks": own_structure_blocks,
            "enemy_unit_blocks": enemy_unit_blocks,
            "enemy_structure_blocks": enemy_structure_blocks,
            "remembered_enemy_unit_blocks": self.entities.remembered_enemy_unit_blocks(
                remembered_enemies, bot
            ),
            "remembered_enemy_structure_blocks": (
                self.entities.remembered_enemy_structure_blocks(remembered_structures, bot)
            ),
            "production_and_technology": self.technology_builder.build(
                bot, structures, pending
            ),
            "action_history": self._action_history_text(),
            "recent_changes": self.change_tracker.recent_changes(facts),
        }
        self.change_tracker.previous_facts = facts
        return Observation(iteration, counts, observation_text(data), context)

    def execution_context(self, bot: Any) -> EntityContext:
        """Build a lightweight current-frame context without rendering a prompt."""
        return self.context_builder.build(bot)

    def _pending_counts(self, bot: Any) -> dict[str, int]:
        from sc2.ids.unit_typeid import UnitTypeId

        pending: dict[str, int] = {}
        for name in PENDING_NAMES:
            if hasattr(UnitTypeId, name):
                pending[name] = int(bot.already_pending(getattr(UnitTypeId, name)))
        return pending

    def _action_history_text(self) -> str:
        return self.action_history.render()
