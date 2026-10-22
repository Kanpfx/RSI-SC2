from sc2.ids.ability_id import AbilityId as A
from sc2.position import Point2

from ..helpers import subjects


class Move:
    async def execute(self, frame, *, tags, target):
        """Move supplied unit tags directly to a world position, preserving matching orders.

        Return True for any living subject, including units already near the target.
        No path safety is evaluated; use PathToTarget when threat-aware routing is needed.
        """
        if target is None:
            return False
        target, handled = Point2(target), False
        for unit in subjects(frame.game, tags):
            handled = True
            if unit.distance_to(target) > 1.5 and not frame.actions.maintained(unit, A.MOVE, target):
                frame.actions.propose(unit.tag, A.MOVE, target)
        return handled
