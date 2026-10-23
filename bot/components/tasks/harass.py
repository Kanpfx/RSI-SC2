from sc2.ids.ability_id import AbilityId as A

from ..behaviors.helpers import subjects
from ..behaviors.combat.attack_move import AttackMove
from ..behaviors.combat.focus_fire import FocusFire
from ..behaviors.combat.retreat import Retreat
from ..behaviors.combat.tactical_jump import TacticalJump


class HarassTask:
    def __init__(self):
        """Create advance, targeting, jump and retreat capabilities for the harassment group."""
        self.attack, self.focus = AttackMove(), FocusFire()
        self.jump, self.retreat = TacticalJump(), Retreat()

    async def execute(self, frame, *, tags, target, retreat_target, retreat_health=.3):
        """Partition assigned tags into advancing, jumping and retreating groups.

        Use retreat_health as a health fraction and retreat_target as the escape base.
        Fallback is decided per unit before group calls; priority favors escape over
        attacks. Return whether any capability handles control, not mission completion.
        """
        advance, jump, retreat = [], [], []
        for unit in subjects(frame.game, tags):
            if unit.health >= unit.health_max * retreat_health or unit.distance_to(retreat_target) < 12:
                advance.append(unit.tag)
            elif (any(o.ability.id == A.EFFECT_TACTICALJUMP for o in unit.orders)
                  or A.EFFECT_TACTICALJUMP in await frame.actions.available(unit)):
                jump.append(unit.tag)
            else:
                retreat.append(unit.tag)
        handled = await frame.run(self.jump, tags=jump, target=retreat_target, priority=110)
        handled |= await frame.run(self.retreat, tags=retreat, target=retreat_target, priority=105)
        moving = []
        for tag in advance:
            if await frame.run(self.focus, tags=(tag,), priority=70):
                handled = True
            else:
                moving.append(tag)
        handled |= await frame.run(self.attack, tags=moving, target=target)
        return handled
