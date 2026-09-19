"""Small queue for macro actions blocked only by temporary resource shortages."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from agent.runtime.actions.loader import ActionCatalog


@dataclass(frozen=True)
class DeferredAction:
    action: dict[str, Any]
    queued_iteration: int
    expires_iteration: int


class DeferredActionQueue:
    """Retry selected macro actions without asking the model to repeat them."""

    READY = "ready"
    QUEUED = "queued"
    BLOCKED = "blocked"

    _COSTED_ACTIONS = {
        "macro.build_structure": "structure_id",
        "macro.upgrade_c_cs": "to",
    }

    def __init__(
        self,
        catalog: ActionCatalog,
        ttl_iterations: int,
        mineral_tolerance: int = 120,
        vespene_tolerance: int = 60,
    ):
        self.catalog = catalog
        self.ttl_iterations = ttl_iterations
        self.mineral_tolerance = mineral_tolerance
        self.vespene_tolerance = vespene_tolerance
        self._items: list[DeferredAction] = []

    def should_defer(self, bot: Any, action: dict[str, Any]) -> bool:
        return self.resource_status(bot, action) != self.READY

    def resource_status(self, bot: Any, action: dict[str, Any]) -> str:
        """Return ready, queued, or blocked using a small fixed shortfall."""
        target = self._cost_target(action)
        can_afford = getattr(bot, "can_afford", None)
        if target is None or not callable(can_afford):
            return self.READY
        try:
            if bool(can_afford(target)):
                return self.READY
        except (AttributeError, TypeError, ValueError):
            return self.READY

        calculate_cost = getattr(bot, "calculate_cost", None)
        if not callable(calculate_cost):
            return self.QUEUED
        try:
            cost = calculate_cost(target)
            mineral_shortfall = max(
                0, int(getattr(cost, "minerals", 0)) - int(bot.minerals)
            )
            vespene_shortfall = max(
                0, int(getattr(cost, "vespene", 0)) - int(bot.vespene)
            )
        except (AttributeError, TypeError, ValueError):
            return self.QUEUED
        if (
            mineral_shortfall <= self.mineral_tolerance
            and vespene_shortfall <= self.vespene_tolerance
        ):
            return self.QUEUED
        return self.BLOCKED

    def enqueue(self, action: dict[str, Any], iteration: int) -> bool:
        key = self._key(action)
        if any(self._key(item.action) == key for item in self._items):
            return False
        self._items.append(
            DeferredAction(
                deepcopy(action),
                iteration,
                iteration + self.ttl_iterations,
            )
        )
        return True

    def pop_ready(
        self, bot: Any, iteration: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        ready: list[dict[str, Any]] = []
        expired: list[dict[str, Any]] = []
        remaining: list[DeferredAction] = []
        for item in self._items:
            if self.construction_pending(bot, item.action):
                ready.append(item.action)
            elif iteration > item.expires_iteration:
                expired.append(item.action)
            elif self.should_defer(bot, item.action):
                remaining.append(item)
            else:
                ready.append(item.action)
        self._items = remaining
        return ready, expired

    @property
    def actions(self) -> list[dict[str, Any]]:
        return [item.action for item in self._items]

    def discard(self, action: dict[str, Any]) -> None:
        key = self._key(action)
        self._items = [item for item in self._items if self._key(item.action) != key]

    def construction_pending(self, bot: Any, action: dict[str, Any]) -> bool:
        if self.catalog.get(action["id"])["id"] != "macro.build_structure":
            return False
        pending = getattr(bot, "structure_pending", None)
        return callable(pending) and pending(self._cost_target(action)) > 0

    def _cost_target(self, action: dict[str, Any]) -> Any | None:
        try:
            entry = self.catalog.get(action.get("id"))
            action_id = entry["id"]
            arg_name = self._COSTED_ACTIONS[action_id]
            value = action["args"][arg_name]
        except (KeyError, TypeError):
            return None
        if not isinstance(value, str):
            return None
        from sc2.ids.unit_typeid import UnitTypeId
        from sc2.ids.upgrade_id import UpgradeId

        try:
            return UnitTypeId[value]
        except KeyError:
            try:
                return UpgradeId[value]
            except KeyError:
                return None

    @staticmethod
    def _key(action: dict[str, Any]) -> str:
        return json.dumps(action, sort_keys=True, separators=(",", ":"))
