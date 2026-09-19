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
        action_id = entry["id"]
        actor_param = entry["availability"]["param"]
        if self.context is not None and actor_param in args:
            value = args[actor_param]
            actors = value if isinstance(value, list) else [value]
            required_types = ActionExposure._availability_types(entry)
            ability = ActionExposure._fixed_ability(entry)
            for value in actors:
                alias = self.context.canonical_entity_alias(value)
                unit = self.context.resolve_entity(alias, own_only=True)
                actual_type = getattr(getattr(unit, "type_id", None), "name", "UNKNOWN")
                if (required_types and actual_type not in required_types) or (
                    not required_types and getattr(unit, "is_structure", False)
                ):
                    expected = "/".join(sorted(required_types)) if required_types else "unit, not structure"
                    raise ParameterError(
                        "unit type mismatch", f"unit {alias}: {actual_type}; expected {expected}",
                        parameter=actor_param, actual=alias,
                    )
                if ability and not ActionExposure._ability_ready(unit, ability):
                    raise ParameterError(
                        "ability currently unavailable", f"unit {alias}: {ability}",
                        parameter=actor_param, actual=alias,
                    )
        if action_id not in self.action_ids:
            raise ActionSurfaceError(
                "action unavailable",
                f"action '{entry['name']}' is not currently available",
            )
        for name, allowed in self.parameter_domains.get(action_id, {}).items():
            if name not in args:
                continue
            value = args[name]
            if isinstance(value, dict):
                submitted = {str(item) for item in value}
            elif isinstance(value, list):
                submitted = {
                    EntityContext.canonical_entity_alias(item) for item in value
                }
            else:
                try:
                    submitted = {EntityContext.canonical_entity_alias(value)}
                except ValueError:
                    submitted = {str(value)}
            if not submitted.issubset(allowed):
                invalid = sorted(submitted - allowed)
                raise ParameterError.invalid_value(
                    name,
                    invalid,
                    "a currently available value; see the current observation",
                )


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
                    if param["type"] == "army_composition"
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
            type_name = getattr(getattr(entity, "type_id", None), "name", "UNKNOWN")
            side = "enemy " if alias in context.enemy_entities else ""
            groups.setdefault(f"{side}{type_name}", []).append(alias)
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
            units_only=not actor_types,
        )
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
            units_only=not actor_types,
        )
        if not actors:
            return None
        domains: dict[str, frozenset[str]] = {"group": frozenset(actors)}
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
        if action_id in {"macro.tech_up", "macro.upgrade_controller"}:
            return bool(own_types & WORKER_TYPES) and bool(own_types & TOWNHALL_TYPES)
        if action_id == "macro.auto_supply":
            return bool(own_types & WORKER_TYPES) and bool(own_types & TOWNHALL_TYPES)
        return True

    @staticmethod
    def _add_target_domains(
        entry: dict[str, Any],
        context: EntityContext,
        domains: dict[str, frozenset[str]],
    ) -> bool:
        enemies = frozenset(context.enemy_entities)
        enemy_params = ENEMY_PARAMS_BY_ACTION.get(entry["id"], ())
        if enemy_params and not enemies and entry["id"] != "combat.group.keep_group_safe":
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
        grid_params = [
            param["name"]
            for param in entry["params"]
            if param["type"] == "grid_ref"
            and param["input"] == "model"
            and param["required"]
        ]
        if not grid_params:
            return True
        grids = frozenset(context.grids)
        if not grids:
            return False
        for name in grid_params:
            domains[name] = grids
        return True

    @staticmethod
    def _availability_types(entry: dict[str, Any]) -> set[str]:
        actor_types = set(entry["availability"]["types"])
        return set() if actor_types == {"ALL"} else actor_types

    @staticmethod
    def _fixed_ability(entry: dict[str, Any]) -> str | None:
        for param in entry["params"]:
            if param.get("input") == "runtime" and param.get("type") == "ability_id":
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
