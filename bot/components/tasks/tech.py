from sc2.ids.unit_typeid import UnitTypeId as U

from ..behaviors.production.build_structure import BuildStructure
from ..behaviors.production.addon import Addon
from ..behaviors.production.research import Research


class TechTask:
    def __init__(self):
        """Keep one construction attempt per BC tech building plus addon and research capabilities."""
        self.builders = {product: BuildStructure() for product in
                         (U.BARRACKS, U.FACTORY, U.STARPORT, U.FUSIONCORE)}
        self.addon, self.research = Addon(), Research()

    async def execute(self, frame, *, upgrades=()):
        """Advance the BC building chain and independently evaluate addons and upgrades.

        Keep calling the first unfinished building's capability so it can recover.
        Each optional upgrade entry is (upgrade, producer_tags); no prerequisites
        are inserted by Core and no resource affordability gate hides these demands.
        """
        game, handled = frame.game, False
        chain = tuple(self.builders)
        for index, product in enumerate(chain):
            structures = [b for b in game.structures if b.type_id == product]
            if any(b.is_ready for b in structures):
                continue
            # Keep invoking the builder while construction is pending so it can recover.
            handled |= await frame.run(self.builders[product], product=product,
                near=frame.spatial.map.anchor(index*5), addon_space=product != U.FUSIONCORE,
                save_if_unaffordable=True)
            break
        starports = [b.tag for b in game.structures if b.type_id == U.STARPORT]
        handled |= await frame.run(self.addon, product=U.STARPORTTECHLAB,
                                   producers=starports, priority=85, save_if_unaffordable=True)
        for upgrade, producers in upgrades:
            handled |= await frame.run(self.research, upgrade=upgrade, producers=producers)
        return handled
