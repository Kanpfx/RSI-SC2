"""Reserved dispatch boundary between runtime facts and optional collectors."""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from agent.logger.information.collector import DataCollector


class DataCollectionManager:
    """Default to no collectors and no filesystem side effects.

    on_error will report collector failures to system telemetry. Dispatch
    must isolate failures so research recording cannot interrupt a match.
    """

    def __init__(
        self, collectors: Sequence[DataCollector] = (), *,
        on_error: Callable[..., None] | None = None,
    ):
        self.collectors = tuple(collectors)
        self.on_error = on_error

    def on_observation(
        self, snapshot: Mapping[str, Any], *, refs: Mapping[str, Any]
    ) -> None:
        # TODO: Dispatch detached observation snapshots with failure isolation.
        pass

    def on_action_event(
        self, event: Mapping[str, Any], *, refs: Mapping[str, Any]
    ) -> None:
        # TODO: Dispatch factual action events with failure isolation.
        pass

    def on_match_end(
        self, summary: Mapping[str, Any], *, refs: Mapping[str, Any]
    ) -> None:
        # TODO: Dispatch the final summary and isolate flush failures.
        pass
