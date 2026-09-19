"""Small, heuristic hints that call attention to common operational risks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _type_name(unit: Any) -> str:
    return getattr(getattr(unit, "type_id", None), "name", "UNKNOWN")


@dataclass(frozen=True)
class SituationHint:
    key: str
    category: str
    priority: int
    text: str


class SituationHintBuilder:
    """Merge Ares intelligence and simple live-state checks into short hints."""

    KEY_STRUCTURES = {
        "COMMANDCENTER",
        "ORBITALCOMMAND",
        "PLANETARYFORTRESS",
        "BARRACKS",
        "FACTORY",
        "STARPORT",
        "ENGINEERINGBAY",
        "ARMORY",
        "FUSIONCORE",
        "GHOSTACADEMY",
    }
    RUSH_HINTS = (
        ("get_enemy_worker_rushed", "Possible early worker rush detected."),
        ("get_enemy_marine_rush", "Possible early Marine rush detected."),
        ("get_enemy_marauder_rush", "Possible early Marauder rush detected."),
        ("get_enemy_four_gate", "Possible early Four Gate pressure detected."),
        ("get_is_proxy_zealot", "Possible early proxy Zealot pressure detected."),
        ("get_enemy_ling_rushed", "Possible early Zergling rush detected."),
        ("get_enemy_roach_rushed", "Possible early Roach rush detected."),
        ("get_enemy_ravager_rush", "Possible early Ravager pressure detected."),
    )

    def __init__(self, attacked_ttl_seconds: float = 6.0) -> None:
        self.attacked_ttl_seconds = attacked_ttl_seconds
        self._structure_health: dict[int, float] = {}
        self._attacked_until: dict[int, float] = {}
        self._attacked_names: dict[int, str] = {}

    def collect_frame(self, bot: Any) -> None:
        """Capture damage between model decisions so short events are not missed."""
        now = float(getattr(bot, "time", 0.0))
        current_tags: set[int] = set()
        for structure in getattr(bot, "structures", []):
            tag = getattr(structure, "tag", None)
            if not isinstance(tag, int):
                continue
            current_tags.add(tag)
            health = self._health(structure)
            previous = self._structure_health.get(tag)
            name = _type_name(structure)
            if (
                name in self.KEY_STRUCTURES
                and previous is not None
                and health is not None
                and health < previous - 0.001
            ):
                self._attacked_until[tag] = now + self.attacked_ttl_seconds
                self._attacked_names[tag] = name
            if health is not None:
                self._structure_health[tag] = health
        self._structure_health = {
            tag: health
            for tag, health in self._structure_health.items()
            if tag in current_tags
        }
        self._attacked_until = {
            tag: expiry for tag, expiry in self._attacked_until.items() if expiry >= now
        }

    def build(self, bot: Any) -> dict[str, list[str]]:
        self.collect_frame(bot)
        hints = [
            *self._under_attack_hints(bot),
            *self._base_proximity_hints(bot),
            *self._proxy_hints(bot),
            *self._rush_hints(bot),
            *self._supply_hints(bot),
        ]
        unique: dict[str, SituationHint] = {}
        for hint in hints:
            unique.setdefault(hint.key, hint)
        selected = sorted(unique.values(), key=lambda item: (item.priority, item.key))[:5]
        result: dict[str, list[str]] = {}
        for hint in selected:
            result.setdefault(hint.category, []).append(hint.text)
        return result

    @staticmethod
    def _health(unit: Any) -> float | None:
        health = getattr(unit, "health", None)
        shield = getattr(unit, "shield", 0.0)
        if isinstance(health, (int, float)):
            return float(health) + (float(shield) if isinstance(shield, (int, float)) else 0.0)
        percentage = getattr(unit, "health_percentage", None)
        return float(percentage) if isinstance(percentage, (int, float)) else None

    def _under_attack_hints(self, bot: Any) -> list[SituationHint]:
        now = float(getattr(bot, "time", 0.0))
        structures = {getattr(item, "tag", None): item for item in getattr(bot, "structures", [])}
        hints: list[SituationHint] = []
        for tag, expiry in self._attacked_until.items():
            if expiry < now or tag not in structures:
                continue
            structure = structures[tag]
            name = self._display_name(self._attacked_names.get(tag, _type_name(structure)))
            location = self._base_label(bot, getattr(structure, "position", None))
            hints.append(
                SituationHint(
                    f"under_attack:{tag}",
                    "Combat",
                    10,
                    f"Our {name}{' near ' + location if location else ''} is under attack.",
                )
            )
        return hints

    def _base_proximity_hints(self, bot: Any) -> list[SituationHint]:
        enemies = [
            unit
            for unit in getattr(bot, "enemy_units", [])
            if getattr(unit, "is_visible", True) and not getattr(unit, "is_memory", False)
        ]
        hints: list[SituationHint] = []
        for index, base in enumerate(getattr(bot, "townhalls", [])):
            label = ("our main", "our natural")[index] if index < 2 else f"our base {index + 1}"
            ground = any(
                not getattr(enemy, "is_flying", False)
                and self._distance(enemy, base) <= 11.0
                for enemy in enemies
            )
            air = any(
                getattr(enemy, "is_flying", False)
                and self._distance(enemy, base) <= 14.0
                for enemy in enemies
            )
            if not ground and not air:
                continue
            kind = "ground and air" if ground and air else "air" if air else "ground"
            hints.append(
                SituationHint(
                    f"base_proximity:{getattr(base, 'tag', index)}:{kind}",
                    "Combat",
                    20,
                    f"Enemy {kind} units are close to {label}.",
                )
            )
        return hints

    def _proxy_hints(self, bot: Any) -> list[SituationHint]:
        detector = getattr(bot, "get_enemy_proxies", None)
        if not callable(detector):
            return []
        positions = [
            ("our main", getattr(bot, "start_location", None)),
            ("our natural", self._mediator_value(bot, "get_own_nat")),
        ]
        for label, position in positions:
            if position is None:
                continue
            try:
                proxies = list(detector(30.0, position))
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
            if proxies:
                return [
                    SituationHint(
                        "proxy",
                        "Combat",
                        30,
                        f"Enemy proxy structures detected near {label}.",
                    )
                ]
        return []

    def _rush_hints(self, bot: Any) -> list[SituationHint]:
        for attribute, message in self.RUSH_HINTS:
            if bool(self._mediator_value(bot, attribute)):
                return [SituationHint(f"rush:{attribute}", "Combat", 40, message)]
        return []

    def _supply_hints(self, bot: Any) -> list[SituationHint]:
        used = int(getattr(bot, "supply_used", 0))
        cap = int(getattr(bot, "supply_cap", 0))
        free = cap - used
        if cap <= 0:
            return []
        if free <= 0:
            text = "Supply is blocked."
        elif free <= 2 and not self._depot_nearly_ready(bot):
            text = f"Supply is low: {free} free and no Supply Depot is nearly ready."
        else:
            return []
        return [SituationHint("supply", "Economy and operations", 50, text)]

    @staticmethod
    def _depot_nearly_ready(bot: Any) -> bool:
        return any(
            _type_name(item) == "SUPPLYDEPOT"
            and 0.7 <= float(getattr(item, "build_progress", 1.0)) < 1.0
            for item in getattr(bot, "structures", [])
        )

    @staticmethod
    def _distance(first: Any, second: Any) -> float:
        try:
            return float(first.distance_to(second))
        except (AttributeError, TypeError, ValueError):
            try:
                dx = first.position.x - second.position.x
                dy = first.position.y - second.position.y
                return float((dx * dx + dy * dy) ** 0.5)
            except AttributeError:
                return float("inf")

    def _base_label(self, bot: Any, position: Any) -> str:
        if position is None:
            return ""
        candidates = [
            ("our main", getattr(bot, "start_location", None)),
            ("our natural", self._mediator_value(bot, "get_own_nat")),
        ]
        valid = [(label, point) for label, point in candidates if point is not None]
        if not valid:
            return ""
        label, point = min(valid, key=lambda item: self._point_distance(position, item[1]))
        return label if self._point_distance(position, point) <= 20.0 else ""

    @staticmethod
    def _point_distance(first: Any, second: Any) -> float:
        try:
            return float(((first.x - second.x) ** 2 + (first.y - second.y) ** 2) ** 0.5)
        except AttributeError:
            return float("inf")

    @staticmethod
    def _mediator_value(bot: Any, attribute: str) -> Any:
        try:
            return getattr(bot.mediator, attribute)
        except (AttributeError, KeyError):
            return None

    @staticmethod
    def _display_name(name: str) -> str:
        return {
            "COMMANDCENTER": "Command Center",
            "ORBITALCOMMAND": "Orbital Command",
            "PLANETARYFORTRESS": "Planetary Fortress",
            "ENGINEERINGBAY": "Engineering Bay",
            "FUSIONCORE": "Fusion Core",
            "GHOSTACADEMY": "Ghost Academy",
        }.get(name, name.replace("_", " ").title())
