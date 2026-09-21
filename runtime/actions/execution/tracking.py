"""Observe Ares execution without replacing its resource or prerequisite logic."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from loguru import logger
from sc2.protocol import ProtocolError


class TrackedBehavior:
    """Report exceptions and execution outcomes; Ares owns readiness checks."""

    def __init__(
        self,
        behavior: Any,
        *,
        on_failure: Callable[[str], None],
        on_result: Callable[[bool], None],
    ):
        self.behavior = behavior
        self.on_failure = on_failure
        self.on_result = on_result

    def execute(self, ai: Any, config: dict, mediator: Any) -> bool:
        try:
            result = self.behavior.execute(ai, config, mediator)
        except ProtocolError:
            raise
        except Exception as exc:
            logger.exception("Ares behavior execution failed")
            self.on_failure(str(exc))
            return False
        self.on_result(result)
        return result
