"""Load the clean Ares action tables without expanding their schema."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agent.paths import ACTIONS_ROOT
from agent.game.actions.errors import ActionNameError
from agent.game.actions.resolution.types import TypeResolver, symbol
from agent.game.actions.resolution.resolver import RUNTIME_SOURCES


def catalog_root() -> Path:
    configured = os.getenv("AGENT_KNOWLEDGE_ROOT")
    return Path(configured).resolve() if configured else ACTIONS_ROOT


def resolved_catalog_root() -> Path:
    root = catalog_root()
    return root / "actions" if (root / "actions").is_dir() else root


class ActionCatalog:
    def __init__(self, entries: dict[str, dict[str, Any]]):
        self.entries = entries
        self.type_resolver = TypeResolver()
        self._names = {}
        for entry in entries.values():
            self._validate_entry(entry)
            key = symbol(entry["name"])
            if key in self._names:
                raise ValueError(f"Duplicate action name: {entry['name']}")
            self._names[key] = entry

    @classmethod
    def load(cls) -> "ActionCatalog":
        entries = {}
        for name in ("individual_actions.json", "group_actions.json", "macro_actions.json"):
            payload = json.loads((resolved_catalog_root() / name).read_text(encoding="utf-8"))
            for entry in payload["actions"]:
                if entry["name"] in entries:
                    raise ValueError(f"Duplicate action name: {entry['name']}")
                entries[entry["name"]] = entry
        return cls(entries)

    def _validate_entry(self, entry):
        if type(entry.get("enabled")) is not bool:
            raise ValueError(f"{entry['name']}: enabled must be boolean")
        if not entry["import"].startswith("ares.behaviors."):
            raise ValueError(f"{entry['name']}: invalid behavior import")
        names = set()
        for param in entry["params"]:
            key = symbol(param["name"])
            if key in names:
                raise ValueError(f"{entry['name']}: duplicate parameter {param['name']}")
            names.add(key)
            self.type_resolver.validate(param["type"])
            if type(param.get("required")) is not bool or param.get("source") not in {"model", "fixed", "runtime"}:
                raise ValueError(f"{entry['name']}.{param['name']}: invalid parameter source")
            if "value" not in param:
                raise ValueError(f"{entry['name']}.{param['name']}: missing value")
            if param["source"] == "runtime" and param["value"] not in RUNTIME_SOURCES:
                raise ValueError(f"{entry['name']}.{param['name']}: unknown runtime source")

    def get(self, action_id):
        if isinstance(action_id, str):
            entry = self._names.get(symbol(action_id))
            if entry is not None:
                return entry
        raise ActionNameError.unknown(action_id)

    @staticmethod
    def model_params(entry):
        return {p["name"]: p for p in entry["params"] if p["source"] == "model"}

    def prompt_entries(self, allowed):
        return [entry for name, entry in self.entries.items() if name in allowed and entry["enabled"]]
