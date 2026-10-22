from sc2.ids.ability_id import AbilityId as A

from ..helpers import attack_target, subjects


class FocusFire:
    async def execute(self, frame, *, tags):
        """Request or maintain an in-range attack for each supplied unit tag.

        Targets are chosen independently by effective health. Return True if at least
        one unit has a target; units without targets remain available to other control.
        """
        handled = False
        enemies = frame.game.enemy_units + frame.game.enemy_structures
        for unit in subjects(frame.game, tags):
            target = attack_target(unit, enemies)
            if target is None:
                continue
            if not frame.actions.maintained(unit, A.ATTACK, target.tag):
                frame.actions.propose(unit.tag, A.ATTACK, target.tag)
            handled = True
        return handled
