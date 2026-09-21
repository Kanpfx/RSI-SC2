"""Extension point for RSI-defined observations, events and metrics."""

from collections.abc import Mapping
from typing import Any

from agent.logger.information.writer import DataWriter


class DataCollector:
    """Consume detached snapshots and facts, never the live game bot.

    Each hook receives correlation refs separately from its payload. Inputs
    must not be modified. Subclasses choose which records to emit.
    """

    name = "rsi"
    version = "1"

    def __init__(self, writer: DataWriter):
        self.writer = writer

    def on_observation(
        self, snapshot: Mapping[str, Any], *, refs: Mapping[str, Any]
    ) -> None:
        # TODO: Select useful game-state fields and emit research records.
        pass

    def on_action_event(
        self, event: Mapping[str, Any], *, refs: Mapping[str, Any]
    ) -> None:
        # TODO: Extract execution events or patterns useful to RSI.
        pass

    def on_match_end(
        self, summary: Mapping[str, Any], *, refs: Mapping[str, Any]
    ) -> None:
        # TODO: Aggregate match metrics and flush collector-local state.
        pass
