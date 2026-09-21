"""Keep accepted control intents until replaced or invalidated."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from agent.runtime.actions.resolution.loader import ActionCatalog

PERSISTENT_ACTION_IDS = {
    "combat.bc.move_safely",
    "combat.individual.keep_unit_safe",
    "combat.individual.medivac_heal",
    "combat.individual.move_to_safe_target",
    "combat.individual.path_unit_to_target",
    "combat.individual.pick_up_and_drop_cargo",
    "combat.individual.pick_up_cargo",
    "combat.individual.reaper_grenade",
    "combat.individual.shoot_and_move_to_target",
    "combat.individual.siege_tank_decision",
    "combat.individual.stutter_unit_back",
    "combat.individual.stutter_unit_forward",
    "combat.individual.worker_kite_back",
    "combat.group.keep_group_safe",
    "combat.group.path_group_to_target",
    "combat.group.stutter_group_back",
    "combat.group.stutter_group_forward",
    "macro.expansion_controller",
    "macro.gas_building_controller",
    "macro.production_controller",
    "macro.spawn_controller",
    "macro.upgrade_c_cs",
    "macro.upgrade_controller",
}


class PersistentActionRegistry:
    """Store global macro targets and per-unit tasks without a renewal deadline."""

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

    @staticmethod
    def actors(action: dict[str, Any]) -> set[str]:
        args = action["args"]
        if "unit" in args:
            return {str(args["unit"])}
        return set(args.get("group", []))

    def is_persistent(self, action: dict[str, Any]) -> bool:
        return self.catalog.get(action["id"])["id"] in PERSISTENT_ACTION_IDS

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
        for old_key, old in list(self._items.items()):
            overlap = actors & self.actors(old)
            same_macro = not actors and old["id"] == action["id"]
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
        """Drop missing units while retaining live members of group controls."""
        failed = []
        for key, action in list(self._items.items()):
            actors = self.actors(action)
            missing = actors - own_entities.keys()
            if not missing:
                continue
            self._items.pop(key)
            remaining = actors - missing
            if remaining and "group" in action["args"]:
                retained = deepcopy(action)
                retained["args"]["group"] = sorted(remaining)
                self._items[self._key(retained)] = retained
            failed.append(action)
        return failed
