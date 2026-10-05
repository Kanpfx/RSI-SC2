"""Current-frame aliases and controlled landmarks used by model instructions."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from agent.game.actions.errors import ResolveError

RUNTIME_SOURCES = {"group_tags", "group_center", "enemy_center", "group_grid", "unit_grid",
                   "unit_position", "worker_position", "resource_target_pos", "aoe_min_targets",
                   "unit_planned_path", "aoe_ability_delay"}


def _normalized_label(value: str) -> str:
    return value.strip().casefold().replace("-", "_").replace(" ", "_")


@dataclass
class EntityContext:
    own_entities: dict[str, Any] = field(default_factory=dict)
    enemy_entities: dict[str, Any] = field(default_factory=dict)
    positions: dict[str, Any] = field(default_factory=dict)
    grids: dict[str, Any] = field(default_factory=dict)
    known_own_aliases: set[str] = field(default_factory=set)
    bot: Any = None
    max_point_nudge_tiles: float = 2.0
    runtime_values: dict[str, Any] = field(default_factory=dict)

    def runtime_value(self, source: str, args: dict[str, Any]) -> Any:
        from sc2.position import Point2
        context = self
        rule = source
        bot = self.bot
        def center(units):
            if not units:
                raise ResolveError.invalid_value("runtime", rule, "selected unit list is empty")
            return Point2((sum(u.position.x for u in units)/len(units), sum(u.position.y for u in units)/len(units)))
        if rule == "group_tags":
            return {u.tag for u in args["group"]}
        if rule == "group_center":
            return center(args["group"])
        if rule == "enemy_center":
            return center(args["enemies"])
        if rule in ("group_grid", "unit_grid"):
            units = args["group"] if rule == "group_grid" else [args["unit"]]
            flying = {u.is_flying for u in units}
            if len(flying) != 1:
                raise ResolveError.invalid_value("runtime", rule, "split mixed air/ground groups; group must be non-empty")
            from agent.game.observation.execution_context import safe_mediator
            grid = safe_mediator(bot, "get_air_grid" if flying.pop() else "get_ground_grid")
            if grid is None:
                raise ResolveError.invalid_value("runtime", rule, "influence grid unavailable")
            return grid
        if rule in ("unit_position", "worker_position"):
            return args["unit" if rule == "unit_position" else "worker"].position
        if rule == "resource_target_pos":
            target = args["target"]
            if getattr(target, "is_mineral_field", False):
                point = bot.mediator.get_mineral_target_dict.get(target.position)
                if point is not None:
                    return point
            elif (getattr(target, "is_vespene_geyser", False) or getattr(target, "is_gas_building", False)) and target.tag in {u.tag for u in context.own_entities.values()}:
                return target.position.towards(args["worker"].position, 2.75 * 1.21)
            raise ResolveError.invalid_value("runtime", rule, "resource approach point unavailable")
        if rule == "aoe_min_targets":
            name = args["ability_id"].name
            if name in ("EFFECT_CORROSIVEBILE", "KD8CHARGE_KD8CHARGE"):
                return 1
            return 2 if name == "EMP_EMP" and bot.enemy_race.name != "Protoss" else 4
        if rule == "unit_planned_path":
            path = context.runtime_values.get("unit_planned_paths", {}).get(args["unit"].tag)
            if path:
                return list(path)
        if rule == "aoe_ability_delay" and args["aoe_ability"].name == "KD8CHARGE_KD8CHARGE":
            return 34
        raise ResolveError.invalid_value("runtime", rule, "runtime dependency unavailable")

    @property
    def entities(self) -> dict[str, Any]:
        return self.own_entities | self.enemy_entities

    def observation_id(self, tag: Any) -> str:
        return next((alias for alias, unit in self.entities.items() if str(unit.tag) == str(tag)),
                    "[Unknown]")

    @staticmethod
    def canonical_entity_alias(alias: Any) -> str:
        """Accept common scalar representations of an observation ``[id]``.

        Observations deliberately display compact aliases as ``[851]``. A
        model may faithfully copy that label as ``851``, ``"851"``, or
        ``"[851]"``. All three identify the same current-frame entity; this
        conversion is deterministic and must not consume another model turn.
        """
        if isinstance(alias, bool):
            raise ResolveError.format("unit", "an observation unit ID, not a boolean")
        if isinstance(alias, int):
            return str(alias)
        if isinstance(alias, float):
            if alias.is_integer():
                return str(int(alias))
            raise ResolveError.format("unit", "an integer observation unit ID")
        if isinstance(alias, str):
            normalized = alias.strip()
            if normalized.startswith("[") and normalized.endswith("]"):
                normalized = normalized[1:-1].strip()
            if normalized:
                return normalized
        raise ResolveError.format("unit", "an observation [id] label")

    def has_entity_alias(self, alias: Any) -> bool:
        try:
            return self.canonical_entity_alias(alias) in self.entities
        except ResolveError:
            return False

    def resolve_entity(self, alias: Any, *, own_only: bool = False) -> Any:
        source = self.own_entities if own_only else self.entities
        canonical = self.canonical_entity_alias(alias)
        try:
            return source[canonical]
        except KeyError as exc:
            reason = "unit disappeared" if canonical in self.known_own_aliases else (
                "unit ownership mismatch" if canonical in self.entities else "unknown or unavailable unit ID"
            )
            category = "action_unavailable" if canonical in self.entities else "entity_missing"
            raise ResolveError(reason, f"unit {canonical}: {reason}", parameter="unit", actual=canonical, category=category) from exc

    def resolve_point(self, value: Any) -> Any:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            value = dict(zip(("x", "y"), value))
        elif isinstance(value, dict):
            normalized = {key.casefold() if isinstance(key, str) else key: item
                          for key, item in value.items()}
            if len(normalized) != len(value):
                raise ResolveError.format("point", "unique x and y coordinate keys")
            value = normalized
        if isinstance(value, str):
            try:
                point = self.positions[_normalized_label(value)]
                value = {"x": point.x, "y": point.y}
            except KeyError as exc:
                raise ResolveError.invalid_value(
                    "point", value, "a known landmark or an {x, y} coordinate"
                ) from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"x", "y"}
            or any(type(value.get(k)) not in (int, float) for k in ("x", "y"))
        ):
            raise ResolveError.format(
                "point", "a known landmark or an {x, y} coordinate object"
            )
        from sc2.position import Point2

        try:
            x, y = float(value["x"]), float(value["y"])
        except OverflowError as exc:
            raise ResolveError.format("point", "finite coordinates") from exc
        if not math.isfinite(x) or not math.isfinite(y):
            raise ResolveError.format("point", "finite coordinates")
        area = getattr(getattr(self.bot, "game_info", None), "playable_area", None)
        if area is not None:
            cx = min(max(x, area.x), area.x + area.width - 1.0)
            cy = min(max(y, area.y), area.y + area.height - 1.0)
            if max(abs(cx - x), abs(cy - y)) > self.max_point_nudge_tiles:
                raise ResolveError.invalid_value("point", value, "a coordinate inside the playable area")
            x, y = cx, cy
        elif x < 0 or y < 0:
            raise ResolveError.invalid_value("point", value, "nonnegative map coordinates")
        return Point2((x, y))
