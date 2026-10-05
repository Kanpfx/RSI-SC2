"""Reserved storage interface for collector-specific research records."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any


class DataWriter:
    """Keep a fixed envelope around an RSI-defined payload.

    Envelope: run_id, collector name/version, record name/schema_version,
    timestamp, and applicable decision_id, iteration, game_loop, game_time.
    Unknown correlation fields stay absent; they must not be inferred.
    """

    def __init__(
        self, run_directory: Path, *, run_id: str,
        collector_name: str, collector_version: str,
    ):
        self.run_directory = run_directory
        self.run_id = run_id
        self.collector_name = collector_name
        self.collector_version = collector_version

    def record(
        self, name: str, *, schema_version: int,
        payload: Mapping[str, Any], refs: Mapping[str, Any],
    ) -> None:
        # TODO: Validate the collector name and correlation fields.
        # TODO: Build the envelope without allowing payload/refs to override it.
        # TODO: Lazily write data/<collector_name>/records.jsonl in UTF-8.
        pass
