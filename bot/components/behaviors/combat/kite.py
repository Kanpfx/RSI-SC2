from sc2.ids.ability_id import AbilityId as A

from ..helpers import attack_target, subjects


class Kite:
    async def execute(self, frame, *, tags):
        """Move supplied units toward safer nearby points while their weapons cool down.

        Only consider units with an attackable enemy in range. Return True when at
        least one escape move is proposed or maintained; otherwise allow fallback.
        """
        handled = False
        enemies = frame.game.enemy_units + frame.game.enemy_structures
        for unit in subjects(frame.game, tags):
            enemy = attack_target(unit, enemies)
            if enemy is None or unit.weapon_cooldown <= 0:
                continue
            away = enemy.position.towards(unit.position, enemy.distance_to(unit)+6)
            safe = frame.spatial.pathing.safe_point(unit.position, flying=unit.is_flying,
                                                   radius=4, towards=away)
            if safe is not None and unit.distance_to(safe) > .5:
                if not frame.actions.maintained(unit, A.MOVE, safe):
                    frame.actions.propose(unit.tag, A.MOVE, safe)
                handled = True
        return handled
