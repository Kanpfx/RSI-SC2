from sc2.ids.unit_typeid import UnitTypeId as U

from ..helpers import count
from ..production.build_structure import BuildStructure


class BuildGas(BuildStructure):
    async def execute(self, frame, *, target):
        """Recover unfinished refineries or request gas buildings up to the supplied target.

        Choose an unoccupied geyser near a ready base. Reuse local construction state;
        resource priority and the desired refinery count come from the caller.
        """
        game = frame.game
        unfinished = next((b for b in game.gas_buildings if not b.is_ready), None)
        if unfinished is not None:
            return await super().execute(frame, product=U.REFINERY, near=unfinished.position)
        if count(frame, U.REFINERY) >= target:
            return False
        geysers = [g for g in game.vespene_geyser if g.vespene_contents > 0
                   and any(b.is_ready and b.distance_to(g) < 12 for b in game.townhalls)
                   and not any(b.distance_to(g) < 2 for b in game.gas_buildings)]
        geyser = min(geysers, key=lambda g: (g.distance_to(frame.spatial.map.main), g.tag), default=None)
        if geyser is None:
            return False
        return await super().execute(frame, product=U.REFINERY, geyser_tag=geyser.tag)
