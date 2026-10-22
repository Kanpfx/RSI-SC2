from sc2.ids.ability_id import AbilityId as A
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from ..helpers import subjects


class TacticalJump:
    async def execute(self, frame, *, tags, target):
        """Request jumps for supplied BC tags with the ability available, or maintain active jumps.

        Return True if any BC is handled. The Task must route other units to its
        fallback; this group result does not imply every subject can jump.
        """
        handled = False
        for unit in subjects(frame.game, tags):
            if unit.type_id != U.BATTLECRUISER:
                continue
            if any(o.ability.id == A.EFFECT_TACTICALJUMP for o in unit.orders):
                handled = True
            elif A.EFFECT_TACTICALJUMP in await frame.actions.available(unit):
                frame.actions.propose(unit.tag, A.EFFECT_TACTICALJUMP, Point2(target))
                handled = True
        return handled
