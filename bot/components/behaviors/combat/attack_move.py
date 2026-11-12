from sc2.ids.ability_id import AbilityId as A
from sc2.position import Point2

from ..helpers import subjects


async def attack_move(frame, *, tags, target):
    """Attack-move supplied units toward a position, preserving matching orders.

    The engine handles movement and target acquisition. True means any supplied
    unit belongs to the current group, including units already at the destination.
    """
    if target is None:
        return False
    target, handled = Point2(target), False
    for unit in subjects(frame, tags):
        handled = True
        if unit.distance_to(target) > 1.5 and not frame.actions.maintained(unit, A.ATTACK, target):
            frame.actions.propose(unit.tag, A.ATTACK, target)
    return handled
