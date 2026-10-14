"""Persistent control intents for the current decision cycle."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from agent.game.actions.resolution.loader import ActionCatalog
from agent.game.actions.policy.rules import actor_parameter

class PersistentActionRegistry:
    """Store controls until the next model decision is received."""

    def __init__(self, catalog: ActionCatalog):
        self.catalog = catalog
        self._items: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _key(action: dict[str, Any]) -> str:
        return json.dumps(action, sort_keys=True, separators=(",", ":"))

    def _canonical(self, action: dict[str, Any]) -> dict[str, Any]:
        action = deepcopy(action)
        action["id"] = self.catalog.get(action["id"])["name"]
        args = action["args"]
        if "group" in args:
            args["group"] = sorted(set(args["group"]))
        return action

    def actors(self, action: dict[str, Any]) -> set[str]:
        args = action["args"]
        value = args.get(actor_parameter(self.catalog.get(action["id"])))
        if value is None:
            return set()
        return set(map(str, value)) if isinstance(value, list) else {str(value)}

    def is_persistent(self, action: dict[str, Any]) -> bool:
        return self.catalog.get(action["id"])["persistent"]

    def clear(self) -> None:
        self._items.clear()

    @property
    def actions(self) -> list[dict[str, Any]]:
        return list(self._items.values())

    def replace(self, action: dict[str, Any]) -> bool:
        """Replace overlapping tasks, preserving unaffected members of a group."""
        action = self._canonical(action)
        key = self._key(action)
        persistent = self.is_persistent(action)
        if persistent and key in self._items:
            return False
        actors = self.actors(action)
        scope = self.catalog.override_key(action)
        for old_key, old in list(self._items.items()):
            overlap = actors & self.actors(old)
            same_macro = scope is not None and scope == self.catalog.override_key(old)
            if not overlap and not same_macro:
                continue
            self._items.pop(old_key)
            remaining = self.actors(old) - actors
            if overlap and remaining and "group" in old["args"]:
                retained = deepcopy(old)
                retained["args"]["group"] = sorted(remaining)
                self._items[self._key(retained)] = retained
        if persistent:
            self._items[key] = action
        return True

    def discard(self, action: dict[str, Any]) -> None:
        self._items.pop(self._key(self._canonical(action)), None)

    def prune_missing_actors(self, own_entities: dict[str, Any]) -> list[dict[str, Any]]:
        """Drop missing single actors; policy review filters group members."""
        failed = []
        for key, action in list(self._items.items()):
            if "group" in action["args"]:
                continue
            actors = self.actors(action)
            missing = actors - own_entities.keys()
            if not missing:
                continue
            self._items.pop(key)
            failed.append(action)
        return failed
