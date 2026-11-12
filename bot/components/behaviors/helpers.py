"""Small observation helpers; all actor lookups use the frame's group and index."""

from sc2.constants import EQUIVALENTS_FOR_TECH_PROGRESS


def subjects(frame, tags):
    """Select living supplied tags owned by the current behavior's group."""
    return frame.units.select(frame.group, tags)


def count(frame, product):
    """Count own ready equivalent forms, native pending units and local submissions.

    The total covers all owned groups. Native observations replace local submissions
    once confirmed."""
    types = {product} | EQUIVALENTS_FOR_TECH_PROGRESS.get(product, set())
    ready = sum(u.type_id in types and u.is_ready for u in frame.units.own.values())
    return ready + int(frame.game.already_pending(product)) + frame.actions.pending_product(product)


def attack_target(unit, enemies):
    """Choose a visible, attackable target within the unit's ground/air range.

    Prefer the lowest combined health and shield, then the lowest tag for stability.
    """
    targets = [enemy for enemy in enemies if enemy.is_visible and enemy.can_be_attacked
               and (unit.can_attack_air if enemy.is_flying else unit.can_attack_ground)
               and unit.distance_to(enemy) <= (unit.air_range if enemy.is_flying else unit.ground_range)
                   + unit.radius + enemy.radius]
    return min(targets, key=lambda enemy: (enemy.health + enemy.shield, enemy.tag), default=None)
