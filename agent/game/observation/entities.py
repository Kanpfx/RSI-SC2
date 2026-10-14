"""Entity positions and key attributes, with compact enemy memory."""

from __future__ import annotations

import json
from collections import defaultdict
from itertools import chain
from typing import Any

from ares.consts import ALL_STRUCTURES
from sc2.dicts.unit_research_abilities import RESEARCH_INFO
from sc2.dicts.unit_train_build_abilities import TRAIN_INFO

from agent.game.actions.formatting import format_action
from agent.game.actions.policy.rules import actor_parameter
from agent.game.actions.resolution.resolver import EntityContext
from agent.game.observation.execution_context import TagIdMapper
from agent.paths import KNOWLEDGE_ROOT


COMPACT_UNIT_TYPES = frozenset({"SCV", "DRONE", "PROBE", "LARVA"})
COMPACT_STRUCTURE_TYPES = frozenset({
    "SUPPLYDEPOT", "SUPPLYDEPOTLOWERED", "PYLON",
    "REFINERY", "REFINERYRICH", "ASSIMILATOR", "ASSIMILATORRICH",
    "EXTRACTOR", "EXTRACTORRICH",
    "BARRACKSTECHLAB", "FACTORYTECHLAB", "STARPORTTECHLAB",
    "BARRACKSREACTOR", "FACTORYREACTOR", "STARPORTREACTOR",
})
PRODUCTION_ABILITIES = frozenset(
    data["ability"].name
    for producer, products in TRAIN_INFO.items() if producer in ALL_STRUCTURES
    for product, data in products.items() if product not in ALL_STRUCTURES
)
RESEARCH_ABILITIES = frozenset(
    data["ability"].name for upgrades in RESEARCH_INFO.values() for data in upgrades.values()
)


def _type_name(unit: Any) -> str:
    type_name = getattr(getattr(unit, "type_id", None), "name", None)
    if type_name:
        return type_name
    try:
        return getattr(unit, "name", "UNKNOWN") or "UNKNOWN"
    except (AttributeError, KeyError, TypeError, ValueError):
        return "UNKNOWN"


class EntityRenderer:
    def __init__(self, ids: TagIdMapper):
        self.ids = ids
        self._ability_whitelist = {
            entry["unit"]: frozenset(entry["abilities"])
            for entry in json.loads((KNOWLEDGE_ROOT / "terran_abilities.json").read_text(encoding="utf-8"))
        }
        self._active_states: dict[str, list[str]] = {}
        self._structure_memory: dict[int, tuple[Any, float]] = {}

    def update_structure_memory(self, bot: Any) -> None:
        """Refresh last-visible snapshots each frame; discard absent buildings."""
        now = float(bot.time)
        memory = {}
        for structure in bot.enemy_structures:
            if getattr(structure, "is_visible", True) and not getattr(structure, "is_memory", False):
                memory[structure.tag] = (structure, now)
            elif structure.tag in self._structure_memory:
                memory[structure.tag] = self._structure_memory[structure.tag]
        self._structure_memory = memory

    def sync_active_actions(self, actions: list[dict[str, Any]]) -> None:
        states: dict[str, list[str]] = defaultdict(list)
        for action in actions:
            param = actor_parameter({"name": action["id"]})
            actors = action["args"].get(param) if param else None
            if actors is None:
                continue
            label = format_action({
                "id": action["id"],
                "args": {key: value for key, value in action["args"].items() if key != param},
            })
            for actor in actors if isinstance(actors, list) else [actors]:
                states[EntityContext.canonical_entity_alias(actor)].append(label)
        self._active_states = dict(states)

    @staticmethod
    def _health_value(unit: Any, field: str = "health") -> str:
        value = getattr(unit, field, None)
        maximum = getattr(unit, field + "_max", None)
        if isinstance(value, (int, float)) and isinstance(maximum, (int, float)) and maximum > 0:
            return f"{int(100 * value / maximum)}%"
        percentage = getattr(unit, field + "_percentage", None)
        return f"{int(100 * percentage)}%" if isinstance(percentage, (int, float)) else "?"

    def own_unit_blocks(self, units: list[Any], bot: Any) -> list[str]:
        return self._entity_blocks(units, COMPACT_UNIT_TYPES, bot, own=True)

    def enemy_unit_blocks(self, units: list[Any], bot: Any) -> list[str]:
        return self._entity_blocks(units, COMPACT_UNIT_TYPES, bot, own=False)

    def remembered_enemy_unit_blocks(self, units: list[Any], bot: Any) -> list[str]:
        ages = {unit.tag: max(0, int(unit.age)) for unit in units}
        return self._entity_blocks(
            units, COMPACT_UNIT_TYPES, bot, own=False, memory_ages=ages
        )

    def remembered_enemy_structure_blocks(
        self, structures: list[Any], bot: Any
    ) -> list[str]:
        now = float(bot.time)
        snapshots, ages = [], {}
        for structure in structures:
            saved = self._structure_memory.get(structure.tag)
            snapshot, seen_at = saved if saved else (structure, None)
            snapshots.append(snapshot)
            ages[structure.tag] = max(0, int(now - seen_at)) if seen_at is not None else None
        return self._entity_blocks(
            snapshots, COMPACT_STRUCTURE_TYPES, bot, own=False, memory_ages=ages
        )

    def structure_blocks(
        self, structures: list[Any], bot: Any, *, own: bool
    ) -> list[str]:
        return self._entity_blocks(structures, COMPACT_STRUCTURE_TYPES, bot, own=own)

    @staticmethod
    def _memory_age(ages: list[int | None]) -> str:
        if any(age is None for age in ages):
            return "unknown"
        youngest, oldest = min(ages), max(ages)
        return f"{youngest}s ago" if youngest == oldest else f"{youngest}-{oldest}s ago"

    def _entity_blocks(
        self, units: list[Any], compact_types: frozenset[str], bot: Any, *,
        own: bool, memory_ages: dict[int, int | None] | None = None,
    ) -> list[str]:
        blocks = []
        compact: dict[tuple[str, str], list[str]] = defaultdict(list)
        compact_ages: dict[str, list[int | None]] = defaultdict(list)
        gas_tags = set()
        if own:
            gas_tags = {unit.tag for unit in chain(
                getattr(bot, "gas_buildings", ()), getattr(bot, "vespene_geyser", ())
            )}
        for unit in sorted(units, key=lambda unit: (_type_name(unit), unit.tag)):
            name = _type_name(unit)
            alias = self.ids.alias(unit.tag)
            age = memory_ages[unit.tag] if memory_ages is not None else None
            if memory_ages is not None:
                state = self._memory_age([age])
            elif own:
                active = self._active_states.get(alias, []) + self._active_states.get(name, [])
                state = self._core_state(unit, active, gas_tags)
            else:
                state = self._visible_enemy_state(unit)
            if name in compact_types:
                compact[name, "" if memory_ages is not None else state].append(alias)
                if memory_ages is not None:
                    compact_ages[name].append(age)
                continue
            position = getattr(unit, "position", None)
            location = (
                f"({position.x:.1f},{position.y:.1f})"
                if position is not None else "[Unknown]"
            )
            hp = self._health_value(unit) if memory_ages is None or age is not None else "?"
            fields = [f"{name}[{alias}]@{location}", f"state={state}", f"HP={hp}"]
            if own:
                extra_fields = ("shield", "energy", "cargo")
            elif memory_ages is not None:
                extra_fields = ("shield",)
            else:
                extra_fields = ("shield", "energy")
            for field in extra_fields:
                if memory_ages is not None and age is None:
                    continue
                if not getattr(unit, field + "_max", 0) > 0:
                    continue
                if field == "shield":
                    value = self._health_value(unit, field)
                else:
                    attr = "cargo_used" if field == "cargo" else field
                    raw = getattr(unit, attr, None)
                    value = str(int(raw)) if isinstance(raw, (int, float)) else "?"
                fields.append(f"{field}={value}")
            # Enemy ability availability is not part of the visible game state.
            abilities = getattr(unit, "abilities", None) if own else None
            if abilities:
                allowed = self._ability_whitelist.get(name, ())
                names = sorted({getattr(a, "name", str(a)) for a in abilities}.intersection(allowed))
                if names:
                    fields.append(",".join(names))
            blocks.append(" | ".join(fields))
        for (name, state), aliases in sorted(compact.items()):
            if memory_ages is not None:
                state = self._memory_age(compact_ages[name])
            blocks.append(f"{name}[{','.join(aliases)}] | state={state}")
        return blocks

    @staticmethod
    def _visible_enemy_state(unit: Any) -> str:
        progress = float(getattr(unit, "build_progress", 1.0))
        states = [f"building({int(progress * 100)}%)"] if progress < 1.0 else []
        states.extend(
            label for flag, label in (
                ("is_flying", "flying"), ("is_burrowed", "burrowed"), ("is_cloaked", "cloaked"),
            ) if getattr(unit, flag, False)
        )
        buffs = getattr(unit, "buffs", ())
        states.extend(f"buff={name}" for name in sorted(getattr(b, "name", str(b)) for b in buffs))
        return ",".join(states) or "visible"

    @staticmethod
    def _core_state(unit: Any, active: list[str], gas_tags: set[int]) -> str:
        progress = float(getattr(unit, "build_progress", 1.0))
        orders = getattr(unit, "orders", [])
        if active:
            state = ",".join(active)
        elif progress < 1.0:
            state = f"building({int(progress * 100)}%)"
        elif getattr(unit, "is_gathering", False) or getattr(unit, "is_returning", False):
            target = getattr(unit, "order_target", None)
            gas = getattr(unit, "is_carrying_vespene", False) or target in gas_tags
            state = "gathering_gas" if gas else "gathering_minerals"
        else:
            state = next((
                label for flag, label in (
                    ("is_constructing_scv", "constructing"), ("is_repairing", "repairing"),
                    ("is_attacking", "attacking"), ("is_moving", "moving"),
                ) if getattr(unit, flag, False)
            ), None)
            if state is None and orders:
                order = orders[0]
                ability = getattr(order.ability, "exact_id", None) or getattr(order.ability, "id", None)
                name = str(getattr(ability, "name", "order"))
                if name in PRODUCTION_ABILITIES:
                    state = "producing"
                elif name in RESEARCH_ABILITIES:
                    state = "researching"
                else:
                    state = name + (f"({int(order.progress * 100)}%)" if 0 < order.progress < 1 else "")
            if state is None:
                state = "idle" if getattr(unit, "is_idle", False) else "unknown"
        modifiers = [
            label for flag, label in (
                ("is_flying", "flying"), ("has_techlab", "addon=TECHLAB"),
                ("has_reactor", "addon=REACTOR"),
            ) if getattr(unit, flag, False)
        ]
        return ",".join([state, *modifiers])
