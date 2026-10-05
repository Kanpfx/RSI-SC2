"""Entity positions and key attributes, with compact enemy memory."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from agent.game.actions.resolution.resolver import EntityContext
from agent.game.observation.execution_context import TagIdMapper, safe_mediator


def _type_name(unit: Any) -> str:
    type_name = getattr(getattr(unit, "type_id", None), "name", None)
    if type_name:
        return type_name
    try:
        return getattr(unit, "name", "UNKNOWN") or "UNKNOWN"
    except (AttributeError, KeyError, TypeError, ValueError):
        return "UNKNOWN"


def count_entities(units: list[Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for unit in units:
        name = _type_name(unit)
        result[name] = result.get(name, 0) + 1
    return result


class EntityRenderer:
    def __init__(self, ids: TagIdMapper):
        self.ids = ids

    def _role_labels(self, bot: Any) -> dict[int, str]:
        """Translate only the few Ares roles that make sense to a commander."""
        try:
            from ares.consts import UnitRole

            get_units = bot.mediator.get_units_from_role
        except (AttributeError, ImportError):
            return {}
        labels: dict[int, str] = {}
        for role, label in (
            (UnitRole.ATTACKING_MAIN_SQUAD, "attacking"),
            (UnitRole.ATTACKING, "attacking"),
            (UnitRole.DEFENDING, "defending"),
            (UnitRole.BASE_DEFENDER, "defending"),
            (UnitRole.BUILDING, "constructing"),
            (UnitRole.GATHERING, "gathering"),
            (UnitRole.REPAIRING, "repairing"),
            (UnitRole.MAP_CONTROL, "scouting"),
        ):
            try:
                for unit in get_units(role=role):
                    labels[unit.tag] = label
            except (AttributeError, KeyError, TypeError):
                continue
        return labels


    @staticmethod
    def _battlecruiser_ability_lines(unit: Any) -> list[str]:
        """Expose only the two BC decisions that model can act on directly."""
        abilities = getattr(unit, "abilities", None)
        if abilities is None:
            return [
                "EFFECT_TACTICALJUMP: [Unknown]",
                "YAMATO_YAMATOGUN: [Unknown]",
            ]
        names = {getattr(ability, "name", str(ability)) for ability in abilities}
        return [
            "EFFECT_TACTICALJUMP: ready"
            if "EFFECT_TACTICALJUMP" in names
            else "EFFECT_TACTICALJUMP: unavailable",
            "YAMATO_YAMATOGUN: ready"
            if "YAMATO_YAMATOGUN" in names
            else "YAMATO_YAMATOGUN: unavailable",
        ]


    @staticmethod
    def _health_value(unit: Any, field: str = "health") -> str:
        value = getattr(unit, field, None)
        maximum = getattr(unit, field + "_max", None)
        if isinstance(value, (int, float)) and isinstance(maximum, (int, float)) and maximum > 0:
            return f"{int(100 * value / maximum)}%"
        percentage = getattr(unit, field + "_percentage", None)
        return f"{int(100 * percentage)}%" if isinstance(percentage, (int, float)) else "?"


    def own_unit_blocks(
        self, units: list[Any], context: EntityContext, bot: Any
    ) -> list[str]:
        role_labels = self._role_labels(bot)
        return self._entity_blocks(
            [(unit, role_labels.get(unit.tag, self._own_unit_state(unit))) for unit in units],
            context.own_entities,
        )


    def enemy_unit_blocks(
        self, units: list[Any], context: EntityContext, bot: Any
    ) -> list[str]:
        return self._entity_blocks(
            [(unit, "attacking" if getattr(unit, "is_attacking", False) else "visible")
             for unit in units],
            context.enemy_entities,
        )


    def remembered_enemy_unit_blocks(
        self, units: list[Any], bot: Any
    ) -> list[str]:
        grouped: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for unit in units:
            grouped[(_type_name(unit), self._area_label(unit, bot))].append(unit)

        blocks: dict[tuple[str, str, str], list[Any]] = {}
        for (name, location), members in grouped.items():
            ages: list[int] = []
            for unit in members:
                try:
                    ages.append(max(0, int(float(getattr(unit, "age", 0.0)))))
                except (AttributeError, TypeError, ValueError):
                    ages.append(0)
            youngest, oldest = min(ages), max(ages)
            age = str(youngest) if youngest == oldest else f"{youngest}-{oldest}"
            blocks[
                (
                    name,
                    f"last_seen={age}s_ago",
                    location,
                )
            ] = members
        return self._memory_blocks(blocks)


    def remembered_enemy_structure_blocks(
        self, structures: list[Any], bot: Any
    ) -> list[str]:
        grouped: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
        for structure in structures:
            grouped[
                (
                    _type_name(structure),
                    f"last_seen={int(getattr(structure, 'age', 0))}s_ago",
                    self._area_label(structure, bot),
                )
            ].append(structure)
        return self._memory_blocks(grouped)


    def structure_blocks(
        self, structures: list[Any], context: EntityContext, bot: Any, *, own: bool
    ) -> list[str]:
        entries = []
        for structure in structures:
            state = self._structure_state(structure)
            if getattr(structure, "is_flying", False):
                state += " flying"
            if getattr(structure, "has_techlab", False):
                state += f" addon={_type_name(structure)}TECHLAB"
            elif getattr(structure, "has_reactor", False):
                state += f" addon={_type_name(structure)}REACTOR"
            entries.append((structure, state))
        target = context.own_entities if own else context.enemy_entities
        return self._entity_blocks(entries, target)


    @staticmethod
    def _memory_blocks(grouped: dict[tuple[str, str, str], list[Any]]) -> list[str]:
        return [
            f"{name}*{len(members)}@{location} {state}"
            for (name, state, location), members in sorted(
                grouped.items(), key=lambda item: (item[0][0], item[0][2], item[0][1])
            )
        ]

    def _entity_blocks(
        self, entries: list[tuple[Any, str]], entity_map: dict[str, Any]
    ) -> list[str]:
        blocks = []
        for unit, state in sorted(entries, key=lambda item: (_type_name(item[0]), item[0].tag)):
            name = _type_name(unit)
            alias = self.ids.alias(unit.tag)
            entity_map[alias] = unit
            position = getattr(unit, "position", None)
            location = (
                f"({position.x:.1f},{position.y:.1f})"
                if position is not None else "[Unknown]"
            )
            fields = [f"{alias}@{location}", state]
            hp = self._health_value(unit)
            if hp != "100%":
                fields.append(f"hp={hp}")
            for field in ("shield", "energy", "cargo"):
                if not getattr(unit, field + "_max", 0) > 0:
                    continue
                if field == "shield":
                    value = self._health_value(unit, field)
                else:
                    attr = "cargo_used" if field == "cargo" else field
                    raw = getattr(unit, attr, None)
                    value = str(int(raw)) if isinstance(raw, (int, float)) else "?"
                fields.append(f"{field}={value}")
            if name == "BATTLECRUISER":
                fields.extend(self._battlecruiser_ability_lines(unit))
            blocks.append(f"{name}: " + " ".join(fields))
        return blocks


    @staticmethod
    def _own_unit_state(unit: Any) -> str:
        if getattr(unit, "is_constructing_scv", False):
            return "constructing"
        if getattr(unit, "is_repairing", False):
            return "repairing"
        if getattr(unit, "is_attacking", False):
            return "attacking"
        if getattr(unit, "is_idle", False):
            return "idle"
        if _type_name(unit) == "SCV":
            return "working"
        return "active"


    def _area_label(self, unit: Any, bot: Any) -> str:
        """Group entities by nearby expansion or map landmark."""
        position = getattr(unit, "position", None)
        if position is None:
            return "[Unknown]"
        main = getattr(bot, "start_location", None)
        candidates = [
            ("main", main),
            ("natural", safe_mediator(bot, "get_own_nat")),
            ("enemy_main", (getattr(bot, "enemy_start_locations", []) or [None])[0]),
            ("enemy_natural", safe_mediator(bot, "get_enemy_nat")),
        ]
        known_points = [point for _, point in candidates if point is not None]
        candidates = [
            (f"{name}({int(point.x)},{int(point.y)})" if point is not None else name, point)
            for name, point in candidates
        ]
        expansions = []
        for point in getattr(bot, "expansion_locations_list", []) or []:
            try:
                if any((point.x - known.x) ** 2 + (point.y - known.y) ** 2 <= 64 for known in known_points):
                    continue
                distance = (point.x - main.x) ** 2 + (point.y - main.y) ** 2 if main is not None else 0
                expansions.append((distance, point.x, point.y, point))
            except (AttributeError, TypeError):
                continue
        for index, (_, _, _, point) in enumerate(sorted(expansions, key=lambda item: item[:3]), start=1):
            candidates.append((f"expansion_{index}", point))
        center = getattr(getattr(bot, "game_info", None), "map_center", None)
        if center is not None:
            candidates.append((f"center({int(center.x)},{int(center.y)})", center))
        closest_name = "elsewhere"
        closest_distance = float("inf")
        for name, point in candidates:
            if point is None:
                continue
            try:
                distance = (position.x - point.x) ** 2 + (position.y - point.y) ** 2
            except AttributeError:
                continue
            if distance < closest_distance:
                closest_name, closest_distance = name, distance
        return closest_name if closest_distance <= 400 else "elsewhere"


    @staticmethod
    def _structure_state(structure: Any) -> str:
        progress = float(getattr(structure, "build_progress", 1.0))
        if progress < 1.0:
            return f"building={int(progress * 100)}%"
        if _type_name(structure) == "SUPPLYDEPOTLOWERED":
            return "lowered"
        if getattr(structure, "is_idle", False):
            return "idle"
        return "busy" if getattr(structure, "orders", []) else "ready"

