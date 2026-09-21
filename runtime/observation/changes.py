"""Observation facts and changes since the previous model observation."""

from __future__ import annotations

from typing import Any

from agent.runtime.observation.entities import count_entities, entity_display_name
from agent.runtime.observation.execution_context import safe_mediator


class ChangeTracker:
    def __init__(self):
        self.previous_facts: dict[str, int | str] | None = None

    def facts(
        self,
        own_counts: dict[str, int],
        structures: list[Any],
        enemies: list[Any],
        enemy_structures: list[Any],
        bot: Any,
    ) -> dict[str, int | str]:
        facts: dict[str, int | str] = {}
        facts.update({f"own:{name}": count for name, count in own_counts.items()})
        structure_counts = count_entities(structures)
        ready_structure_counts = count_entities(
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
            {f"enemy:{name}": count for name, count in count_entities(enemies).items()}
        )
        facts.update(
            {
                f"enemy_structure:{name}": count
                for name, count in count_entities(enemy_structures).items()
            }
        )
        facts["threat"] = (
            "present"
            if safe_mediator(bot, "get_ground_enemy_near_bases")
            else "none"
        )
        return facts


    def recent_changes(self, facts: dict[str, int | str]) -> list[str]:
        if self.previous_facts is None:
            return []
        changes: list[str] = []
        for key in sorted(set(facts) | set(self.previous_facts)):
            before = self.previous_facts.get(key, 0)
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
                    f"Enemy {entity_display_name(key.split(':', 1)[1])} first seen."
                )
            elif key.startswith("own:") and isinstance(after, int) and after > before:
                changes.append(
                    f"Our {entity_display_name(key.split(':', 1)[1])} count increased to {after}."
                )
            elif (
                key.startswith("ready_structure:")
                and isinstance(after, int)
                and after > before
            ):
                changes.append(
                    f"Our {entity_display_name(key.split(':', 1)[1])} became ready."
                )
            elif (
                key.startswith("structure:")
                and isinstance(after, int)
                and after > before
            ):
                name = key.split(":", 1)[1]
                ready_key = f"ready_structure:{name}"
                ready_increased = facts.get(ready_key, 0) > self.previous_facts.get(
                    ready_key, 0
                )
                if not ready_increased:
                    changes.append(f"Our {entity_display_name(name)} started building.")
            if len(changes) == 5:
                break
        return changes

