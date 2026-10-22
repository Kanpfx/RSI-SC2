from sc2.ids.unit_typeid import UnitTypeId as U

from ..production.train_units import TrainUnits


class BuildWorkers(TrainUnits):
    async def execute(self, frame, *, target, owner="economy"):
        """Request SCVs up to target from townhalls assigned to owner.

        Reuse training feasibility checks and inherited priority. Return True when
        requests are proposed, even if the current mineral budget cannot fund them.
        """
        producers = [b.tag for b in frame.game.townhalls if frame.units.owners.get(b.tag) == owner]
        return await super().execute(frame, product=U.SCV, producers=producers, target=target)
