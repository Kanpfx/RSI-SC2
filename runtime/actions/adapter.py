"""Resolve catalog arguments and construct Ares behaviors."""
from __future__ import annotations

import importlib
from functools import lru_cache
from math import isclose
from typing import Any

from agent.runtime.actions.errors import InstructionError, ParameterError, ResolveError
from agent.runtime.actions.loader import ActionCatalog
from agent.runtime.actions.resolver import EntityContext
from agent.runtime.actions.types import symbol


@lru_cache(maxsize=128)
def behavior_class(import_path: str) -> type:
    if not import_path.startswith("ares.behaviors."):
        raise ValueError("Only catalog Ares behavior imports are supported")
    module, name = import_path.rsplit(".", 1)
    return getattr(importlib.import_module(module), name)


class AresActionAdapter:
    def __init__(self, catalog: ActionCatalog):
        self.catalog = catalog

    def compile_and_register(self, bot: Any, actions: list[dict[str, Any]], context: EntityContext) -> list[Any]:
        context.bot = bot
        compiled = self.compile(actions, context)
        for behavior in compiled:
            bot.register_behavior(behavior)
        return compiled

    def construct(self, action: dict[str, Any], kwargs: dict[str, Any]) -> Any:
        return behavior_class(self.catalog.get(action["id"])["api"]["import"])(**kwargs)

    def compile(self, actions: list[dict[str, Any]], context: EntityContext) -> list[Any]:
        # LLM exposure is enforced at the model review boundary, not for internal callers.
        return [self.construct(action, kwargs) for action, kwargs in
                (self.prepare(action, context) for action in actions)]

    def prepare(self, action: Any, context: EntityContext) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(action, dict) or set(action) != {"id", "args"}:
            raise InstructionError("format error", "action must contain exactly id and args")
        if not isinstance(action['id'], str) or not isinstance(action['args'], dict):
            raise InstructionError("format error", "id must be a string and args an object")
        entry = self.catalog.get(action['id'])
        params = self.catalog.model_params(entry)
        aliases = {symbol(name): name for name in params}
        supplied = {}
        for raw_name, value in action['args'].items():
            if not isinstance(raw_name, str):
                raise ParameterError.format('args', 'string parameter names')
            name = aliases.get(symbol(raw_name), raw_name)
            if name not in params:
                raise ParameterError.unexpected([name])
            if name in supplied:
                raise ParameterError.duplicate(name)
            supplied[name] = value
        wire, kwargs = {}, {}
        for name, param in params.items():
            if name not in supplied:
                if param['required']:
                    raise ParameterError.missing(name)
                continue  # Ares supplies its constructor default, including fresh collections.
            wire[name], kwargs[name] = self.catalog.type_resolver.resolve(
                supplied[name], param['type'], context, name)
            if 'choices' in param and wire[name] is not None and wire[name] not in param['choices']:
                raise ParameterError.invalid_value(name, wire[name], str(param['choices']))
        for param in entry['params']:
            if param['input'] != 'runtime':
                continue
            if 'value' in param:
                _, kwargs[param['name']] = self.catalog.type_resolver.resolve(
                    param['value'], param['type'], context, param['name'])
            else:
                kwargs[param['name']] = context.runtime_value(param['source'], kwargs)
        self._validate_behavior(entry, kwargs)
        return {"id": entry['name'], "args": wire}, kwargs

    def _validate_behavior(self, entry: dict[str, Any], args: dict[str, Any]) -> None:
        name = entry['name']
        if name == 'TechUp':
            self._resolve_tech_up_target(args['desired_tech'])
        if name == 'BuildStructure':
            from ares.dicts.structure_to_building_size import STRUCTURE_TO_BUILDING_SIZE
            if args['structure_id'] not in STRUCTURE_TO_BUILDING_SIZE:
                raise ParameterError.invalid_value('structure_id', args['structure_id'].name,
                                                   'a structure supported by BuildStructure; use dedicated gas/add-on behaviors otherwise')
        if name == 'UpgradeController':
            from sc2.dicts.upgrade_researched_from import UPGRADE_RESEARCHED_FROM
            if not args['upgrade_list'] or any(u not in UPGRADE_RESEARCHED_FROM for u in args['upgrade_list']):
                raise ParameterError.format('upgrade_list', 'a non-empty list of supported research upgrades')
        if 'army_composition_dict' in args:
            composition = args['army_composition_dict']
            if not composition:
                raise ParameterError.format('army_composition_dict', 'a non-empty composition')
            from sc2.dicts.unit_trained_from import UNIT_TRAINED_FROM
            from sc2.ids.unit_typeid import UnitTypeId
            if any(u not in UNIT_TRAINED_FROM and u != UnitTypeId.ARCHON for u in composition):
                raise ParameterError.format('army_composition_dict', 'trainable or morphable unit types')
            if not args.get('freeflow_mode', False) and not isclose(
                    sum(v['proportion'] for v in composition.values()), 1.0, abs_tol=1e-6):
                raise ParameterError.format('army_composition_dict', 'proportions summing to 1')
        if name == 'PlacePredictiveAoE' and not args['path']:
            raise ParameterError.format('path', 'a non-empty path')
        if 'group' in args and not args['group']:
            raise ParameterError.format('group', 'a non-empty group')
        for key in ('to_count', 'to_count_per_base', 'max_on_route', 'max_pending', 'maximum',
                    'min_targets', 'ability_delay', 'workers_per_gas'):
            if key in args and args[key] < 0:
                raise ParameterError.invalid_value(key, args[key], 'a nonnegative integer')

    @staticmethod
    def _resolve_tech_up_target(value: Any) -> Any:
        """Resolve only targets that Ares' ``TechUp`` can safely inspect.

        Ares indexes ``UNIT_TECH_REQUIREMENT`` during behavior execution.  A
        generic enum such as ``TECHLAB`` therefore used to pass our adapter and
        crash the game loop with a KeyError. Validate the same prerequisite
        lookup here so malformed targets become recoverable policy issues.
        """
        from ares.behaviors.macro.tech_up import BUILD_TECHLAB_FROM
        from ares.consts import ALL_STRUCTURES, GATEWAY_UNITS, TECHLAB_TYPES
        from ares.dicts.unit_tech_requirement import UNIT_TECH_REQUIREMENT
        from sc2.dicts.unit_trained_from import UNIT_TRAINED_FROM
        from sc2.dicts.upgrade_researched_from import UPGRADE_RESEARCHED_FROM
        from sc2.ids.unit_typeid import UnitTypeId
        from sc2.ids.upgrade_id import UpgradeId

        desired_tech = value

        try:
            if isinstance(desired_tech, UpgradeId):
                researched_from = UPGRADE_RESEARCHED_FROM[desired_tech]
            elif desired_tech in ALL_STRUCTURES:
                researched_from = desired_tech
            else:
                researched_from = next(iter(UNIT_TRAINED_FROM[desired_tech]))
                if desired_tech in GATEWAY_UNITS:
                    researched_from = UnitTypeId.GATEWAY

            if researched_from in TECHLAB_TYPES:
                if researched_from not in BUILD_TECHLAB_FROM:
                    raise KeyError(researched_from)
            else:
                UNIT_TECH_REQUIREMENT[researched_from]
        except (KeyError, StopIteration) as exc:
            raise ResolveError.invalid_value(
                "desired_tech",
                value,
                "a concrete technology target supported by Ares TechUp",
            ) from exc
        return desired_tech

