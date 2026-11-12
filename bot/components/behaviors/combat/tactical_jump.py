from sc2.ids.ability_id import AbilityId as A
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from ..helpers import subjects


async def tactical_jump(frame, *, tags, target):
    """Maintain active BC jumps or propose available jumps to the target.

    Skip other unit types and preserve jumps already in progress. True means any
    BC is jumping or has a jump request; the tactic must handle units unable to jump.
    """
    handled = False
    for unit in subjects(frame, tags):
        if unit.type_id != U.BATTLECRUISER:
            continue
        if any(o.ability.id == A.EFFECT_TACTICALJUMP for o in unit.orders):
            handled = True
        elif A.EFFECT_TACTICALJUMP in await frame.actions.available(unit):
            frame.actions.propose(unit.tag, A.EFFECT_TACTICALJUMP, Point2(target))
            handled = True
    return handled
