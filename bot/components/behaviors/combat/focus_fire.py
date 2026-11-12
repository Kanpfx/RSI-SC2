from sc2.ids.ability_id import AbilityId as A

from ..helpers import attack_target, subjects


async def focus_fire(frame, *, tags):
    """Request or maintain an in-range attack for each supplied unit tag.

    Each unit picks a visible, attackable target with the lowest health plus shield
    in its own range. True means any unit has a target; others need a fallback.
    """
    handled = False
    enemies = frame.game.enemy_units + frame.game.enemy_structures
    for unit in subjects(frame, tags):
        target = attack_target(unit, enemies)
        if target is None:
            continue
        if not frame.actions.maintained(unit, A.ATTACK, target.tag):
            frame.actions.propose(unit.tag, A.ATTACK, target.tag)
        handled = True
    return handled
