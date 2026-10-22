from ..behaviors.combat.attack_move import AttackMove
from ..behaviors.combat.focus_fire import FocusFire
from ..behaviors.combat.kite import Kite


class DefenseTask:
    def __init__(self):
        """Create independent advance, targeting and kiting capabilities for the assigned group."""
        self.attack, self.focus, self.kite = AttackMove(), FocusFire(), Kite()

    async def execute(self, frame, *, tags, target):
        # Evaluate every group; arbitration resolves overlapping local controls.
        """Control the Tactics-assigned tags around the supplied defense destination.

        Evaluate advance, focus fire and kite for the group; their explicit priorities
        resolve overlapping requests in Actions. Do not reassign strategic ownership.
        """
        handled = await frame.run(self.attack, tags=tags, target=target)
        handled |= await frame.run(self.focus, tags=tags, priority=70)
        handled |= await frame.run(self.kite, tags=tags, priority=80)
        return handled
