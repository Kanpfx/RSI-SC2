"""Compact unit and structure grouping and presentation."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from agent.runtime.actions.resolution.resolver import EntityContext
from agent.runtime.observation.execution_context import safe_mediator
from agent.runtime.observation.state import TagIdMapper

NO_ROUTINE_HP_NAMES = {
    "SCV",
    "MULE",
    "SUPPLYDEPOT",
    "SUPPLYDEPOTLOWERED",
    "REFINERY",
    "REFINERYRICH",
    "ENGINEERINGBAY",
    "ARMORY",
    "SENSORTOWER",
}


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
        grouped: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
        for unit in units:
            name = _type_name(unit)
            state = role_labels.get(unit.tag, self._own_unit_state(unit))
            grouped[(name, state, self._area_label(unit, bot))].append(unit)
        return self._group_blocks(grouped, context.own_entities)


    def enemy_unit_blocks(
        self, units: list[Any], context: EntityContext, bot: Any
    ) -> list[str]:
        grouped: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
        for unit in units:
            state = "attacking" if getattr(unit, "is_attacking", False) else "visible"
            grouped[(_type_name(unit), state, self._area_label(unit, bot))].append(unit)
        return self._group_blocks(grouped, context.enemy_entities)


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
        return self._group_blocks(blocks, None)


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
        return self._group_blocks(grouped, None)


    def structure_blocks(
        self, structures: list[Any], context: EntityContext, bot: Any, *, own: bool
    ) -> list[str]:
        grouped: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
        for structure in structures:
            state = self._structure_state(structure)
            if getattr(structure, "is_flying", False):
                state += " flying"
            if getattr(structure, "has_techlab", False):
                state += f" addon={_type_name(structure)}TECHLAB"
            elif getattr(structure, "has_reactor", False):
                state += f" addon={_type_name(structure)}REACTOR"
            grouped[(_type_name(structure), state, self._area_label(structure, bot))].append(structure)
        target = context.own_entities if own else context.enemy_entities
        return self._group_blocks(grouped, target)


    def _group_blocks(
        self,
        grouped: dict[tuple[str, str, str], list[Any]],
        entity_map: dict[str, Any] | None,
    ) -> list[str]:
        if entity_map is None:
            blocks = []
            for (name, state, location), members in grouped.items():
                blocks.append((name, location, state, f"{name}*{len(members)}@{location} {state}"))
            return [block for *_, block in sorted(blocks)]

        by_area: dict[tuple[str, str], dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
        for (name, state, location), members in grouped.items():
            by_area[(name, location)][state].extend(members)

        blocks = []
        for (name, location), states in sorted(by_area.items()):
            ordered_states = [
                (state, sorted(units, key=lambda unit: unit.tag))
                for state, units in sorted(states.items())
            ]
            members = [unit for _, units in ordered_states for unit in units]
            aliases = {unit.tag: self.ids.alias(unit.tag) for unit in members}
            entity_map.update((aliases[unit.tag], unit) for unit in members)
            status = "; ".join(
                f"{state}:[{','.join(aliases[unit.tag] for unit in units)}]"
                for state, units in ordered_states
            )
            fields = [f"{name}@{location}", status]
            hp = [self._health_value(unit) for unit in members]
            low_hp = any(value.endswith("%") and int(value[:-1]) < 50 for value in hp)
            if name not in NO_ROUTINE_HP_NAMES or low_hp:
                fields.append("hp=[" + ",".join(hp) + "]")
            for field in ("shield", "energy", "cargo"):
                if not any(getattr(unit, field + "_max", 0) > 0 for unit in members):
                    continue
                values = []
                for unit in members:
                    if field == "shield":
                        values.append(self._health_value(unit, field))
                    else:
                        attr = "cargo_used" if field == "cargo" else field
                        value = getattr(unit, attr, None)
                        values.append(str(int(value)) if isinstance(value, (int, float)) else "?")
                fields.append(field + "=[" + ",".join(values) + "]")
            if name == "BATTLECRUISER":
                ability_values = [self._battlecruiser_ability_lines(unit) for unit in members]
                for index, ability in enumerate(("EFFECT_TACTICALJUMP", "YAMATO_YAMATOGUN")):
                    values = [lines[index].split(": ", 1)[1] for lines in ability_values]
                    fields.append(ability + "=[" + ",".join(values) + "]")
            blocks.append(" ".join(fields))
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

