"""Action history including resource waits with a separate, unbounded set of live intents."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent.runtime.actions.formatting import format_action


@dataclass
class ActionRecord:
    time: str
    key: str
    description: str
    status: str
    reason: str = ""


class ActionHistory:
    def __init__(self):
        self._recent: list[ActionRecord] = []
        self._active: dict[str, ActionRecord] = {}
        self._queued: dict[str, ActionRecord] = {}

    @staticmethod
    def _key(action: Any) -> str:
        return json.dumps(action, sort_keys=True, separators=(",", ":"))

    def sync_active(self, actions: list[dict[str, Any]], time: str) -> None:
        active = {}
        for action in actions:
            key = self._key(action)
            active[key] = self._active.get(key) or ActionRecord(
                time, key, format_action(action), "active"
            )
        self._active = active

    def record(self, action: Any, time: str, status: str, reason: str = "") -> None:
        if status not in {"accepted", "queued", "failed"}:
            raise ValueError("history status must be accepted, queued or failed")
        key = self._key(action)
        description = (
            format_action(action)
            if isinstance(action, dict) and set(action) == {"id", "args"}
            else str(action)
        )
        self._queued.pop(key, None)
        if status == "queued":
            self._recent = [record for record in self._recent if record.key != key]
            self._queued[key] = ActionRecord(time, key, description, status, reason)
            return
        if status == "failed":
            self._active.pop(key, None)
            for record in reversed(self._recent):
                if record.key == key and record.status == "accepted":
                    record.status = status
                    record.reason = reason
                    return
        self._recent.append(ActionRecord(time, key, description, status, reason))
        self._recent = self._recent[-10:]

    def forget_queued(self, action: Any) -> None:
        self._queued.pop(self._key(action), None)

    def annotate(self, action: Any, reason: str) -> None:
        key = self._key(action)
        for record in reversed(self._recent):
            if record.key == key and record.status == "accepted":
                record.reason = reason
                break

    def render(self) -> str:
        # Show the latest state once; live intents take precedence over history.
        records = {record.key: record for record in self._recent}
        records.update(self._active)
        records.update(self._queued)
        if not records:
            return "[None]"
        rows = []
        for status in ("active", "queued", "accepted", "failed"):
            group = [record for record in records.values() if record.status == status]
            if not group:
                continue
            rows.append(f"{status}:")
            for record in group:
                action = " ".join(record.description.splitlines())
                reason = " ".join(record.reason.splitlines()).strip()
                rows.append(f"- time@{record.time} {action}" + (f": {reason}" if reason else ""))
        return "\n".join(rows)
