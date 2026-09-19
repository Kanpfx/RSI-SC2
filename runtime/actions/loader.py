"""Load the agent's local JSON action catalogs."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from agent.runtime.actions.errors import ActionNameError


def normalize_catalog_name(value: str) -> str:
    """Apply small, deterministic case/separator tolerance to catalog names."""
    return "".join(
        character for character in value.strip().casefold() if character.isalnum()
    )


def catalog_root() -> Path:
    configured = os.getenv("AGENT_KNOWLEDGE_ROOT")
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parent / "catalog"


def load_type_definitions() -> dict[str, Any]:
    return json.loads((catalog_root() / "shared_types.json").read_text(encoding="utf-8"))


def model_type_names(expression: str) -> set[str]:
    return set(re.findall(r"[A-Za-z]+", expression))


class ActionCatalog:
    def __init__(self, entries: dict[str, dict[str, Any]]):
        self.entries = entries
        self._names: dict[str, dict[str, Any]] = {}
        for entry in entries.values():
            for alias in (entry.get("id"), entry.get("name")):
                if not isinstance(alias, str):
                    continue
                normalized = normalize_catalog_name(alias)
                existing = self._names.get(normalized)
                if existing is not None and existing is not entry:
                    raise ValueError(
                        f"catalog alias {alias!r} conflicts with {existing['id']!r}"
                    )
                self._names[normalized] = entry

    @classmethod
    def load(cls) -> "ActionCatalog":
        root = catalog_root()
        # Preserve the existing AGENT_KNOWLEDGE_ROOT directory layout.
        if (root / "actions").is_dir():
            root = root / "actions"
        entries: dict[str, dict[str, Any]] = {}
        for name in (
            "Individual Combat Behaviors.json",
            "Group Combat Behaviors.json",
            "Macro Behaviors.json",
        ):
            payload = json.loads((root / name).read_text(encoding="utf-8"))
            for entry in payload["entries"]:
                cls._validate_entry(entry)
                entries[entry["id"]] = entry
        return cls(entries)

    @staticmethod
    def _validate_entry(entry: dict[str, Any]) -> None:
        tags = entry.get("tags")
        if (
            not isinstance(tags, list)
            or not tags
            or not all(isinstance(tag, str) and tag for tag in tags)
        ):
            raise ValueError(f"{entry.get('id')} must define non-empty tags")

        availability = entry.get("availability")
        if not isinstance(availability, dict) or set(availability) != {
            "param",
            "types",
        }:
            raise ValueError(f"{entry.get('id')} has invalid availability")
        actor_types = availability["types"]
        if not isinstance(actor_types, list) or not actor_types:
            raise ValueError(f"{entry.get('id')} availability.types must be non-empty")
        if "ALL" in actor_types and actor_types != ["ALL"]:
            raise ValueError(f"{entry.get('id')} availability ALL must be used alone")

        actor_param = availability["param"]
        if actor_param is not None and actor_param not in {
            param["name"] for param in entry["params"]
        }:
            raise ValueError(
                f"{entry.get('id')} availability.param references "
                "an unknown parameter"
            )

    def get(self, action_id: str) -> dict[str, Any]:
        if action_id in self.entries:
            return self.entries[action_id]
        if not isinstance(action_id, str):
            raise ActionNameError.unknown(action_id)
        try:
            return self._names[normalize_catalog_name(action_id)]
        except KeyError as exc:
            raise ActionNameError.unknown(action_id) from exc

    @staticmethod
    def required_model_params(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Return the deliberately small argument surface exposed to model."""
        return {
            param["name"]: param
            for param in entry["params"]
            if param["input"] == "model"
            and param["required"]
        }

    def prompt_entries(self, allowed: set[str]) -> list[dict[str, Any]]:
        return [
            self.entries[action_id]
            for action_id in sorted(allowed)
            if action_id in self.entries
            and self.entries[action_id].get("llm_exposure") == "eligible"
        ]

