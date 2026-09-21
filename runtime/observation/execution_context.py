"""Build current-frame entity, landmark and grid mappings."""

from __future__ import annotations

from typing import Any

from agent.runtime.actions.resolution.resolver import EntityContext
from agent.runtime.observation.state import TagIdMapper


def safe_mediator(bot: Any, attribute: str) -> Any:
    try:
        return getattr(bot.mediator, attribute)
    except (AttributeError, KeyError):
        return None



class ExecutionContextBuilder:
    def __init__(self, ids: TagIdMapper):
        self.ids = ids
        self._known_own_aliases: set[str] = set()

    def build(self, bot: Any) -> EntityContext:
        """Build a lightweight current-frame context without rendering a prompt."""
        context = EntityContext(bot=bot, )
        self._add_execution_context(bot, context)
        for entity in list(bot.units) + list(bot.structures):
            context.own_entities[self.ids.alias(entity.tag)] = entity
        for entity in list(bot.enemy_units) + list(bot.enemy_structures):
            if getattr(entity, "is_visible", True) and not getattr(
                entity, "is_memory", False
            ):
                context.enemy_entities[self.ids.alias(entity.tag)] = entity
        self._known_own_aliases.update(context.own_entities)
        context.known_own_aliases = set(self._known_own_aliases)
        return context


    def _add_execution_context(self, bot: Any, context: EntityContext) -> None:
        candidates = {
            "main": getattr(bot, "start_location", None),
            "natural": safe_mediator(bot, "get_own_nat"),
            "enemy_main": (getattr(bot, "enemy_start_locations", []) or [None])[0],
        }
        context.positions.update(
            {name: point for name, point in candidates.items() if point is not None}
        )
        for alias, attr in {
            "ground": "get_ground_grid",
            "air": "get_air_grid",
            "ground_avoidance": "get_ground_avoidance_grid",
            "air_avoidance": "get_air_avoidance_grid",
            "tactical_ground": "get_tactical_ground_grid",
        }.items():
            value = safe_mediator(bot, attr)
            if value is not None:
                context.grids[alias] = value

