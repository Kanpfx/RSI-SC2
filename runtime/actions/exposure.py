"""State-driven action exposure shared by all tactics."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable

from agent.runtime.actions.errors import AvailabilityError, InstructionError, ParameterError
from agent.runtime.actions.resolver import EntityContext
from agent.runtime.actions.loader import ActionCatalog

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

WORKER_TYPES = {"SCV", "DRONE", "PROBE"}
TOWNHALL_TYPES = {
    "COMMANDCENTER",
    "ORBITALCOMMAND",
    "PLANETARYFORTRESS",
    "NEXUS",
    "HATCHERY",
    "LAIR",
    "HIVE",
}
class ActionSurfaceError(AvailabilityError):
    pass


@dataclass(frozen=True)
class ActionSurface:
    """The exact action subset and argument domains shown to one model call."""

    entries: list[dict[str, Any]]
    action_ids: frozenset[str]
    parameter_domains: dict[str, dict[str, frozenset[str]]]
    context: EntityContext | None = None

    def validate(self, entry: dict[str, Any], args: dict[str, Any]) -> None:
        if entry['id'] not in self.action_ids:
            raise ActionSurfaceError('action unavailable', f"{entry['name']} is not currently available")
        for name, allowed in self.parameter_domains.get(entry['id'], {}).items():
            if name not in args or args[name] is None:
                continue
            value = args[name]
            values = value if isinstance(value, list) else [value]
            submitted = {str(v) for v in values}
            if not submitted <= allowed:
                raise ParameterError.invalid_value(name, sorted(submitted - allowed),
                                                   'a currently available value from the action reference')


@dataclass(frozen=True)
class _Availability:
    status: str
    note: str
    domains: dict[str, frozenset[str]]


class ActionExposure:
    """Expose every eligible action whose live prerequisites currently exist."""

    def __init__(self, catalog: ActionCatalog):
        self.catalog = catalog

    def build(self, bot: Any, context: EntityContext) -> ActionSurface:
        prompt_entries: list[dict[str, Any]] = []
        domains: dict[str, dict[str, frozenset[str]]] = {}
        for entry in self.catalog.prompt_entries(set(self.catalog.entries)):
            availability = self._availability(entry, bot, context)
            if availability is None:
                continue
            param_names = {param["name"] for param in entry["params"]}
            if unknown := set(availability.domains) - param_names:
                raise InstructionError(
                    "catalog error",
                    f"action '{entry['id']}' availability references unknown "
                    f"parameters: {sorted(unknown)}",
                )
            prompt_entry = deepcopy(entry)
            prompt_entry["prompt_availability"] = {
                "status": availability.status,
                "note": availability.note,
            }
            for param in prompt_entry["params"]:
                allowed = availability.domains.get(param["name"])
                if allowed is None:
                    continue
                key = (
                    "allowed_keys"
                    if param["type"] == "ArmyComposition"
                    else "allowed_values"
                )
                param[key] = sorted(allowed)
                if key == "allowed_values":
                    groups = self._entity_value_groups(allowed, context)
                    if groups:
                        param["allowed_value_groups"] = groups
            prompt_entries.append(prompt_entry)
            domains[entry["id"]] = availability.domains
        return ActionSurface(
            prompt_entries,
            frozenset(entry["id"] for entry in prompt_entries),
            domains,
            context,
        )

    @staticmethod
    def _entity_value_groups(
        values: Iterable[str], context: EntityContext
    ) -> dict[str, list[str]]:
        """Group prompt-only observation IDs without changing validation domains."""
        groups: dict[str, list[str]] = {}
        for alias in sorted(values):
            entity = context.entities.get(alias)
            if entity is None:
                continue
            type_id = getattr(entity, "type_id", None)
            if type_id is None:
                continue
            side = "enemy " if alias in context.enemy_entities else ""
            groups.setdefault(f"{side}{type_id.name}", []).append(alias)
        return groups

    def _availability(
        self, entry: dict[str, Any], bot: Any, context: EntityContext
    ) -> _Availability | None:
        availability = entry["availability"]
        actor_param = availability["param"]
        if actor_param == "group":
            return self._group(entry, context)
        if actor_param is not None:
            return self._actor(entry, context)

        required_types = self._availability_types(entry)
        if required_types and not self._aliases(context.own_entities, required_types):
            return None
        if not required_types and not self._general_macro_ready(entry["id"], context):
            return None
        return _Availability(
            "available_now",
            "Required units and structures are currently available.",
            {},
        )

    def _actor(
        self, entry: dict[str, Any], context: EntityContext
    ) -> _Availability | None:
        actor_types = self._availability_types(entry)
        actors = self._aliases(
            context.own_entities,
            actor_types,
            units_only=False,
        )
        actors = {a for a in actors if actor_supported(entry, context.own_entities[a])}
        ability = self._fixed_ability(entry)
        if ability:
            actors = {
                alias
                for alias in actors
                if self._ability_ready(context.own_entities[alias], ability)
            }
        if entry["id"] == "combat.individual.drop_cargo":
            actors = {
                alias
                for alias in actors
                if bool(getattr(context.own_entities[alias], "has_cargo", False))
            }
        if not actors:
            return None

        domains: dict[str, frozenset[str]] = {
            entry["availability"]["param"]: frozenset(actors)
        }
        actor_param = entry['availability']['param']
        spec = next((p for p in entry['params'] if p['name'] == actor_param), None)
        if spec and 'UnitTypeId' in spec['type']:
            domains[actor_param] |= frozenset(context.own_entities[a].type_id.name for a in actors)
        if not self._add_target_domains(entry, context, domains):
            return None
        if not self._add_grid_domains(entry, context, domains):
            return None
        return _Availability(
            "available_now",
            f"Current actors: [{','.join(sorted(actors))}].",
            domains,
        )

    def _group(
        self, entry: dict[str, Any], context: EntityContext
    ) -> _Availability | None:
        actor_types = self._availability_types(entry)
        actors = self._aliases(
            context.own_entities,
            actor_types,
            units_only=False,
        )
        actors = {a for a in actors if actor_supported(entry, context.own_entities[a])}
        if not actors:
            return None
        domains: dict[str, frozenset[str]] = {"group": frozenset(actors)}
        actor_param = entry['availability']['param']
        spec = next((p for p in entry['params'] if p['name'] == actor_param), None)
        if spec and 'UnitTypeId' in spec['type']:
            domains[actor_param] |= frozenset(context.own_entities[a].type_id.name for a in actors)
        if not self._add_target_domains(entry, context, domains):
            return None
        if not self._add_grid_domains(entry, context, domains):
            return None
        return _Availability(
            "available_now",
            f"Current group candidates: [{','.join(sorted(actors))}].",
            domains,
        )

    @staticmethod
    def _general_macro_ready(action_id: str, context: EntityContext) -> bool:
        own_types = {
            getattr(getattr(entity, "type_id", None), "name", "UNKNOWN")
            for entity in context.own_entities.values()
        }
        # Macro behaviors can acquire prerequisites themselves. Do not require both
        # workers and townhalls when an existing research/production structure suffices.
        if action_id in {"macro.tech_up", "macro.upgrade_controller", "macro.spawn_controller"}:
            return bool(own_types)
        return True

    @staticmethod
    def _add_target_domains(
        entry: dict[str, Any],
        context: EntityContext,
        domains: dict[str, frozenset[str]],
    ) -> bool:
        enemies = frozenset(context.enemy_entities)
        enemy_params = ENEMY_PARAMS_BY_ACTION.get(entry["id"], ())
        specs = {p['name']: p for p in entry['params']}
        if not enemies and any(specs[n]['type'] == 'Unit' and specs[n]['required'] for n in enemy_params):
            return False
        for name in enemy_params:
            domains[name] = enemies

        allies = frozenset(context.own_entities)
        ally_params = ALLY_PARAMS_BY_ACTION.get(entry["id"], ())
        if ally_params and not allies:
            return False
        for name in ally_params:
            domains[name] = allies
        return True

    @staticmethod
    def _add_grid_domains(
        entry: dict[str, Any],
        context: EntityContext,
        domains: dict[str, frozenset[str]],
    ) -> bool:
        grids = frozenset(context.grids)
        for param in entry['params']:
            if 'Grid' not in param['type'] or param['input'] != 'model':
                continue
            if not grids and param['required'] and 'null' not in param['type']:
                return False
            domains[param['name']] = grids
        return True

    @staticmethod
    def _availability_types(entry: dict[str, Any]) -> set[str]:
        actor_types = set(entry["availability"]["types"])
        return set() if actor_types == {"ALL"} else actor_types

    @staticmethod
    def _fixed_ability(entry: dict[str, Any]) -> str | None:
        for param in entry["params"]:
            if param.get("input") == "runtime" and param.get("type") == "AbilityId":
                return param.get("value")
        return None

    @staticmethod
    def _aliases(
        entities: dict[str, Any],
        unit_types: set[str],
        *,
        units_only: bool = False,
    ) -> set[str]:
        aliases: set[str] = set()
        for alias, entity in entities.items():
            name = getattr(getattr(entity, "type_id", None), "name", "UNKNOWN")
            if unit_types and name not in unit_types:
                continue
            if units_only and bool(getattr(entity, "is_structure", False)):
                continue
            aliases.add(alias)
        return aliases

    @staticmethod
    def _ability_ready(unit: Any, ability_name: str) -> bool:
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
        return ActionExposure._ability_ready(unit, CAST_ABILITIES[name])
    if ready and name == 'AutoUseAOEAbility':
        from ares.dicts.aoe_ability_to_range import AOE_ABILITY_SPELLS_INFO
        return any(ActionExposure._ability_ready(unit, a.name) for a in AOE_ABILITY_SPELLS_INFO)
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
    allowed = ActionExposure._availability_types(entry)
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
            readiness = [ActionExposure._ability_ready(u, ability.name) for u in units]
            synchronized = args.get('sync_command', True)
            if (synchronized and not all(readiness)) or (not synchronized and not any(readiness)):
                raise ActionSurfaceError('ability currently unavailable', ability.name)
    target = args.get('target')
    if entry['name'] in ATTACK_ACTIONS and hasattr(target, 'tag'):
        capability = 'can_attack_air' if getattr(target, 'is_flying', False) else 'can_attack_ground'
        if any(getattr(u, capability, True) is False for u in units):
            raise ParameterError.invalid_value('target', target.tag, 'an attackable ground/air category')
    return actor_tags
