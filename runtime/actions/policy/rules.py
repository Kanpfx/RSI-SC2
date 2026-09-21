"""Shared actor capabilities, target constraints and readiness checks."""

from __future__ import annotations

from typing import Any, Iterable

from agent.runtime.actions.errors import AvailabilityError, ParameterError
from agent.runtime.actions.resolution.resolver import EntityContext

ENEMY_PARAMS_BY_ACTION = {
    "combat.individual.attack_target": ("target",),
    "combat.individual.ghost_snipe": ("close_enemy",),
    "combat.individual.place_predictive_ao_e": ("enemy_center_unit",),
    "combat.individual.raven_auto_turret": ("all_close_enemy",),
    "combat.individual.reaper_grenade": ("enemy_units",),
    "combat.individual.shoot_and_move_to_target": ("enemy_units",),
    "combat.individual.shoot_target_in_range": ("targets",),
    "combat.individual.siege_tank_decision": ("close_enemy",),
    "combat.individual.stutter_unit_back": ("target",),
    "combat.individual.stutter_unit_forward": ("target",),
    "combat.individual.use_a_o_e_ability": ("targets",),
    "combat.individual.worker_kite_back": ("target",),
    "combat.group.keep_group_safe": ("close_enemy",),
    "combat.group.stutter_group_forward": ("enemies",),
}

ALLY_PARAMS_BY_ACTION = {
    "combat.individual.medivac_heal": ("close_allied",),
    "combat.individual.pick_up_and_drop_cargo": ("pickup_targets",),
    "combat.individual.pick_up_cargo": ("pickup_targets",),
    "combat.individual.use_transfuse": ("targets",),
}


class ActionSurfaceError(AvailabilityError):
    pass


def availability_types(entry: dict[str, Any]) -> set[str]:
    actor_types = set(entry["availability"]["types"])
    return set() if actor_types == {"ALL"} else actor_types


def ability_ready(unit: Any, ability_name: str) -> bool:
    abilities: Iterable[Any] | None = getattr(unit, "abilities", None)
    if abilities is None:
        return False
    return ability_name in {
        getattr(ability, "name", str(ability)) for ability in abilities
    }


# Static capabilities belong to code; conditions in the JSON remain explanatory text.
MOVING_ACTIONS = {
    'MoveSafely', 'AMove', 'AMoveGroup', 'KeepUnitSafe', 'KeepGroupSafe',
    'MoveToSafeTarget', 'PathUnitToTarget', 'PathGroupToTarget', 'NydusPathUnitToTarget',
    'PickUpAndDropCargo', 'PickUpCargo', 'ShootAndMoveToTarget',
    'StutterUnitBack', 'StutterUnitForward', 'StutterGroupBack', 'StutterGroupForward',
    'WorkerKiteBack', 'ReaperGrenade',
}
ATTACK_ACTIONS = {'AttackTarget', 'ShootTargetInRange', 'ShootAndMoveToTarget',
                  'StutterUnitBack', 'StutterUnitForward', 'WorkerKiteBack'}
CAST_ABILITIES = {'GhostSnipe': 'EFFECT_GHOSTSNIPE', 'RavenAutoTurret': 'BUILDAUTOTURRET_AUTOTURRET',
                  'UseTransfuse': 'TRANSFUSION_TRANSFUSION', 'TumorSpreadCreep': 'BUILD_CREEPTUMOR_TUMOR'}


def actor_supported(entry: dict[str, Any], unit: Any, *, ready: bool = True) -> bool:
    name = entry['name']
    if name in MOVING_ACTIONS:
        if getattr(unit, 'is_structure', False) and not getattr(unit, 'is_flying', False):
            return False
        speed = getattr(unit, 'movement_speed', None)
        if speed is not None and speed <= 0:
            return False
    if name in ATTACK_ACTIONS and getattr(unit, 'can_attack', True) is False:
        return False
    if name == 'NydusPathUnitToTarget' and getattr(unit, 'is_flying', False):
        return False
    if ready and name in CAST_ABILITIES:
        return ability_ready(unit, CAST_ABILITIES[name])
    if ready and name == 'AutoUseAOEAbility':
        from ares.dicts.aoe_ability_to_range import AOE_ABILITY_SPELLS_INFO
        return any(ability_ready(unit, a.name) for a in AOE_ABILITY_SPELLS_INFO)
    return True


def validate_resolved(entry: dict[str, Any], args: dict[str, Any], context: EntityContext,
                      *, ready: bool = True) -> set[str]:
    """Validate actor ownership/capabilities and target sides using resolved objects."""
    actor_param = entry['availability']['param']
    actors = args.get(actor_param) if actor_param else None
    if actors is None:
        units = []
    elif hasattr(actors, 'tag'):
        units = [actors]
    elif hasattr(actors, 'name'):  # UnitTypeId alternative, e.g. AddonSwap.
        units = [u for u in context.own_entities.values() if u.type_id == actors]
        if not units:
            raise ParameterError.invalid_value(actor_param, actors.name, 'an available own structure type')
    else:
        units = list(actors)
    allowed = availability_types(entry)
    own_tags = {u.tag for u in context.own_entities.values()}
    actor_tags = set()
    for u in units:
        if u.tag not in own_tags:
            raise ParameterError.invalid_value(actor_param, u.tag, 'an own actor')
        if allowed and u.type_id.name not in allowed:
            raise ParameterError.invalid_value(actor_param, u.type_id.name, str(sorted(allowed)))
        if not actor_supported(entry, u, ready=ready):
            raise ActionSurfaceError('actor cannot execute', f"{u.tag}: {entry['name']}")
        if str(u.tag) in actor_tags:
            raise ParameterError.invalid_value(actor_param, u.tag, 'distinct actor IDs')
        actor_tags.add(str(u.tag))
    for table, entities in ((ENEMY_PARAMS_BY_ACTION, context.enemy_entities),
                            (ALLY_PARAMS_BY_ACTION, context.own_entities)):
        tags = {u.tag for u in entities.values()}
        for name in table.get(entry['id'], ()):
            values = args.get(name)
            values = [values] if hasattr(values, 'tag') else values or []
            if any(u.tag not in tags for u in values):
                raise ParameterError.invalid_value(name, [u.tag for u in values],
                                                   'entities on the required side')
    if ready:
        ability = args.get('ability', args.get('ability_id', args.get('aoe_ability')))
        if ability is not None and units:
            readiness = [ability_ready(u, ability.name) for u in units]
            synchronized = args.get('sync_command', True)
            if (synchronized and not all(readiness)) or (not synchronized and not any(readiness)):
                raise ActionSurfaceError('ability currently unavailable', ability.name)
    target = args.get('target')
    if entry['name'] in ATTACK_ACTIONS and hasattr(target, 'tag'):
        capability = 'can_attack_air' if getattr(target, 'is_flying', False) else 'can_attack_ground'
        if any(getattr(u, capability, True) is False for u in units):
            raise ParameterError.invalid_value('target', target.tag, 'an attackable ground/air category')
    return actor_tags
