from ..behaviors.combat.attack_move import AttackMove
from ..behaviors.combat.focus_fire import FocusFire
from ..behaviors.combat.kite import Kite


class DefenseTask:
    def __init__(self):
        """Create independent advance, targeting and kiting capabilities for the assigned group."""
        self.attack, self.focus, self.kite = AttackMove(), FocusFire(), Kite()

    async def execute(self, frame, *, tags, target):
        """Control the Tactics-assigned tags around the supplied defense destination.

        Prefer kite, then focus fire, then advance for each unit. Maintained higher
        priority orders must not be overwritten by fallback movement.
        """
        handled, advance = False, []
        for tag in tags:
            if await frame.run(self.kite, tags=(tag,), priority=80):
                handled = True
            elif await frame.run(self.focus, tags=(tag,), priority=70):
                handled = True
            else:
                advance.append(tag)
        handled |= await frame.run(self.attack, tags=advance, target=target)
        return handled
