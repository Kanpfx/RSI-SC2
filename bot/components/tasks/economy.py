from sc2.ids.unit_typeid import UnitTypeId as U

from ..behaviors.economy.mining import Mining
from ..behaviors.economy.build_workers import BuildWorkers
from ..behaviors.economy.auto_supply import AutoSupply
from ..behaviors.economy.build_gas import BuildGas
from ..behaviors.helpers import count
from ..behaviors.production.build_structure import BuildStructure


class EconomyTask:
    def __init__(self):
        """Keep economic capabilities and the expansion builder's cross-frame state."""
        self.mining, self.workers = Mining(), BuildWorkers()
        self.supply, self.gas, self.expansion = AutoSupply(), BuildGas(), BuildStructure()

    async def execute(self, frame, *, worker_target, gas_target, base_target, saving_bc=False):
        """Translate worker, gas and base targets into concurrent economic capabilities.

        Set local request priorities, reduce worker priority while saving for BCs,
        and select the next expansion. Evaluate every active duty; Actions alone
        allocates spending and protects accepted construction workers.
        """
        handled = await frame.run(self.mining, priority=20)
        handled |= await frame.run(self.workers, target=worker_target, priority=30 if saving_bc else 60)
        handled |= await frame.run(self.supply, margin=8 if frame.game.supply_cap >= 31 else 4, priority=100)
        has_barracks = count(frame, U.BARRACKS) > 0
        handled |= await frame.run(self.gas, target=gas_target, priority=90,
                                  enabled=has_barracks, skip_reason="barracks_missing")
        expand = len(frame.game.townhalls) + int(frame.game.already_pending(U.COMMANDCENTER)) < base_target
        unfinished = any(not b.is_ready for b in frame.game.townhalls)
        sites = [p for p in frame.spatial.map.expansions
                 if all(base.distance_to(p) > 12 for base in frame.game.townhalls)]
        site = min(sites, key=lambda p: p.distance_to(frame.spatial.map.main),
                   default=frame.spatial.map.main if unfinished else None)
        handled |= await frame.run(self.expansion, product=U.COMMANDCENTER, near=site, exact=True,
            priority=40, enabled=(expand or unfinished) and site is not None,
            skip_reason="base_target_met_or_no_site")
        return handled
