"""Harvesting, supply, worker scouting and requested worker targets."""
from __future__ import annotations

from typing import Any
from ares.behaviors.macro import AutoSupply, BuildWorkers
from ares.behaviors.macro.mining import Mining
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from agent.runtime.scouting import ScoutController


class AutomationController:
    def __init__(self) -> None:
        self.worker_target: int | None = None
        self._worker_override: dict[str, Any] | None = None
        self.scouting = ScoutController()

    async def run(self, bot: Any, iteration: int) -> None:
        self.scouting.run(bot)
        bot.register_behavior(Mining(workers_per_gas=3))
        bot.register_behavior(AutoSupply(base_location=bot.start_location))
        if iteration % 16 == 0:
            for depot in bot.mediator.get_own_structures_dict.get(UnitTypeId.SUPPLYDEPOT, []):
                if depot.is_ready and depot.type_id == UnitTypeId.SUPPLYDEPOT:
                    depot(AbilityId.MORPH_SUPPLYDEPOT_LOWER)

    def replace_worker_override(self, action: dict[str, Any] | None) -> tuple[dict[str, Any] | None, bool]:
        previous = self._worker_override
        if action is None:
            return previous, False
        self._worker_override = action
        self.worker_target = int(action["args"]["to_count"])
        return previous, previous != action

    @property
    def worker_action(self) -> dict[str, Any] | None:
        return self._worker_override

    def register_worker_production(self, bot: Any) -> None:
        if self.worker_target is not None:
            bot.register_behavior(BuildWorkers(to_count=self.worker_target))
