from sc2.ids.ability_id import AbilityId as A
from sc2.position import Point2

from ..helpers import subjects


async def move(frame, *, tags, target, takeover=False):
    """Move directly to a position, preserving matching orders.

    takeover allows abandoning the unit's current build. True means any supplied
    unit belongs to the group, including arrivals. Use path_to_target for safer routing.
    """
    if target is None:
        return False
    target, handled = Point2(target), False
    for unit in subjects(frame, tags):
        handled = True
        if takeover or (unit.distance_to(target) > 1.5 and not frame.actions.maintained(unit, A.MOVE, target)):
            frame.actions.propose(unit.tag, A.MOVE, target, takeover=takeover)
    return handled
