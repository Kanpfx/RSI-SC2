"""Current-frame aliases and controlled landmarks used by model instructions."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from agent.runtime.actions.errors import ResolveError

RUNTIME_SOURCES = {"group_tags", "group_center", "combat_children", "macro_children", "mining_action_times"}


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
        if source in {"group_tags", "group_center"}:
            group = args.get("group", [])
            if not group:
                raise ResolveError.format("group", "a non-empty resolved group")
            if source == "group_tags":
                return {unit.tag for unit in group}
            from sc2.position import Point2
            return Point2((sum(u.position.x for u in group) / len(group),
                           sum(u.position.y for u in group) / len(group)))
        if source in {"combat_children", "macro_children"}:
            return list(self.runtime_values.get(source, []))
        if source == "mining_action_times":
            return self.runtime_values.setdefault(source, {})
        raise ResolveError.invalid_value("source", source, "a registered runtime source")

    @property
    def entities(self) -> dict[str, Any]:
        return self.own_entities | self.enemy_entities

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
            raise ResolveError(reason, f"unit {canonical}", parameter="unit", actual=canonical) from exc

    def resolve_point(self, value: Any) -> Any:
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
