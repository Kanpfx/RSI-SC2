"""Ares/python-sc2 state -> one compact model observation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from agent.runtime.observation.action_history import ActionHistory
from agent.runtime.actions.resolver import EntityContext
from agent.runtime.observation.hints import SituationHintBuilder
from agent.runtime.observation.overview import OverviewBuilder
from agent.runtime.observation.renderer import observation_text
from agent.runtime.observation.state import TagIdMapper
from agent.runtime.observation.technology import ProductionTechnologyBuilder, display_name

PENDING_NAMES = (
    "COMMANDCENTER",
    "ORBITALCOMMAND",
    "SUPPLYDEPOT",
    "REFINERY",
    "BARRACKS",
    "FACTORY",
    "STARPORT",
    "FUSIONCORE",
    "STARPORTTECHLAB",
    "MARINE",
    "REAPER",
    "MARAUDER",
    "GHOST",
    "HELLION",
    "WIDOWMINE",
    "CYCLONE",
    "SIEGETANK",
    "THOR",
    "VIKINGFIGHTER",
    "MEDIVAC",
    "LIBERATOR",
    "BANSHEE",
    "RAVEN",
    "BATTLECRUISER",
)

IMPORTANT_UNIT_NAMES = {
    "BATTLECRUISER",
    "MEDIVAC",
    "RAVEN",
    "GHOST",
    "SIEGETANK",
    "SIEGETANKSIEGED",
}
COMPACT_UNIT_NAMES = {"SCV", "MARINE", "MULE"}
IMPORTANT_STRUCTURE_NAMES = {
    "COMMANDCENTER",
    "ORBITALCOMMAND",
    "PLANETARYFORTRESS",
    "FACTORY",
    "STARPORT",
    "FUSIONCORE",
    "STARPORTTECHLAB",
}
COMPACT_STRUCTURE_NAMES = {
    "SUPPLYDEPOT",
    "SUPPLYDEPOTLOWERED",
    "REFINERY",
    "REFINERYRICH",
    "BUNKER",
    "MISSILETURRET",
}


@dataclass
class Observation:
    iteration: int
    counts: dict[str, int]
    text: str
    context: EntityContext


def _type_name(unit: Any) -> str:
    type_name = getattr(getattr(unit, "type_id", None), "name", None)
    if type_name:
        return type_name
    try:
        return getattr(unit, "name", "UNKNOWN") or "UNKNOWN"
    except (AttributeError, KeyError, TypeError, ValueError):
        return "UNKNOWN"


def _count(units: list[Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for unit in units:
        name = _type_name(unit)
        result[name] = result.get(name, 0) + 1
    return result


class ObservationBuilder:
    """Build the canonical observation and preserve only useful cross-frame state."""

    def __init__(self, ids: TagIdMapper):
        self.ids = ids
        self._known_own_aliases: set[str] = set()
        self._controlled_aliases: set[str] = set()
        self.overview_builder = OverviewBuilder()
        self.hint_builder = SituationHintBuilder()
        self.technology_builder = ProductionTechnologyBuilder()
        self.action_history = ActionHistory()
        self._previous_facts: dict[str, int | str] | None = None

    def record_registered_actions(
        self, actions: list[dict[str, Any]], time: str = "--:--"
    ) -> None:
        for action in actions:
            self.action_history.record(action, time, "accepted")

    def sync_active_actions(
        self, actions: list[dict[str, Any]], time: str = "--:--"
    ) -> None:
        self.action_history.sync_active(actions, time)
        self._controlled_aliases = set()
        for action in actions:
            args = action.get("args", {})
            for key in ("unit", "target", "structure"):
                value = args.get(key)
                if isinstance(value, (str, int)):
                    self._controlled_aliases.add(str(value).strip("[]"))
            for key in ("units", "group"):
                values = args.get(key, [])
                if isinstance(values, list):
                    self._controlled_aliases.update(str(value).strip("[]") for value in values)

    def record_failed_actions(
        self, actions: list[Any], time: str = "--:--", reason: str = ""
    ) -> None:
        for action in actions:
            self.action_history.record(action, time, "failed", reason)

    def collect_frame(self, bot: Any) -> None:
        """Collect short-lived facts even when no model observation is due."""
        self.hint_builder.collect_frame(bot)

    def build(self, bot: Any, iteration: int) -> Observation:
        own_units = list(bot.units)
        structures = list(bot.structures)
        enemies = [
            unit
            for unit in bot.enemy_units
            if getattr(unit, "is_visible", True)
            and not getattr(unit, "is_memory", False)
        ]
        remembered_enemies = [
            unit for unit in bot.enemy_units if getattr(unit, "is_memory", False)
        ]
        enemy_structures = [
            unit for unit in bot.enemy_structures if getattr(unit, "is_visible", True)
        ]
        remembered_structures = [
            unit
            for unit in bot.enemy_structures
            if not getattr(unit, "is_visible", True)
        ]
        own_counts = _count(own_units)
        structure_counts = _count(structures)
        pending = self._pending_counts(bot)
        counts = dict(own_counts)
        counts.update(structure_counts)
        counts.update({f"pending:{name}": value for name, value in pending.items()})

        context = self.execution_context(bot)
        own_unit_blocks = self._own_unit_blocks(own_units, context, bot)
        own_structure_blocks = self._structure_blocks(
            structures, context, bot, own=True
        )
        enemy_unit_blocks = self._enemy_unit_blocks(enemies, context, bot)
        enemy_structure_blocks = self._structure_blocks(
            enemy_structures, context, bot, own=False
        )
        facts = self._facts(own_counts, structures, enemies, enemy_structures, bot)
        overview = self.overview_builder.build(
            bot,
            structures,
            own_units,
            enemies,
            enemy_structures,
        )
        data = {
            "overview": overview,
            "situational_hints": self.hint_builder.build(bot),
            "own_unit_blocks": own_unit_blocks,
            "own_structure_blocks": own_structure_blocks,
            "enemy_unit_blocks": enemy_unit_blocks,
            "enemy_structure_blocks": enemy_structure_blocks,
            "remembered_enemy_unit_blocks": self._remembered_enemy_unit_blocks(
                remembered_enemies, bot
            ),
            "remembered_enemy_structure_blocks": (
                self._remembered_enemy_structure_blocks(remembered_structures, bot)
            ),
            "production_and_technology": self.technology_builder.build(
                bot, structures, pending
            ),
            "action_history": self._action_history_text(),
            "recent_changes": self._recent_changes(facts),
        }
        self._previous_facts = facts
        return Observation(iteration, counts, observation_text(data), context)

    def execution_context(self, bot: Any) -> EntityContext:
        """Build a lightweight current-frame context without rendering a prompt."""
        context = EntityContext(bot=bot, )
        self._add_execution_context(bot, context)
        for entity in list(bot.units) + list(bot.structures):
            context.own_entities[self.ids.alias(entity.tag)] = entity
        for entity in list(bot.enemy_units) + list(bot.enemy_structures):
            if getattr(entity, "is_visible", True) and not getattr(
                entity, "is_memory", False
            ):
                context.enemy_entities[self.ids.alias(entity.tag)] = entity
        self._known_own_aliases.update(context.own_entities)
        context.known_own_aliases = set(self._known_own_aliases)
        return context

    def _add_execution_context(self, bot: Any, context: EntityContext) -> None:
        candidates = {
            "main": getattr(bot, "start_location", None),
            "natural": self._safe_mediator(bot, "get_own_nat"),
            "enemy_main": (getattr(bot, "enemy_start_locations", []) or [None])[0],
        }
        context.positions.update(
            {name: point for name, point in candidates.items() if point is not None}
        )
        for alias, attr in {
            "ground": "get_ground_grid",
            "air": "get_air_grid",
            "ground_avoidance": "get_ground_avoidance_grid",
            "air_avoidance": "get_air_avoidance_grid",
            "tactical_ground": "get_tactical_ground_grid",
        }.items():
            value = self._safe_mediator(bot, attr)
            if value is not None:
                context.grids[alias] = value

    def _pending_counts(self, bot: Any) -> dict[str, int]:
        from sc2.ids.unit_typeid import UnitTypeId

        pending: dict[str, int] = {}
        for name in PENDING_NAMES:
            if hasattr(UnitTypeId, name):
                pending[name] = int(bot.already_pending(getattr(UnitTypeId, name)))
        return pending

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
                "Tactical Jump: [Unknown]",
                "Yamato Cannon: [Unknown]",
            ]
        names = {getattr(ability, "name", str(ability)) for ability in abilities}
        return [
            "Tactical Jump: ready"
            if "EFFECT_TACTICALJUMP" in names
            else "Tactical Jump: unavailable",
            "Yamato Cannon: ready"
            if "YAMATO_YAMATOGUN" in names
            else "Yamato Cannon: unavailable",
        ]

    @staticmethod
    def _health_value(unit: Any, field: str = "health") -> str:
        value = getattr(unit, field, None)
        maximum = getattr(unit, field + "_max", None)
        if isinstance(value, (int, float)) and isinstance(maximum, (int, float)) and maximum > 0:
            return f"{int(100 * value / maximum)}%"
        percentage = getattr(unit, field + "_percentage", None)
        return f"{int(100 * percentage)}%" if isinstance(percentage, (int, float)) else "?"

    @classmethod
    def _needs_detail(cls, unit: Any) -> bool:
        return (
            _type_name(unit) in IMPORTANT_UNIT_NAMES | IMPORTANT_STRUCTURE_NAMES
            or cls._health_value(unit) not in {"100%", "?"}
            or cls._health_value(unit, "shield") not in {"100%", "?"}
            or getattr(unit, "energy_max", 0) > 0
            or getattr(unit, "cargo_max", 0) > 0
        )

    def _position_label(self, unit: Any, bot: Any) -> str:
        position = getattr(unit, "position", None)
        if position is None:
            return "[Unknown]"
        try:
            coordinates = f"({int(position.x)}, {int(position.y)})"
        except (AttributeError, TypeError, ValueError):
            coordinates = "[Unknown]"
        candidates = (
            ("our main", getattr(bot, "start_location", None)),
            ("our natural", self._safe_mediator(bot, "get_own_nat")),
            (
                "enemy main",
                (getattr(bot, "enemy_start_locations", []) or [None])[0],
            ),
            ("enemy natural", self._safe_mediator(bot, "get_enemy_nat")),
        )
        nearest_name = "on the map"
        nearest_distance = float("inf")
        for name, point in candidates:
            if point is None:
                continue
            try:
                distance = (position.x - point.x) ** 2 + (position.y - point.y) ** 2
            except AttributeError:
                continue
            if distance < nearest_distance:
                nearest_name, nearest_distance = name, distance
        if nearest_distance <= 400:
            return f"{coordinates}, near {nearest_name}"
        return coordinates

    def _own_unit_blocks(
        self, units: list[Any], context: EntityContext, bot: Any
    ) -> list[str]:
        role_labels = self._role_labels(bot)
        grouped: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
        for unit in units:
            name = _type_name(unit)
            state = role_labels.get(unit.tag, self._own_unit_state(unit))
            grouped[(name, state, self._area_label(unit, bot))].append(unit)
        return self._group_blocks(grouped, context.own_entities, own=True)

    def _enemy_unit_blocks(
        self, units: list[Any], context: EntityContext, bot: Any
    ) -> list[str]:
        grouped: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
        for unit in units:
            state = "attacking" if getattr(unit, "is_attacking", False) else "visible"
            grouped[(_type_name(unit), state, self._area_label(unit, bot))].append(unit)
        return self._group_blocks(grouped, context.enemy_entities)

    def _remembered_enemy_unit_blocks(
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

    def _remembered_enemy_structure_blocks(
        self, structures: list[Any], bot: Any
    ) -> list[str]:
        grouped: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
        for structure in structures:
            grouped[
                (
                    _type_name(structure),
                    f"last_seen={int(getattr(structure, 'age', 0))}s_ago",
                    self._position_label(structure, bot),
                )
            ].append(structure)
        return self._group_blocks(grouped, None)

    def _structure_blocks(
        self, structures: list[Any], context: EntityContext, bot: Any, *, own: bool
    ) -> list[str]:
        grouped: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
        for structure in structures:
            state = self._structure_state(structure)
            if getattr(structure, "is_flying", False):
                state += " flying"
            if getattr(structure, "has_techlab", False):
                state += " addon=TechLab"
            elif getattr(structure, "has_reactor", False):
                state += " addon=Reactor"
            grouped[(_type_name(structure), state, self._area_label(structure, bot))].append(structure)
        target = context.own_entities if own else context.enemy_entities
        return self._group_blocks(grouped, target, own=own)

    def _group_blocks(
        self,
        grouped: dict[tuple[str, str, str], list[Any]],
        entity_map: dict[str, Any] | None,
        *,
        own: bool = False,
    ) -> list[str]:
        blocks = []
        for (name, state, location), members in grouped.items():
            members.sort(key=lambda unit: getattr(unit, "tag", 0))
            detailed = entity_map is not None and any(
                self._needs_detail(unit) or (own and self.ids.alias(unit.tag) in self._controlled_aliases)
                for unit in members
            )
            ordinary_workers = own and name in {"SCV", "MULE"} and state in {"working", "gathering", "idle"}
            ordinary_structures = own and name in (COMPACT_STRUCTURE_NAMES | {"BARRACKS", "ENGINEERINGBAY", "ARMORY"}) and state.split()[0] in {"ready", "idle", "lowered", "busy"}
            show_ids = entity_map is not None and (detailed or not (ordinary_workers or ordinary_structures))
            label = self._display_name(name)
            if show_ids:
                aliases = [self.ids.alias(unit.tag) for unit in members]
                entity_map.update(zip(aliases, members))
                label += "[" + ",".join(aliases) + "]"
            else:
                label += f"*{len(members)}"
            fields = [label, f"@{location}", state]
            if detailed:
                fields.append("health=[" + ",".join(self._health_value(unit) for unit in members) + "]")
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
                    for index, ability in enumerate(("Tactical Jump", "Yamato Cannon")):
                        values = [self._battlecruiser_ability_lines(unit)[index].split(": ", 1)[1] for unit in members]
                        fields.append(ability + "=[" + ",".join(values) + "]")
            blocks.append(((0 if detailed else 1, name, location, state), " ".join(fields)))
        return [block for _, block in sorted(blocks)]

    @staticmethod
    def _display_name(name: str, *, plural: bool = False) -> str:
        overrides = {
            "SUPPLYDEPOTLOWERED": "Supply Depot",
            "REFINERYRICH": "Rich Refinery",
            "SIEGETANKSIEGED": "Siege Tank (Sieged)",
            "WIDOWMINEBURROWED": "Widow Mine (Burrowed)",
            "VIKINGASSAULT": "Viking (Assault)",
            "LIBERATORAG": "Liberator (Defender Mode)",
        }
        return overrides.get(name, display_name(name, 2 if plural else 1))

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

    def _enemy_state(self, unit: Any, bot: Any) -> str:
        state = "attacking" if getattr(unit, "is_attacking", False) else "visible"
        return f"{state}; {self._enemy_area_label(unit, bot)}"

    def _enemy_area_label(self, unit: Any, bot: Any) -> str:
        """Locate this specific enemy instead of applying a global threat flag."""
        ground = self._safe_mediator(bot, "get_ground_enemy_near_bases") or {}
        air = self._safe_mediator(bot, "get_flying_enemy_near_bases") or {}
        for index, base in enumerate(getattr(bot, "townhalls", [])):
            try:
                nearby_tags = set(ground.get(base.tag, set())) | set(
                    air.get(base.tag, set())
                )
            except AttributeError:
                nearby_tags = set()
            if getattr(unit, "tag", None) in nearby_tags:
                return (
                    "near our main"
                    if index == 0
                    else (
                        "near our natural"
                        if index == 1
                        else f"near our base {index + 1}"
                    )
                )
        return self._area_label(unit, bot)

    def _area_label(self, unit: Any, bot: Any) -> str:
        """Group nearby entities in small spatial cells with a shared landmark."""
        position = getattr(unit, "position", None)
        if position is None:
            return "[Unknown]"
        candidates = (
            ("our main", getattr(bot, "start_location", None)),
            ("our natural", self._safe_mediator(bot, "get_own_nat")),
            (
                "enemy main",
                (getattr(bot, "enemy_start_locations", []) or [None])[0],
            ),
            ("enemy natural", self._safe_mediator(bot, "get_enemy_nat")),
            (
                "map center",
                getattr(getattr(bot, "game_info", None), "map_center", None),
            ),
        )
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
        cell = f"({int(position.x // 12) * 12 + 6},{int(position.y // 12) * 12 + 6})"
        names = {"our main": "main", "our natural": "natural", "enemy main": "enemy_main", "enemy natural": "enemy_natural", "map center": "center"}
        return f"{names[closest_name]}{cell}" if closest_distance <= 400 else cell

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

    def _facts(
        self,
        own_counts: dict[str, int],
        structures: list[Any],
        enemies: list[Any],
        enemy_structures: list[Any],
        bot: Any,
    ) -> dict[str, int | str]:
        facts: dict[str, int | str] = {}
        facts.update({f"own:{name}": count for name, count in own_counts.items()})
        structure_counts = _count(structures)
        ready_structure_counts = _count(
            [
                structure
                for structure in structures
                if float(getattr(structure, "build_progress", 1.0)) >= 1.0
            ]
        )
        facts.update(
            {f"structure:{name}": count for name, count in structure_counts.items()}
        )
        facts.update(
            {
                f"ready_structure:{name}": count
                for name, count in ready_structure_counts.items()
            }
        )
        facts.update(
            {f"enemy:{name}": count for name, count in _count(enemies).items()}
        )
        facts.update(
            {
                f"enemy_structure:{name}": count
                for name, count in _count(enemy_structures).items()
            }
        )
        facts["threat"] = (
            "present"
            if self._safe_mediator(bot, "get_ground_enemy_near_bases")
            else "none"
        )
        return facts

    def _recent_changes(self, facts: dict[str, int | str]) -> list[str]:
        if self._previous_facts is None:
            return []
        changes: list[str] = []
        for key in sorted(set(facts) | set(self._previous_facts)):
            before = self._previous_facts.get(key, 0)
            after = facts.get(key, 0)
            if before == after:
                continue
            if key == "threat":
                changes.append(
                    "A visible ground threat is near our base."
                    if after == "present"
                    else "The visible ground threat near our base is gone."
                )
            elif (
                key.startswith("enemy_structure:")
                and isinstance(after, int)
                and after > before
            ):
                changes.append(
                    f"Enemy {self._display_name(key.split(':', 1)[1])} first seen."
                )
            elif key.startswith("own:") and isinstance(after, int) and after > before:
                changes.append(
                    f"Our {self._display_name(key.split(':', 1)[1])} count increased to {after}."
                )
            elif (
                key.startswith("ready_structure:")
                and isinstance(after, int)
                and after > before
            ):
                changes.append(
                    f"Our {self._display_name(key.split(':', 1)[1])} became ready."
                )
            elif (
                key.startswith("structure:")
                and isinstance(after, int)
                and after > before
            ):
                name = key.split(":", 1)[1]
                ready_key = f"ready_structure:{name}"
                ready_increased = facts.get(ready_key, 0) > self._previous_facts.get(
                    ready_key, 0
                )
                if not ready_increased:
                    changes.append(f"Our {self._display_name(name)} started building.")
            if len(changes) == 5:
                break
        return changes

    def _action_history_text(self) -> str:
        return self.action_history.render()

    @staticmethod
    def _safe_mediator(bot: Any, attribute: str) -> Any:
        try:
            return getattr(bot.mediator, attribute)
        except (AttributeError, KeyError):
            return None
