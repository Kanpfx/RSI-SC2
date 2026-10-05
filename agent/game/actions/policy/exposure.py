"""State-driven action exposure shared by all tactics."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable

from agent.game.actions.errors import InstructionError, ParameterError
from agent.game.actions.resolution.resolver import EntityContext
from agent.game.actions.resolution.loader import ActionCatalog
from agent.game.actions.policy.rules import (
    ALLY_PARAMS_BY_ACTION,
    ENEMY_PARAMS_BY_ACTION,
    ActionSurfaceError,
    ability_ready,
    actor_supported,
    availability_types,
    actor_parameter,
)

@dataclass(frozen=True)
class ActionSurface:
    """The exact action subset and argument domains shown to one model call."""

    entries: list[dict[str, Any]]
    action_ids: frozenset[str]
    parameter_domains: dict[str, dict[str, frozenset[str]]]
    context: EntityContext | None = None

    def validate(self, entry: dict[str, Any], args: dict[str, Any]) -> None:
        if entry['name'] not in self.action_ids:
            raise ActionSurfaceError('action unavailable', f"{entry['name']} is not currently available")
        for name, allowed in self.parameter_domains.get(entry['name'], {}).items():
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
            if entry['name'] == 'GhostSnipe' and getattr(getattr(bot, 'enemy_race', None), 'name', None) != 'Zerg':
                continue
            availability = self._availability(entry, bot, context)
            if availability is None:
                continue
            param_names = {param["name"] for param in entry["params"]}
            if unknown := set(availability.domains) - param_names:
                raise InstructionError(
                    "catalog error",
                    f"action '{entry['name']}' availability references unknown "
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
                    if param["name"] == "army_composition_dict"
                    else "allowed_values"
                )
                param[key] = sorted(allowed)
                if key == "allowed_values":
                    groups = self._entity_value_groups(allowed, context)
                    if groups:
                        param["allowed_value_groups"] = groups
            prompt_entries.append(prompt_entry)
            domains[entry["name"]] = availability.domains
        return ActionSurface(
            prompt_entries,
            frozenset(entry["name"] for entry in prompt_entries),
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
        actor_param = actor_parameter(entry)
        if actor_param == "group":
            return self._group(entry, context)
        if actor_param is not None:
            return self._actor(entry, context)

        required_types = availability_types(entry)
        if required_types and not self._aliases(context.own_entities, required_types):
            return None
        if not required_types and not self._general_macro_ready(entry["name"], context):
            return None
        return _Availability(
            "available_now",
            "Required units and structures are currently available.",
            {},
        )

    def _actor(
        self, entry: dict[str, Any], context: EntityContext
    ) -> _Availability | None:
        actor_types = availability_types(entry)
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
                if ability_ready(context.own_entities[alias], ability)
            }
        if entry["name"] == "DropCargo":
            actors = {
                alias
                for alias in actors
                if bool(getattr(context.own_entities[alias], "has_cargo", False))
            }
        if not actors:
            return None

        domains: dict[str, frozenset[str]] = {
            actor_parameter(entry): frozenset(actors)
        }
        actor_param = actor_parameter(entry)
        spec = next((p for p in entry['params'] if p['name'] == actor_param), None)
        if spec and 'UnitTypeId' in spec['type'] and entry['name'] != 'AddonSwap':
            domains[actor_param] |= frozenset(context.own_entities[a].type_id.name for a in actors)
        if not self._add_target_domains(entry, context, domains):
            return None
        return _Availability(
            "available_now",
            f"Current actors: [{','.join(sorted(actors))}].",
            domains,
        )

    def _group(
        self, entry: dict[str, Any], context: EntityContext
    ) -> _Availability | None:
        actor_types = availability_types(entry)
        actors = self._aliases(
            context.own_entities,
            actor_types,
            units_only=False,
        )
        actors = {a for a in actors if actor_supported(entry, context.own_entities[a])}
        if not actors:
            return None
        domains: dict[str, frozenset[str]] = {"group": frozenset(actors)}
        if not self._add_target_domains(entry, context, domains):
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
        if action_id in {"TechUp", "UpgradeController", "SpawnController"}:
            return bool(own_types)
        return True

    @staticmethod
    def _add_target_domains(
        entry: dict[str, Any],
        context: EntityContext,
        domains: dict[str, frozenset[str]],
    ) -> bool:
        enemies = frozenset(context.enemy_entities)
        enemy_params = ENEMY_PARAMS_BY_ACTION.get(entry["name"], ())
        specs = {p['name']: p for p in entry['params']}
        if not enemies and any(specs[n]['type'] == 'Unit' and specs[n]['required'] for n in enemy_params):
            return False
        for name in enemy_params:
            domains[name] = enemies

        allies = frozenset(context.own_entities)
        ally_params = ALLY_PARAMS_BY_ACTION.get(entry["name"], ())
        if ally_params and not allies:
            return False
        for name in ally_params:
            domains[name] = allies
        return True

    @staticmethod
    def _fixed_ability(entry: dict[str, Any]) -> str | None:
        for param in entry["params"]:
            if param.get("source") == "fixed" and param.get("type") == "AbilityId":
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
