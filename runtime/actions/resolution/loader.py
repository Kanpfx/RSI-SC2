"""Load the agent's local JSON action catalogs."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from agent.runtime.actions.errors import ActionNameError
from agent.runtime.actions.resolution.types import TypeResolver, symbol
from agent.runtime.actions.resolution.resolver import RUNTIME_SOURCES


def catalog_root() -> Path:
    configured = os.getenv("AGENT_KNOWLEDGE_ROOT")
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[1] / "catalog"


def resolved_catalog_root() -> Path:
    root = catalog_root()
    return root / "actions" if (root / "actions").is_dir() else root


@lru_cache(maxsize=None)
def load_type_definitions() -> dict[str, Any]:
    payload = json.loads((resolved_catalog_root() / "shared_types.json").read_text(encoding="utf-8"))
    definitions = {}
    for group in payload["types"].values():
        for name, definition in group.items():
            if name in definitions:
                raise ValueError(f"Duplicate type: {name}")
            definitions[name] = definition
    return definitions


class ActionCatalog:
    def __init__(self, entries: dict[str, dict[str, Any]]):
        self.entries = entries
        self.types = load_type_definitions()
        self.type_resolver = TypeResolver(self.types)
        for entry in entries.values():
            self._validate_entry(entry)
        self._names: dict[str, dict[str, Any]] = {}
        for entry in entries.values():
            for alias in (entry.get("id"), entry.get("name")):
                if not isinstance(alias, str):
                    continue
                normalized = symbol(alias)
                existing = self._names.get(normalized)
                if existing is not None and existing is not entry:
                    raise ValueError(
                        f"catalog alias {alias!r} conflicts with {existing['id']!r}"
                    )
                self._names[normalized] = entry

    @classmethod
    def load(cls) -> "ActionCatalog":
        root = resolved_catalog_root()
        entries: dict[str, dict[str, Any]] = {}
        for name in (
            "individual_combat_actions.json",
            "group_combat_actions.json",
            "macro_actions.json",
        ):
            payload = json.loads((root / name).read_text(encoding="utf-8"))
            for entry in payload["entries"]:
                if entry["id"] in entries:
                    raise ValueError(f"Duplicate action ID: {entry['id']}")
                entries[entry["id"]] = entry
        return cls(entries)

    def _validate_entry(self, entry: dict[str, Any]) -> None:
        tags = entry.get("tags")
        if (
            not isinstance(tags, list)
            or not tags
            or not all(isinstance(tag, str) and tag for tag in tags)
        ):
            raise ValueError(f"{entry.get('id')} must define non-empty tags")

        availability = entry.get("availability")
        if not isinstance(availability, dict) or not {"param", "types"} <= set(availability) or set(availability) - {"param", "types", "conditions"}:
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

        if entry.get("llm_exposure") not in {"eligible", "disabled"}:
            raise ValueError(f"{entry['id']} has invalid llm_exposure")
        if not entry["api"]["import"].startswith("ares.behaviors."):
            raise ValueError(f"{entry['id']} must reference an Ares behavior")
        names = set()
        for param in entry["params"]:
            name = symbol(param["name"])
            if name in names:
                raise ValueError(f"{entry['id']} has duplicate parameter {name}")
            names.add(name)
            self.type_resolver.validate(param["type"])
            if type(param.get("required")) is not bool or param.get("input") not in {"model", "runtime"}:
                raise ValueError(f"{entry['id']}.{name} has invalid input/required")
            if param["required"] and ("default" in param or "default_python" in param):
                raise ValueError(f"{entry['id']}.{name}: required parameter has a default")
            if not param["required"] and not ({"default", "default_python"} & param.keys()):
                raise ValueError(f"{entry['id']}.{name}: optional parameter needs a default")
            if param["input"] == "runtime":
                if ("source" in param) == ("value" in param):
                    raise ValueError(f"{entry['id']}.{name}: specify exactly one runtime source/value")
                if "source" in param and param["source"] not in RUNTIME_SOURCES:
                    raise ValueError(f"{entry['id']}.{name}: unknown runtime source")
            elif {"source", "value"} & param.keys():
                raise ValueError(f"{entry['id']}.{name}: model parameter has runtime data")

    def get(self, action_id: str) -> dict[str, Any]:
        if not isinstance(action_id, str):
            raise ActionNameError.unknown(action_id)
        if action_id in self.entries:
            return self.entries[action_id]
        try:
            return self._names[symbol(action_id)]
        except KeyError as exc:
            raise ActionNameError.unknown(action_id) from exc

    @staticmethod
    def model_params(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """All model inputs; required only controls whether omission is valid."""
        return {
            param["name"]: param
            for param in entry["params"]
            if param["input"] == "model"
        }

    def prompt_entries(self, allowed: set[str]) -> list[dict[str, Any]]:
        return [
            entry
            for action_id in sorted(allowed)
            if (entry := self.entries.get(action_id)) is not None
            and entry.get("llm_exposure") == "eligible"
        ]

