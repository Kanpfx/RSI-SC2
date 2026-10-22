from sc2.ids.ability_id import AbilityId as A
from sc2.position import Point2

from ..helpers import subjects


class AttackMove:
    async def execute(self, frame, *, tags, target):
        """Attack-move the supplied unit tags toward a world position without restarting orders.

        Return True for any living subject, including units already at the destination.
        Requests inherit the caller's priority and still require arbitration.
        """
        if target is None:
            return False
        target, handled = Point2(target), False
        for unit in subjects(frame.game, tags):
            handled = True
            if unit.distance_to(target) > 1.5 and not frame.actions.maintained(unit, A.ATTACK, target):
                frame.actions.propose(unit.tag, A.ATTACK, target)
        return handled
