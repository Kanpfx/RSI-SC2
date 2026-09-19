"""Compile only catalog-described instructions into registered Ares behaviors."""

from __future__ import annotations

import importlib
from math import isclose
from typing import Any

from agent.runtime.actions.errors import (
    ActionNameError,
    InstructionError,
    ParameterError,
    ResolveError,
)
from agent.runtime.actions.resolver import EntityContext
from agent.runtime.actions.loader import ActionCatalog


# Common multiplayer names can otherwise resolve to unrelated campaign enums.
UPGRADE_NAME_ALIASES = {
    "combatshield": "shieldwall",
    "combatshields": "shieldwall",
    "concussiveshell": "punishergrenades",
    "concussiveshells": "punishergrenades",
}


class AresActionAdapter:
    def __init__(self, catalog: ActionCatalog):
        self.catalog = catalog

    def compile_and_register(
        self, bot: Any, actions: list[dict[str, Any]], context: EntityContext
    ) -> list[Any]:
        compiled = self.compile(actions, context)
        for behavior in compiled:
            bot.register_behavior(behavior)
        return compiled

    def compile(
        self, actions: list[dict[str, Any]], context: EntityContext
    ) -> list[Any]:
        """Construct catalog behaviors without choosing how they are scheduled."""
        compiled: list[Any] = []
        for action in actions:
            entry = self._validate_shape(action)
            kwargs = self._resolve_arguments(entry, action["args"], context)
            module_name, class_name = entry["api"]["import"].rsplit(".", 1)
            behavior_type = getattr(importlib.import_module(module_name), class_name)
            behavior = behavior_type(**kwargs)
            compiled.append(behavior)
        return compiled

    def _validate_shape(self, action: Any) -> dict[str, Any]:
        if not isinstance(action, dict) or set(action) != {"id", "args"}:
            raise InstructionError(
                "format error", "each action must contain exactly 'id' and 'args'"
            )
        if not isinstance(action["id"], str) or not isinstance(action["args"], dict):
            raise InstructionError(
                "format error",
                "action 'id' must be a string and 'args' must be a JSON object",
            )
        entry = self.catalog.get(action["id"])
        if entry.get("llm_exposure") != "eligible":
            raise ActionNameError.disabled(action["id"])
        return entry

    def _resolve_arguments(
        self, entry: dict[str, Any], args: dict[str, Any], context: EntityContext
    ) -> dict[str, Any]:
        params = self.catalog.required_model_params(entry)
        unknown = set(args) - set(params)
        if unknown:
            raise ParameterError.unexpected(sorted(unknown))
        kwargs: dict[str, Any] = {}
        for name, param in params.items():
            if name not in args:
                raise ParameterError.missing(name)
            if entry["id"] == "combat.group.keep_group_safe" and name == "close_enemy" and args[name] == []:
                kwargs[name] = []
            elif entry["id"] == "macro.tech_up" and name == "desired_tech":
                kwargs[name] = self._resolve_tech_up_target(args[name])
            else:
                kwargs[name] = self._resolve_value(
                    args[name], param["type"], context, args, name
                )
        for param in entry["params"]:
            if param.get("input") == "derived":
                kwargs[param["name"]] = context.derive_group_value(param["derive"], args.get("group"))
                continue
            if param.get("input") != "runtime":
                continue
            name = param["name"]
            if "value" not in param:
                raise InstructionError(
                    "catalog error", f"runtime parameter '{name}' has no fixed value"
                )
            kwargs[name] = self._resolve_value(
                param["value"], param["type"], context, args, name
            )
        return kwargs

    def _resolve_value(
        self,
        value: Any,
        type_name: str,
        context: EntityContext,
        args: dict[str, Any],
        name: str,
    ) -> Any:
        if value is None:
            return None
        if type_name == "boolean":
            if not isinstance(value, bool):
                raise ResolveError.format(name, "a JSON boolean")
            return value
        if type_name == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ResolveError.format(name, "an integer")
            return value
        if type_name == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ResolveError.format(name, "a number")
            return value
        if type_name == "army_composition":
            return self._resolve_army_composition(value, name)
        # The upstream generated catalog calls this a UnitTypeId even though
        # SpawnController accepts a mapping of UnitTypeId to composition data.
        # Keep the raw catalog intact during restoration and normalize its one
        # known compound parameter at the runtime boundary.
        if name == "army_composition_dict" and type_name == "unit_type_id":
            return self._resolve_army_composition(value, name)
        if type_name == "unit_ref":
            return context.resolve_entity(value)
        if type_name == "unit_refs":
            if not isinstance(value, list) or not value:
                raise ResolveError.format(name, "a non-empty observation unit ID list")
            return [context.resolve_entity(alias) for alias in value]
        if type_name == "unit_or_unit_type_id":
            if context.has_entity_alias(value):
                return context.resolve_entity(value, own_only=True)
            from sc2.ids.unit_typeid import UnitTypeId

            return self._resolve_enum(UnitTypeId, value, name)
        if type_name == "point_ref":
            return context.resolve_point(value)
        if type_name == "point_or_unit_ref":
            return (
                context.resolve_entity(value)
                if context.has_entity_alias(value)
                else context.resolve_point(value)
            )
        if type_name == "grid_ref":
            if not isinstance(value, str):
                raise ResolveError.format(name, "a grid name")
            normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
            try:
                return context.grids[normalized]
            except KeyError as exc:
                raise ResolveError.invalid_value(
                    name, value, f"one of {sorted(context.grids)}"
                ) from exc
        if type_name == "unit_type_id":
            from sc2.ids.unit_typeid import UnitTypeId

            return self._resolve_enum(UnitTypeId, value, name)
        if type_name == "ability_id":
            from sc2.ids.ability_id import AbilityId

            return self._resolve_enum(AbilityId, value, name)
        if type_name == "upgrade_id":
            from sc2.ids.upgrade_id import UpgradeId

            return self._resolve_enum(UpgradeId, value, name)
        if type_name == "upgrade_ids":
            if not isinstance(value, list) or not value:
                raise ResolveError.format(name, "a non-empty upgrade name list")
            from sc2.dicts.upgrade_researched_from import UPGRADE_RESEARCHED_FROM
            from sc2.ids.upgrade_id import UpgradeId

            upgrades = [self._resolve_enum(UpgradeId, item, name) for item in value]
            unsupported = [
                upgrade.name
                for upgrade in upgrades
                if upgrade not in UPGRADE_RESEARCHED_FROM
            ]
            if unsupported:
                raise ResolveError.invalid_value(
                    name,
                    unsupported,
                    "upgrade names supported by Ares UpgradeController",
                )
            return upgrades
        if type_name == "unit_role":
            from ares.consts import UnitRole

            return self._resolve_enum(UnitRole, value, name)
        raise ResolveError.invalid_value(
            name, type_name, "a catalog parameter type supported by the runtime"
        )

    @staticmethod
    def _resolve_enum(enum_type: Any, value: Any, name: str) -> Any:
        if not isinstance(value, str):
            raise ResolveError.format(name, f"a {enum_type.__name__} name")
        normalized = "".join(
            character for character in value.strip().casefold() if character.isalnum()
        )
        if enum_type.__name__ == "UpgradeId":
            normalized = UPGRADE_NAME_ALIASES.get(normalized, normalized)
        for member_name, member in enum_type.__members__.items():
            candidate = "".join(
                character for character in member_name.casefold() if character.isalnum()
            )
            if candidate == normalized:
                return member
        raise ResolveError.invalid_value(
            name, value, f"a valid {enum_type.__name__} name"
        )

    @staticmethod
    def _resolve_tech_up_target(value: Any) -> Any:
        """Resolve only targets that Ares' ``TechUp`` can safely inspect.

        Ares indexes ``UNIT_TECH_REQUIREMENT`` during behavior execution.  A
        generic enum such as ``TECHLAB`` therefore used to pass our adapter and
        crash the game loop with a KeyError. Validate the same prerequisite
        lookup here so malformed targets become recoverable policy issues.
        """
        if not isinstance(value, str):
            raise ResolveError.format("desired_tech", "a unit or upgrade enum name")

        from ares.behaviors.macro.tech_up import BUILD_TECHLAB_FROM
        from ares.consts import ALL_STRUCTURES, GATEWAY_UNITS, TECHLAB_TYPES
        from ares.dicts.unit_tech_requirement import UNIT_TECH_REQUIREMENT
        from sc2.dicts.unit_trained_from import UNIT_TRAINED_FROM
        from sc2.dicts.upgrade_researched_from import UPGRADE_RESEARCHED_FROM
        from sc2.ids.unit_typeid import UnitTypeId
        from sc2.ids.upgrade_id import UpgradeId

        try:
            desired_tech: Any = AresActionAdapter._resolve_enum(
                UnitTypeId, value, "desired_tech"
            )
        except ResolveError:
            try:
                desired_tech = AresActionAdapter._resolve_enum(
                    UpgradeId, value, "desired_tech"
                )
            except ResolveError as exc:
                raise ResolveError.invalid_value(
                    "desired_tech", value, "a valid unit or upgrade enum name"
                ) from exc

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

    @staticmethod
    def _resolve_army_composition(value: Any, name: str) -> dict[Any, dict[str, Any]]:
        """Convert the only model-exposed composition shape to Ares enums.

        Unit names remain enum-checked, while tactic policy stays outside the
        generic Ares translation layer.
        """
        if not isinstance(value, dict) or not value:
            raise ResolveError.format(name, "a non-empty army composition object")
        from sc2.ids.unit_typeid import UnitTypeId

        normalized_value: dict[Any, Any] = {}
        for raw_unit_name, settings in value.items():
            if not isinstance(raw_unit_name, str):
                raise ResolveError.format(name, "an object with unit-name keys")
            normalized_name = (
                "BATTLECRUISER"
                if raw_unit_name.strip().casefold() == "bc"
                else raw_unit_name
            )
            unit_type = AresActionAdapter._resolve_enum(
                UnitTypeId, normalized_name, name
            )
            if unit_type in normalized_value:
                raise ResolveError.invalid_value(
                    name, raw_unit_name, "each unit type exactly once"
                )
            normalized_value[unit_type] = settings
        value = normalized_value

        composition: dict[Any, dict[str, Any]] = {}
        total = 0.0
        for unit_type, settings in value.items():
            unit_name = unit_type.name
            if not isinstance(settings, dict) or set(settings) != {
                "proportion",
                "priority",
            }:
                raise ResolveError.format(
                    f"{name}.{unit_name}",
                    "an object containing exactly 'proportion' and 'priority'",
                )
            proportion = settings["proportion"]
            priority = settings["priority"]
            if isinstance(proportion, bool) or not isinstance(proportion, (int, float)):
                raise ResolveError.format(f"{name}.{unit_name}.proportion", "a number")
            if not 0.0 < float(proportion) <= 1.0:
                raise ResolveError.invalid_value(
                    f"{name}.{unit_name}.proportion",
                    proportion,
                    "a number in the interval (0, 1]",
                )
            if (
                isinstance(priority, bool)
                or not isinstance(priority, int)
                or not 0 <= priority < 11
            ):
                raise ResolveError.invalid_value(
                    f"{name}.{unit_name}.priority",
                    priority,
                    "an integer from 0 to 10",
                )
            total += float(proportion)
            composition[unit_type] = {
                "proportion": float(proportion),
                "priority": priority,
            }
        if not isclose(total, 1.0, abs_tol=1e-6):
            raise ResolveError.invalid_value(
                name, total, "composition proportions that sum to 1.0"
            )
        return composition
