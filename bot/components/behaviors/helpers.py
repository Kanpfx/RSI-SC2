"""Pure helpers shared by local capabilities."""


def subjects(game, tags):
    """Resolve living own units and structures from tags; silently ignore missing tags."""
    tags = set(tags)
    return tuple(u for u in game.units + game.structures if u.tag in tags)


def count(frame, product):
    """Count ready units, native pending production and unobserved accepted requests."""
    game = frame.game
    ready = sum(u.type_id == product and u.is_ready for u in game.units + game.structures)
    return ready + int(game.already_pending(product)) + frame.actions.pending_product(product)


def attack_target(unit, enemies):
    """Choose the lowest health-plus-shield visible enemy in this unit's weapon range.

    Respect air/ground targeting and radii; return None when no target is attackable.
    """
    targets = [enemy for enemy in enemies if enemy.is_visible
               and (unit.can_attack_air if enemy.is_flying else unit.can_attack_ground)
               and unit.distance_to(enemy) <= (unit.air_range if enemy.is_flying else unit.ground_range)
                   + unit.radius + enemy.radius]
    return min(targets, key=lambda enemy: (enemy.health + enemy.shield, enemy.tag), default=None)
