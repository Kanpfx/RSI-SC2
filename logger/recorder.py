"""Per-match JSONL trace writer that deliberately never receives credentials."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.logger.system.writer import SystemWriter
from agent.logger.system.records import RequestTrace, decision_fields
from agent.logger.system.manifest import runtime_manifest, snapshot_sources


class Telemetry:
    """Keep observation, conversations and accepted decisions separate.

    A single instance belongs to one match.  The directory name is timestamped
    so later matches can never overwrite an earlier trace.
    """

    def __init__(
        self,
        metadata: dict[str, Any] | None = None,
        root: Path = Path("logs/agent"),
        directory: Path | None = None,
    ):
        if directory is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            self.directory = root / timestamp
            self.directory.mkdir(parents=True, exist_ok=False)
        else:
            self.directory = directory
            self.directory.mkdir(parents=True, exist_ok=True)
        self.run_id = self.directory.name
        self.system_directory = self.directory / "system"
        self._writer = SystemWriter(self.system_directory)
        self._decisions: dict[int, dict[str, Any]] = {}
        self._metadata = {
            "log_schema_version": 3, "run_id": self.run_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            **runtime_manifest(), **(metadata or {}),
        }
        self._write_json("metadata.json", self._metadata)
        for filename in (
            "obs.jsonl",
            "model.jsonl",
            "accepted_actions.jsonl",
            "events.jsonl",
        ):
            (self.system_directory / filename).touch()

    def snapshot_context(self, sources: dict[str, str], settings: dict[str, Any]) -> None:
        """Archive the exact editable sources loaded for this match."""
        context = snapshot_sources(self.directory / "context", sources)
        self.update_metadata(context_snapshot=context)
        self._write_json("settings.json", settings)

    def request_trace(self, iteration: int, phase: str) -> RequestTrace:
        return RequestTrace(self, iteration, phase)

    def working_memory(self, content: str, **fields: Any) -> None:
        self.event("working_memory_updated", content=content, **fields)

    def event(self, name: str, **fields: Any) -> None:
        self._append("events.jsonl", {"event": name, **fields})

    def observation(self, **fields: Any) -> None:
        iteration = fields.get("iteration")
        if iteration is not None:
            self._decisions[iteration] = {
                **decision_fields(iteration),
                "observation_iteration": iteration,
                "observation_game_loop": fields.get("game_loop"),
                "observation_game_time": fields.get("game_time"),
            }
        self._append("obs.jsonl", fields)

    def model_conversation(self, **fields: Any) -> None:
        self._append("model.jsonl", fields)

    def accepted_decision(self, **fields: Any) -> None:
        self._append("accepted_actions.jsonl", fields)

    def update_metadata(self, **fields: Any) -> None:
        """Merge match-final fields without changing any JSONL trace."""
        self._metadata.update(fields)
        self._write_json("metadata.json", self._metadata)

    def _append(self, filename: str, fields: dict[str, Any]) -> None:
        request_iteration = fields.get("request_iteration", fields.get("iteration"))
        refs = decision_fields(request_iteration)
        refs.update(self._decisions.get(request_iteration, {}))
        self._writer.append(filename, {**refs, **fields, "run_id": self.run_id})

    def _write_json(self, filename: str, fields: dict[str, Any]) -> None:
        self._writer.write_json(filename, fields)
