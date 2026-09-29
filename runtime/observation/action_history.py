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
    cycle: int = 0


class ActionHistory:
    def __init__(self):
        self._recent: list[ActionRecord] = []
        self._active: dict[str, ActionRecord] = {}
        self._queued: dict[str, ActionRecord] = {}
        self._notices: list[ActionRecord] = []
        self._cycle = 0

    def begin_decision(self) -> None:
        """The next observation includes events from the preceding two cycles."""
        self._cycle += 1
        oldest = self._cycle - 2
        self._recent = [record for record in self._recent if record.cycle >= oldest]
        self._notices = [record for record in self._notices if record.cycle >= oldest]

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
        if status not in {"accepted", "queued", "failed", "notice"}:
            raise ValueError("history status must be accepted, queued, failed or notice")
        key = self._key(action)
        description = (
            format_action(action)
            if isinstance(action, dict) and set(action) == {"id", "args"}
            else str(action)
        )
        if submitted_action is not None:
            description = submitted_action
        if status == "notice":
            self._notices.append(ActionRecord(time, key, description, status, reason, self._cycle))
            return
        self._queued.pop(key, None)
        if status == "queued":
            self._recent = [record for record in self._recent if record.key != key]
            self._queued[key] = ActionRecord(time, key, description, status, reason, self._cycle)
            return
        if status == "failed":
            self._active.pop(key, None)
            for record in reversed(self._recent):
                if record.key == key and record.status == "accepted":
                    record.status = status
                    record.reason = reason
                    record.time = time
                    record.description = description
                    record.cycle = self._cycle
                    return
        self._recent.append(ActionRecord(time, key, description, status, reason, self._cycle))

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
        failures = [record for record in records.values() if record.status == "failed"]
        records.update(self._active)
        records.update(self._queued)
        if not records and not self._notices:
            return "[None]"
        rows = []
        for status in ("active", "queued", "accepted", "failed", "notice"):
            group = (self._notices if status == "notice" else failures if status == "failed" else
                     [record for record in records.values() if record.status == status])
            if not group:
                continue
            rows.append(f"{status}:")
            for record in group:
                action = " ".join(record.description.splitlines())
                reason = " ".join(record.reason.splitlines()).strip()
                rows.append(f"- time@{record.time} {action}" + (f": {reason}" if reason else ""))
        return "\n".join(rows)
