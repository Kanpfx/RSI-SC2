"""Ares bot entry point for the LLM-controlled game runtime."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ares import AresBot
from loguru import logger
from sc2.data import Result, Status
from sc2.protocol import ProtocolError

from agent.runtime.controller import LLMGameController


class WhyBot(AresBot):
    def __init__(
        self,
        game_step_override: int | None = None,
        *,
        tactic_name: str = "BattleCruiserRush",
        run_metadata: dict[str, str | bool] | None = None,
        log_directory: Path | None = None,
    ) -> None:
        super().__init__(game_step_override)
        self._last_iteration: int = -1
        self.tactic_name = tactic_name
        self.run_metadata = run_metadata or {}
        self.log_directory = log_directory
        self.llm_controller: LLMGameController | None = None

    async def on_start(self) -> None:
        await super().on_start()
        self.llm_controller = LLMGameController(
            tactic_name=self.tactic_name,
            run_metadata={**self.run_metadata, "sc2_base_build": getattr(self, "base_build", None)},
            log_directory=self.log_directory,
        )
        if not self.llm_controller.active:
            raise RuntimeError("LLM_MODEL, LLM_BASE_URL and LLM_API_KEY are required")
        # Disable scripted openings while retaining Ares manager updates.
        self.build_order_runner.set_build_completed()
        logger.info(
            "Single-model mode started (tactic: {}, logs: {})",
            self.tactic_name,
            self.llm_controller.telemetry.directory,
        )

    async def on_step(self, iteration: int) -> None:
        try:
            await super().on_step(iteration)
            self._last_iteration = iteration
            if self.supply_used < 1 and not self.realtime:
                await self.client.leave()
                return

            if self.llm_controller is None:
                raise RuntimeError("model controller was not initialized")
            await self.llm_controller.run_iteration(self, iteration)
        except ProtocolError as exc:
            if not self.realtime or not exc.is_game_over_error:
                raise
            await self.client.observation()

    async def _after_step(self) -> int:
        if self.realtime and (
            self.client._status == Status.ended or self.client._game_result
        ):
            if not self.client._game_result:
                await self.client.observation()
            return 0
        try:
            return await super()._after_step()
        except ProtocolError as exc:
            if not self.realtime or not exc.is_game_over_error:
                raise
            # Fetch SC2's result; python-sc2 then calls on_end and saves the replay.
            await self.client.observation()
            return 0

    async def on_end(self, game_result: Result) -> None:
        if self.llm_controller is not None:
            await self.llm_controller.close()
            try:
                self.llm_controller.telemetry.update_metadata(
                    **self._result_metadata(game_result),
                    completed_at=datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds"),
                )
            except Exception as exc:
                logger.warning("Failed to write match result metadata: {}", exc)
        await super().on_end(game_result)

    def _result_metadata(self, game_result: Result) -> dict[str, Any]:
        score = getattr(getattr(self, "state", None), "score", None)
        result_name = getattr(game_result, "name", str(game_result))
        return {
            "result": result_name,
            "final_iteration": self._last_iteration,
            "game_time_seconds": round(float(getattr(self, "time", 0.0)), 1),
            "final_resources": {
                "minerals": int(getattr(self, "minerals", 0)),
                "vespene": int(getattr(self, "vespene", 0)),
                "supply_used": int(getattr(self, "supply_used", 0)),
                "supply_cap": int(getattr(self, "supply_cap", 0)),
                "workers": int(getattr(self, "supply_workers", 0)),
                "army_supply": int(getattr(self, "supply_army", 0)),
            },
            "score": {
                "collected_minerals": getattr(score, "collected_minerals", 0),
                "collected_vespene": getattr(score, "collected_vespene", 0),
                "killed_value_units": getattr(score, "killed_value_units", 0),
                "killed_value_structures": getattr(score, "killed_value_structures", 0),
                "idle_worker_time": getattr(score, "idle_worker_time", 0),
            },
        }

