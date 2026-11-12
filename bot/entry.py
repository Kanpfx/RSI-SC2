"""Game lifecycle; tactics plan and execute, core arbitrates, telemetry observes."""

from time import perf_counter
import traceback
import warnings

from sc2.bot_ai import BotAI

from .core.frame import Frame
from .components.tactics.bc_rush import BcRush
from .telemetry.logger import Logger

GAME_STEP = 2


class ResearchBot(BotAI):
    """Connect native callbacks, tactic execution and passive telemetry."""

    def __init__(self, *, logger=None, sc2_build=None):
        super().__init__()
        self.unit_command_uses_self_do = True
        self.logger = logger if logger is not None else Logger()
        self.sc2_build = sc2_build
        self.opponent_id = None
        self.tactics = {"bc_rush": BcRush()}
        self.frame = None
        self.runtime_error = None
        self.final_result = None
        self.steps = 0
        self.total_step_seconds = self.max_step_seconds = 0

    async def on_start(self):
        """Set the observation interval, record engine metadata and initialize shared services."""
        try:
            self.client.game_step = GAME_STEP
            ping = (await self.client.ping()).ping
            self.logger.emit("game_started", 0, game_version=ping.game_version,
                             sc2_build=ping.base_build, game_step=GAME_STEP,
                             opponent_id=self.opponent_id)
            if self.sc2_build is not None and ping.base_build != self.sc2_build:
                warnings.warn(f"Configured SC2 build {self.sc2_build}, running {ping.base_build}",
                              RuntimeWarning, stacklevel=2)
            self.frame = Frame(self)
        except Exception:
            self.runtime_error = traceback.format_exc()
            self.logger.emit("bot_runtime_error", 0, exception=self.runtime_error)
            raise

    async def on_step(self, iteration):
        """Observe, collect claims, allocate units, run tactics, then resolve commands."""
        start = perf_counter()
        self.logger.observe(self)
        try:
            self.frame.begin()
            for key, tactic in self.tactics.items():
                self.frame.request_units(tactic, key=key)
            self.frame.units.resolve()
            for key, tactic in self.tactics.items():
                await self.frame.execute(tactic, key=key)
            await self.frame.resolve()
        except Exception:
            self.runtime_error = traceback.format_exc()
            self.logger.emit("bot_runtime_error", self.state.game_loop, exception=self.runtime_error)
            raise
        finally:
            # Detach frame records even if execution failed or logging has been disabled.
            records = self.frame.end()
            self.logger.consume(self.state.game_loop, records)
            elapsed = perf_counter() - start
            self.steps += 1
            self.total_step_seconds += elapsed
            self.max_step_seconds = max(self.max_step_seconds, elapsed)

    async def on_unit_created(self, unit):
        self.logger.emit("unit_created", self.state.game_loop, tag=unit.tag, type=unit.type_id.name)

    async def on_unit_destroyed(self, unit_tag):
        self.logger.emit("unit_destroyed", self.state.game_loop, tag=unit_tag)

    async def on_building_construction_started(self, unit):
        self.logger.emit("construction_started", self.state.game_loop, tag=unit.tag, type=unit.type_id.name)

    async def on_building_construction_complete(self, unit):
        self.logger.emit("construction_complete", self.state.game_loop, tag=unit.tag, type=unit.type_id.name)

    async def on_end(self, result):
        self.final_result = str(result)
        frame = self.state.game_loop if self.frame is not None else 0
        self.logger.emit("game_ended", frame, result=self.final_result)

    def summary(self):
        """Return match outcome, runtime errors, tactic phase and step timings."""
        return dict(result=self.final_result, opponent_id=self.opponent_id,
            bot_runtime_error=self.runtime_error, steps=self.steps,
            phase=self.tactics["bc_rush"].phase,
            mean_step_ms=1000*self.total_step_seconds/max(1, self.steps),
            max_step_ms=1000*self.max_step_seconds)
