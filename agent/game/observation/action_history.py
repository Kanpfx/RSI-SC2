"""Recent submissions and failures with current decision-cycle controls."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent.game.actions.formatting import format_action


@dataclass
class ActionRecord:
    time: str
    key: str
    description: str
    status: str
    reason: str = ""
    cycle: int = 0


class ActionHistory:
    def __init__(self):
        self._recent: list[ActionRecord] = []
        self._active: dict[str, ActionRecord] = {}
        self._cycle = 0

    def begin_decision(self) -> None:
        """Retain the latest two execution cycles."""
        self._cycle += 1
        oldest = self._cycle - 1
        self._recent = [record for record in self._recent if record.cycle >= oldest]

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

    def record(self, action: Any, time: str, status: str, reason: str = "", *,
               submitted_action: str | None = None) -> None:
        if status not in {"submitted", "failed"}:
            raise ValueError("history event must be submitted or failed")
        key = self._key(action)
        description = (
            format_action(action)
            if isinstance(action, dict) and set(action) == {"id", "args"}
            else str(action)
        )
        if submitted_action is not None:
            description = submitted_action
        if status == "failed":
            self._active.pop(key, None)
            for record in reversed(self._recent):
                if record.key == key and record.status == "submitted" and record.cycle == self._cycle:
                    record.status = status
                    record.reason = reason
                    record.time = time
                    record.description = description
                    return
        self._recent.append(ActionRecord(time, key, description, status, reason, self._cycle))

    def annotate(self, action: Any, reason: str) -> None:
        key = self._key(action)
        live = self._active.get(key)
        if live is not None:
            live.reason = reason
            return
        for record in reversed(self._recent):
            if record.key == key and record.status == "submitted":
                record.reason = reason
                break

    def render(self, cycles: int = 2) -> str:
        records = {(record.cycle, record.key): record for record in self._recent
                   if record.cycle >= self._cycle - cycles + 1}
        failures = [record for record in records.values() if record.status == "failed"]
        records.update({(self._cycle, key): record for key, record in self._active.items()})
        if not records:
            return "[None]"
        rows = []
        for status in ("active", "submitted", "failed"):
            group = (failures if status == "failed" else
                     [record for record in records.values() if record.status == status])
            if not group:
                continue
            rows.append(f"{status}:")
            for record in sorted(group, key=lambda record: tuple(int(part) for part in record.time.split(':'))
                                 if record.time != '--:--' else (-1, -1)):
                action = " ".join(record.description.splitlines())
                reason = " ".join(record.reason.splitlines()).strip()
                rows.append(f"- time@{record.time} {action}" + (f": {reason}" if reason else ""))
        return "\n".join(rows)
