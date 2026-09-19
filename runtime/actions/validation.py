"""Recoverable model action review before Ares behavior construction."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isclose
from typing import Any

from agent.config import GameConfig
from agent.runtime.actions.adapter import AresActionAdapter
from agent.runtime.actions.errors import (
    ActionNameError,
    ConflictError,
    InstructionError,
    OutputFormatError,
    ParameterError,
)
from agent.runtime.actions.exposure import ActionSurface
from agent.runtime.actions.resolver import EntityContext
from agent.runtime.actions.loader import ActionCatalog


@dataclass(frozen=True)
class ValidationIssue:
    """One action-level problem suitable for next-turn validation feedback."""

    index: int
    action: Any
    reason: str

    def text(self) -> str:
        return f"Action {self.index + 1}: {self.reason}"


@dataclass(frozen=True)
class ActionReview:
    """Valid actions are retained even when sibling actions need repair."""

    actions: list[dict[str, Any]]
    issues: list[ValidationIssue]
    normalizations: list[str]
    notices: list[ValidationIssue] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return not self.issues

    @property
    def message(self) -> str:
        if not self.issues:
            return "accepted"
        return "\n".join(issue.text() for issue in self.issues)


class PolicyValidator:
    def __init__(self, catalog: ActionCatalog, game_config: GameConfig):
        self.catalog = catalog
        self.adapter = AresActionAdapter(catalog)
        self.game_config = game_config

    def review(
        self,
        bot: Any,
        actions: list[dict[str, Any]],
        context: EntityContext,
        surface: ActionSurface | None = None,
    ) -> ActionReview:
        """Keep valid actions and report validation failures individually.

        This deliberately does not invent replacement actions. Deterministic
        normalization is limited to clamping a point that is only slightly
        outside the playable area; semantic errors go to the next model turn.
        """
        if not isinstance(actions, list):
            return ActionReview(
                [],
                [
                    ValidationIssue(
                        0,
                        actions,
                        str(OutputFormatError.field_type("actions", "a JSON list")),
                    )
                ],
                [],
            )

        valid: list[dict[str, Any]] = []
        issues: list[ValidationIssue] = []
        normalizations: list[str] = []
        notices: list[ValidationIssue] = []
        seen_own: set[str] = set()
        for index, action in enumerate(actions):
            if index >= self.game_config.max_actions_per_decision:
                issues.append(
                    ValidationIssue(
                        index,
                        action,
                        str(
                            InstructionError(
                                "action limit exceeded",
                                "at most "
                                f"{self.game_config.max_actions_per_decision} actions "
                                "may be executed in one decision",
                            )
                        ),
                    )
                )
                continue
            current_action = action
            try:
                if not isinstance(action, dict):
                    raise InstructionError(
                        "format error", "each action must be a JSON object"
                    )
                current_action, shape_notes = self._normalize_action_keys(action)
                action_id = current_action.get("id")
                entry = self.catalog.get(action_id)
                if entry.get("llm_exposure") != "eligible":
                    raise ActionNameError.disabled(action_id)

                normalized, notes = self._normalize_action(
                    entry, current_action, bot, context
                )
                group = normalized["args"].get("group")
                if entry["availability"]["param"] == "group" and isinstance(group, list):
                    remaining = []
                    for value in group:
                        alias = context.canonical_entity_alias(value)
                        if alias in context.known_own_aliases and alias not in context.own_entities:
                            notices.append(ValidationIssue(index, f"unit {alias}", "unit disappeared; removed from group"))
                        else:
                            remaining.append(alias)
                    normalized["args"]["group"] = remaining
                    if group and not remaining:
                        raise ParameterError("empty group", "no surviving group members")
                if surface is not None:
                    surface.validate(entry, normalized["args"])
                self.adapter._validate_shape(normalized)
                self._validate_ares_behavior_constraints(action_id, normalized["args"])
                provisional_seen = set(seen_own)
                self._validate_live_args(
                    entry, normalized["args"], context, provisional_seen
                )
                self.adapter._resolve_arguments(entry, normalized["args"], context)
                seen_own = provisional_seen
                valid.append(normalized)
                normalizations.extend(
                    f"Action {index + 1}: {note}" for note in shape_notes + notes
                )
            except (KeyError, TypeError, InstructionError, ValueError) as exc:
                issues.append(ValidationIssue(index, current_action, str(exc)))
        return ActionReview(valid, issues, normalizations, notices)

    @staticmethod
    def _symbol_key(value: str) -> str:
        return "".join(
            character for character in value.strip().casefold() if character.isalnum()
        )

    def _normalize_action_keys(
        self, action: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        """Normalize only harmless case and separator variations in key names."""
        top_level_names = {self._symbol_key(name): name for name in ("id", "args")}
        normalized: dict[str, Any] = {}
        notes: list[str] = []
        for raw_name, value in action.items():
            name = raw_name
            if isinstance(raw_name, str):
                name = top_level_names.get(self._symbol_key(raw_name), raw_name)
            if name in normalized:
                raise ParameterError.duplicate(str(name))
            normalized[name] = value
            if name != raw_name:
                notes.append(f"normalized field name {raw_name!r} to {name!r}")
        return normalized, notes

    def _normalize_action(
        self,
        entry: dict[str, Any],
        action: dict[str, Any],
        bot: Any,
        context: EntityContext,
    ) -> tuple[dict[str, Any], list[str]]:
        """Apply only lossless, local normalizations before validation."""
        if not isinstance(action.get("args"), dict):
            return action, []
        normalized_args, argument_notes = self._normalize_argument_names(
            entry, action["args"]
        )
        normalized = {"id": entry["name"], "args": normalized_args}
        notes: list[str] = []
        notes.extend(argument_notes)
        if action.get("id") != entry["name"]:
            notes.append(f"normalized action name to {entry['name']!r}")
        # ``TECHLAB`` is a common human/LLM shorthand, but it is an abstract
        # UnitTypeId which Ares' TechUp cannot look up in its technology table.
        # Normalize the concrete SC2 enum spelling without consulting a tactic
        # or phase; action validation is deliberately strategy-agnostic.
        if (
            entry["id"] == "macro.tech_up"
            and normalized["args"].get("desired_tech") == "STARPORT_TECHLAB"
        ):
            normalized["args"]["desired_tech"] = "STARPORTTECHLAB"
            notes.append("normalized desired_tech to STARPORTTECHLAB for BC Rush")
        for param in self.catalog.required_model_params(entry).values():
            name = param["name"]
            type_name = param["type"]
            if type_name == "army_composition":
                value = normalized["args"].get(name)
                repaired = self._normalize_army_composition(value)
                if repaired != value:
                    normalized["args"][name] = repaired
                    notes.append(f"normalized {name} to proportion/priority objects")
            elif type_name in {
                "ability_id",
                "unit_type_id",
                "upgrade_id",
                "unit_or_upgrade_id",
            }:
                value = normalized["args"].get(name)
                repaired = self._normalize_enum_name(value, type_name, name)
                if repaired != value:
                    normalized["args"][name] = repaired
                    notes.append(f"normalized {name} enum name")
            elif type_name == "unit_refs":
                value = normalized["args"].get(name)
                if isinstance(value, list):
                    repaired = [
                        self._canonicalize_known_entity_alias(alias, context)
                        for alias in value
                    ]
                    if repaired != value:
                        normalized["args"][name] = repaired
                        notes.append(f"normalized {name} observation IDs")
            elif type_name in {"unit_ref", "point_or_unit_ref"}:
                value = normalized["args"].get(name)
                repaired = self._canonicalize_known_entity_alias(value, context)
                if repaired != value:
                    normalized["args"][name] = repaired
                    notes.append(f"normalized {name} observation ID")

            if type_name == "grid_ref":
                value = normalized["args"].get(name)
                if isinstance(value, str):
                    repaired = (
                        value.strip().casefold().replace("-", "_").replace(" ", "_")
                    )
                    if repaired != value:
                        normalized["args"][name] = repaired
                        notes.append(f"normalized {name} grid name")
            elif type_name in {"point_ref", "point_or_unit_ref"}:
                value = normalized["args"].get(name)
                if isinstance(value, str) and not context.has_entity_alias(value):
                    repaired = (
                        value.strip().casefold().replace("-", "_").replace(" ", "_")
                    )
                    if repaired in context.positions and repaired != value:
                        normalized["args"][name] = repaired
                        notes.append(f"normalized {name} landmark")

            if param["type"] not in {"point_ref", "point_or_unit_ref"}:
                continue
            value = normalized["args"].get(name)
            if not isinstance(value, dict):
                continue
            corrected = self._clamp_nearby_point(value, bot)
            if corrected != value:
                normalized["args"][name] = corrected
                notes.append(f"clamped {name} to the playable area")
        return normalized, notes

    def _normalize_enum_name(self, value: Any, type_name: str, name: str) -> Any:
        if not isinstance(value, str):
            return value
        if type_name == "ability_id":
            from sc2.ids.ability_id import AbilityId

            return self.adapter._resolve_enum(AbilityId, value, name).name
        if type_name == "upgrade_id":
            from sc2.ids.upgrade_id import UpgradeId

            return self.adapter._resolve_enum(UpgradeId, value, name).name
        if type_name == "unit_type_id":
            from sc2.ids.unit_typeid import UnitTypeId

            return self.adapter._resolve_enum(UnitTypeId, value, name).name

        from sc2.ids.unit_typeid import UnitTypeId
        from sc2.ids.upgrade_id import UpgradeId

        try:
            return self.adapter._resolve_enum(UnitTypeId, value, name).name
        except ValueError:
            return self.adapter._resolve_enum(UpgradeId, value, name).name

    def _normalize_argument_names(
        self, entry: dict[str, Any], args: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        exposed = self.catalog.required_model_params(entry)
        aliases = {self._symbol_key(name): name for name in exposed}
        normalized: dict[str, Any] = {}
        notes: list[str] = []
        for raw_name, value in args.items():
            name = raw_name
            if isinstance(raw_name, str):
                name = aliases.get(self._symbol_key(raw_name), raw_name)
            if name in normalized:
                raise ParameterError.duplicate(str(name))
            normalized[name] = value
            if name != raw_name:
                notes.append(f"normalized parameter name {raw_name!r} to {name!r}")
        return normalized, notes

    def _normalize_army_composition(self, value: Any) -> Any:
        """Repair common unambiguous model shorthands for Ares compositions."""
        if not isinstance(value, dict) or not value:
            return value
        from sc2.ids.unit_typeid import UnitTypeId

        repaired: dict[str, dict[str, Any]] = {}
        for index, (raw_unit_name, settings) in enumerate(value.items()):
            if not isinstance(raw_unit_name, str):
                return value
            requested_name = (
                "BATTLECRUISER"
                if raw_unit_name.strip().casefold() == "bc"
                else raw_unit_name
            )
            try:
                unit_name = self.adapter._resolve_enum(
                    UnitTypeId, requested_name, "army_composition_dict"
                ).name
            except ValueError:
                return value
            if isinstance(settings, bool):
                return value
            if isinstance(settings, (int, float)):
                proportion = float(settings)
                priority = index
            elif isinstance(settings, dict):
                proportion = settings.get("proportion")
                priority = settings.get("priority", index)
            else:
                return value
            if (
                isinstance(proportion, bool)
                or not isinstance(proportion, (int, float))
                or float(proportion) < 0.0
                or isinstance(priority, bool)
                or not isinstance(priority, int)
            ):
                return value
            if float(proportion) == 0.0:
                continue
            repaired[unit_name] = {
                "proportion": float(proportion),
                "priority": priority,
            }
        if not repaired:
            return value
        total = sum(item["proportion"] for item in repaired.values())
        if total <= 0.0:
            return value
        if not isclose(total, 1.0, abs_tol=1e-6):
            for settings in repaired.values():
                settings["proportion"] /= total
        return repaired

    @staticmethod
    def _canonicalize_known_entity_alias(value: Any, context: EntityContext) -> Any:
        """Normalize only aliases that identify a current entity.

        Unknown values are retained for the ordinary resolver to reject as a
        stale ID; landmarks such as ``enemy_main`` are also intentionally left
        untouched.
        """
        try:
            canonical = context.canonical_entity_alias(value)
        except ValueError:
            return value
        return canonical if canonical in context.entities else value

    def _clamp_nearby_point(self, value: dict[str, Any], bot: Any) -> dict[str, Any]:
        if not isinstance(value.get("x"), (int, float)) or not isinstance(
            value.get("y"), (int, float)
        ):
            return value
        game_info = getattr(bot, "game_info", None)
        area = getattr(game_info, "playable_area", None)
        if area is None:
            return value
        try:
            min_x, min_y = float(area.x), float(area.y)
            max_x = min_x + float(area.width) - 1.0
            max_y = min_y + float(area.height) - 1.0
        except (AttributeError, TypeError, ValueError):
            return value
        x, y = float(value["x"]), float(value["y"])
        clamped_x = min(max(x, min_x), max_x)
        clamped_y = min(max(y, min_y), max_y)
        if (clamped_x, clamped_y) == (x, y):
            return value
        distance = max(abs(clamped_x - x), abs(clamped_y - y))
        if distance > self.game_config.max_point_nudge_tiles:
            raise ParameterError.invalid_value(
                "point", value, "a coordinate inside the playable area"
            )
        return {"x": clamped_x, "y": clamped_y}

    @staticmethod
    def _validate_ares_behavior_constraints(
        action_id: str, args: dict[str, Any]
    ) -> None:
        """Reject catalog-valid values that an Ares behavior cannot execute."""
        if action_id not in {"macro.build_structure", "BuildStructure"}:
            return
        structure_name = args.get("structure_id")
        if not isinstance(structure_name, str):
            return
        from ares.dicts.structure_to_building_size import STRUCTURE_TO_BUILDING_SIZE
        from sc2.ids.unit_typeid import UnitTypeId

        try:
            structure_id = UnitTypeId[structure_name]
        except KeyError as exc:
            raise ParameterError.invalid_value(
                "structure_id", structure_name, "a valid structure type"
            ) from exc
        if structure_id not in STRUCTURE_TO_BUILDING_SIZE:
            guidance = "a structure supported by BuildStructure"
            if structure_name == "REFINERY":
                guidance = "GasBuildingController for Refineries"
            elif "TECHLAB" in structure_name or "REACTOR" in structure_name:
                guidance = "TechUp with a concrete unit or technology target for add-ons; not BuildStructure"
            raise ParameterError.invalid_value(
                "structure_id",
                structure_name,
                guidance,
            )

    def _validate_live_args(
        self,
        entry: dict[str, Any],
        args: dict[str, Any],
        context: EntityContext,
        seen_own: set[str],
    ) -> None:
        for param in self.catalog.required_model_params(entry).values():
            if param["name"] not in args:
                continue
            value, type_name, name = args[param["name"]], param["type"], param["name"]
            if type_name == "unit_ref":
                own_only = name in {"unit", "group"}
                entity = context.resolve_entity(value, own_only=own_only)
                if own_only:
                    availability = entry.get("availability", {})
                    allowed_types = availability.get("types")
                    if allowed_types == ["ALL"]:
                        allowed_types = None
                    actual_type = getattr(
                        getattr(entity, "type_id", None), "name", "UNKNOWN"
                    )
                    if allowed_types and actual_type not in allowed_types:
                        allowed = ", ".join(allowed_types)
                        raise ParameterError.invalid_value(
                            name,
                            actual_type,
                            f"an actor of type {allowed} for {entry['name']}",
                        )
                    if value in seen_own:
                        raise ConflictError.unit_reused(value)
                    seen_own.add(value)
            elif type_name == "unit_refs":
                if entry["id"] == "combat.group.keep_group_safe" and name == "close_enemy" and value == []:
                    continue
                if not isinstance(value, list) or not value:
                    raise ParameterError.format(
                        name, "a non-empty observation unit ID list"
                    )
                for alias in value:
                    context.resolve_entity(alias, own_only=name == "group")
                    if name == "group":
                        if alias in seen_own:
                            raise ConflictError.unit_reused(alias)
                        seen_own.add(alias)
            elif type_name == "point_ref":
                point = context.resolve_point(value)
                if point.x < 0 or point.y < 0:
                    raise ParameterError.invalid_value(
                        name, value, "a point on the playable map"
                    )
            elif type_name == "ability_id":
                if not isinstance(value, str):
                    raise ParameterError.format(name, "an ability enum name")
                from sc2.ids.ability_id import AbilityId

                try:
                    ability = AbilityId[value]
                except KeyError as exc:
                    raise ParameterError.invalid_value(
                        name, value, "a valid ability enum name"
                    ) from exc
                actor_alias = args.get("unit")
                if isinstance(actor_alias, str):
                    actor = context.resolve_entity(actor_alias, own_only=True)
                    available = getattr(actor, "abilities", None)
                    if available is not None and ability not in available:
                        raise ParameterError.invalid_value(
                            name,
                            value,
                            f"an ability currently ready for unit {actor_alias}",
                        )
        self._validate_fixed_abilities(entry, args, context)

    @staticmethod
    def _validate_fixed_abilities(
        entry: dict[str, Any], args: dict[str, Any], context: EntityContext
    ) -> None:
        """Check a model-facing high-level action's hidden, fixed ability."""
        actor_alias = args.get("unit")
        if not isinstance(actor_alias, str):
            return
        actor = context.resolve_entity(actor_alias, own_only=True)
        available = getattr(actor, "abilities", None)
        if available is None:
            return
        from sc2.ids.ability_id import AbilityId

        for param in entry["params"]:
            if param.get("input") != "runtime" or param.get("type") != "ability_id":
                continue
            ability_name = param.get("value")
            try:
                ability = AbilityId[ability_name]
            except (KeyError, TypeError) as exc:
                raise ParameterError.invalid_value(
                    "ability", ability_name, "a valid fixed ability enum name"
                ) from exc
            if ability not in available:
                raise ParameterError.invalid_value(
                    "unit",
                    actor_alias,
                    f"a unit with {entry['name']} currently ready",
                )
