"""Shared actor capabilities, target constraints and readiness checks."""

from __future__ import annotations

from typing import Any, Iterable

from agent.game.actions.errors import AvailabilityError, ParameterError
from agent.game.actions.resolution.resolver import EntityContext

ACTOR_RULES = {
    'AMoveGroup': {'param': 'group', 'types': ['ALL']},
    'GroupUseAbility': {'param': 'group', 'types': ['ALL']},
    'KeepGroupSafe': {'param': 'group', 'types': ['ALL']},
    'PathGroupToTarget': {'param': 'group', 'types': ['ALL']},
    'StutterGroupBack': {'param': 'group', 'types': ['ALL']},
    'StutterGroupForward': {'param': 'group', 'types': ['ALL']},
    'AMove': {'param': 'unit', 'types': ['ALL']},
    'AttackTarget': {'param': 'unit', 'types': ['ALL']},
    'AutoUseAOEAbility': {'param': 'unit', 'types': ['VIPER', 'RAVAGER', 'RAVEN', 'GHOST', 'INFESTOR', 'REAPER', 'HIGHTEMPLAR']},
    'DropCargo': {'param': 'unit', 'types': ['MEDIVAC', 'OVERLORDTRANSPORT', 'WARPPRISM', 'NYDUSNETWORK', 'NYDUSCANAL']},
    'GhostSnipe': {'param': 'unit', 'types': ['GHOST']},
    'KeepUnitSafe': {'param': 'unit', 'types': ['ALL']},
    'MedivacHeal': {'param': 'unit', 'types': ['MEDIVAC']},
    'MoveToSafeTarget': {'param': 'unit', 'types': ['ALL']},
    'NydusPathUnitToTarget': {'param': 'unit', 'types': ['ALL']},
    'PathUnitToTarget': {'param': 'unit', 'types': ['ALL']},
    'PickUpAndDropCargo': {'param': 'unit', 'types': ['MEDIVAC', 'OVERLORDTRANSPORT', 'WARPPRISM']},
    'PickUpCargo': {'param': 'unit', 'types': ['MEDIVAC', 'OVERLORDTRANSPORT', 'WARPPRISM']},
    'PlacePredictiveAoE': {'param': 'unit', 'types': ['ALL']},
    'QueenSpreadCreep': {'param': 'unit', 'types': ['QUEEN']},
    'RavenAutoTurret': {'param': 'unit', 'types': ['RAVEN']},
    'ReaperGrenade': {'param': 'unit', 'types': ['REAPER']},
    'ShootAndMoveToTarget': {'param': 'unit', 'types': ['ALL']},
    'ShootTargetInRange': {'param': 'unit', 'types': ['ALL']},
    'SiegeTankDecision': {'param': 'unit', 'types': ['SIEGETANK', 'SIEGETANKSIEGED']},
    'StutterUnitBack': {'param': 'unit', 'types': ['ALL']},
    'StutterUnitForward': {'param': 'unit', 'types': ['ALL']},
    'TumorSpreadCreep': {'param': 'unit', 'types': ['CREEPTUMOR', 'CREEPTUMORBURROWED', 'CREEPTUMORQUEEN']},
    'UseAOEAbility': {'param': 'unit', 'types': ['VIPER', 'RAVAGER', 'RAVEN', 'GHOST', 'INFESTOR', 'REAPER', 'HIGHTEMPLAR']},
    'UseAbility': {'param': 'unit', 'types': ['ALL']},
    'UseTransfuse': {'param': 'unit', 'types': ['QUEEN']},
    'WorkerKiteBack': {'param': 'unit', 'types': ['SCV', 'DRONE', 'PROBE']},
    'AddonSwap': {'param': 'structure_needing_addon', 'types': ['BARRACKS', 'FACTORY', 'STARPORT']},
    'AutoSupply': {'param': None, 'types': ['SCV', 'PROBE', 'LARVA']},
    'BuildStructure': {'param': None, 'types': ['SCV', 'DRONE', 'PROBE']},
    'BuildWorkers': {'param': None, 'types': ['COMMANDCENTER', 'ORBITALCOMMAND', 'PLANETARYFORTRESS', 'NEXUS', 'HATCHERY', 'LAIR', 'HIVE']},
    'ExpansionController': {'param': None, 'types': ['SCV', 'DRONE', 'PROBE']},
    'GasBuildingController': {'param': None, 'types': ['SCV', 'DRONE', 'PROBE']},
    'Mining': {'param': None, 'types': ['SCV', 'DRONE', 'PROBE']},
    'ProductionController': {'param': None, 'types': ['SCV', 'DRONE', 'PROBE']},
    'ProtossStaticDefence': {'param': None, 'types': ['PROBE']},
    'RestorePower': {'param': None, 'types': ['PROBE']},
    'SpawnController': {'param': None, 'types': ['ALL']},
    'SpeedMining': {'param': 'worker', 'types': ['SCV', 'DRONE', 'PROBE']},
    'TechUp': {'param': None, 'types': ['ALL']},
    'UpgradeCCs': {'param': None, 'types': ['COMMANDCENTER']},
    'UpgradeController': {'param': None, 'types': ['ALL']},
}


def actor_parameter(entry):
    return ACTOR_RULES[entry["name"]]["param"]


ENEMY_PARAMS_BY_ACTION = {
    "AttackTarget": ("target",),
    "GhostSnipe": ("close_enemy",),
    "PlacePredictiveAoE": ("enemy_center_unit",),
    "RavenAutoTurret": ("all_close_enemy",),
    "ReaperGrenade": ("enemy_units",),
    "ShootAndMoveToTarget": ("enemy_units",),
    "ShootTargetInRange": ("targets",),
    "SiegeTankDecision": ("close_enemy",),
    "StutterUnitBack": ("target",),
    "StutterUnitForward": ("target",),
    "UseAOEAbility": ("targets",),
    "WorkerKiteBack": ("target",),
    "KeepGroupSafe": ("close_enemy",),
    "StutterGroupForward": ("enemies",),
}

ALLY_PARAMS_BY_ACTION = {
    "MedivacHeal": ("close_allied",),
    "PickUpAndDropCargo": ("pickup_targets",),
    "PickUpCargo": ("pickup_targets",),
    "UseTransfuse": ("targets",),
}


class ActionSurfaceError(AvailabilityError):
    pass


def availability_types(entry: dict[str, Any]) -> set[str]:
    actor_types = set(ACTOR_RULES[entry["name"]]["types"])
    return set() if actor_types == {"ALL"} else actor_types


def ability_ready(unit: Any, ability_name: str) -> bool:
    abilities: Iterable[Any] | None = getattr(unit, "abilities", None)
    if abilities is None:
        return False
    return ability_name in {
        getattr(ability, "name", str(ability)) for ability in abilities
    }


# Static actor capabilities and target constraints belong to code.
MOVING_ACTIONS = {
    'AMove', 'AMoveGroup', 'KeepUnitSafe', 'KeepGroupSafe',
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
    actor_param = actor_parameter(entry)
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
            raise ParameterError.invalid_value(actor_param, context.observation_id(u.tag), 'an own actor')
        if allowed and u.type_id.name not in allowed:
            raise ParameterError.invalid_value(actor_param, u.type_id.name, str(sorted(allowed)))
        if not actor_supported(entry, u, ready=ready):
            raise ActionSurfaceError('actor cannot execute', f"unit {context.observation_id(u.tag)}: {entry['name']}")
        if str(u.tag) in actor_tags:
            raise ParameterError.invalid_value(actor_param, context.observation_id(u.tag), 'distinct actor IDs')
        actor_tags.add(str(u.tag))
    for table, entities in ((ENEMY_PARAMS_BY_ACTION, context.enemy_entities),
                            (ALLY_PARAMS_BY_ACTION, context.own_entities)):
        tags = {u.tag for u in entities.values()}
        for name in table.get(entry['name'], ()):
            values = args.get(name)
            values = [values] if hasattr(values, 'tag') else values or []
            if any(u.tag not in tags for u in values):
                raise ParameterError.invalid_value(name, [context.observation_id(u.tag) for u in values],
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
            raise ParameterError.invalid_value('target', context.observation_id(target.tag), 'an attackable ground/air category')
    return actor_tags
