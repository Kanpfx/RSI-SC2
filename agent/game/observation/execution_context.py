"""Build current-frame entity and landmark mappings."""

from __future__ import annotations

from itertools import chain
from typing import Any

from agent.game.actions.resolution.resolver import EntityContext


class TagIdMapper:
    """Compact IDs that are never reused after a unit dies."""

    def __init__(self) -> None:
        self._tag_to_id: dict[int, int] = {}
        self._next_id = 0

    def alias(self, tag: int) -> str:
        if tag not in self._tag_to_id:
            self._tag_to_id[tag] = self._next_id
            self._next_id += 1
        return str(self._tag_to_id[tag])


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
        context = EntityContext(bot=bot)
        self._add_execution_context(bot, context)
        for entity in chain(bot.units, bot.structures):
            context.own_entities[self.ids.alias(entity.tag)] = entity
        for entity in chain(bot.enemy_units, bot.enemy_structures):
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
            "enemy_natural": safe_mediator(bot, "get_enemy_nat"),
        }
        context.positions.update(
            {name: point for name, point in candidates.items() if point is not None}
        )

