from sc2.ids.ability_id import AbilityId as A

from ..helpers import attack_target, subjects


async def kite(frame, *, tags):
    """Move toward safer nearby points while weapons cool down.

    Require an attackable enemy in range and a connected local point worth moving
    to. True means any move is proposed or maintained; otherwise allow fallback.
    """
    handled = False
    enemies = frame.game.enemy_units + frame.game.enemy_structures
    for unit in subjects(frame, tags):
        enemy = attack_target(unit, enemies)
        if enemy is None or unit.weapon_cooldown <= 0:
            continue
        away = enemy.position.towards(unit.position, enemy.distance_to(unit) + 6)
        safe = frame.spatial.pathing.safe_point(unit.position, flying=unit.is_flying,
                                               radius=4, towards=away)
        if safe is not None and unit.distance_to(safe) > .5:
            if not frame.actions.maintained(unit, A.MOVE, safe):
                frame.actions.propose(unit.tag, A.MOVE, safe)
            handled = True
    return handled
