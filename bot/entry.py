"""The sole game loop: observe, run strategy, resolve actions, consume telemetry."""
from time import perf_counter
import traceback

from sc2.bot_ai import BotAI

from .core.actions import Actions
from .core.frame import Frame
from .core.units import UnitControl
from .spatial import Spatial
from .components.tactics.bc_rush import BcRush
from .telemetry.logger import Logger


GAME_STEP = 2


class ResearchBot(BotAI):
    def __init__(self, *, logger=None, sc2_build=98370):
        """Configure the expected game build, passive logger and persistent root Tactics."""
        super().__init__()
        self.unit_command_uses_self_do = True
        self.logger = logger if logger is not None else Logger()
        self.sc2_build = sc2_build
        self.tactics = BcRush()
        self.frame = None
        self.runtime_error = None
        self.final_result = None
        self.steps = 0
        self.total_step_seconds = 0
        self.max_step_seconds = 0

    async def on_start(self):
        """Verify the native build, set game_step and construct shared frame services."""
        self.client.game_step = GAME_STEP
        ping = (await self.client.ping()).ping
        self.logger.emit("game_started", 0, game_version=ping.game_version,
                         sc2_build=ping.base_build, game_step=GAME_STEP)
        if ping.base_build != self.sc2_build:
            raise RuntimeError(f"Expected SC2 build {self.sc2_build}, got {ping.base_build}")
        units, spatial = UnitControl(), Spatial(self)
        actions = Actions(self, units, spatial.placement)
        self.frame = Frame(self, spatial, units, actions)

    async def on_step(self, iteration):
        """Observe prior actions, run the root Tactics, arbitrate requests and flush telemetry.

        Record and rethrow execution failures; telemetry consumption and step timing
        run even when a module or action raises.
        """
        start = perf_counter()
        try:
            self.logger.observe(self)
            self.frame.actions.begin()
            self.frame.units.update(self)
            await self.frame.run(self.tactics)
            await self.frame.actions.resolve()
        except Exception:
            self.runtime_error = traceback.format_exc()
            self.logger.emit("bot_runtime_error", self.state.game_loop, exception=self.runtime_error)
            raise
        finally:
            self.logger.consume(self.frame)
            elapsed = perf_counter()-start
            self.steps += 1
            self.total_step_seconds += elapsed
            self.max_step_seconds = max(self.max_step_seconds, elapsed)

    async def on_unit_created(self, unit):
        """Record a native unit-creation event without changing strategic ownership."""
        self.logger.emit("unit_created", self.state.game_loop, tag=unit.tag, type=unit.type_id.name)

    async def on_unit_destroyed(self, unit_tag):
        """Record destruction; normal frame maintenance cleans up ownership and requests."""
        self.logger.emit("unit_destroyed", self.state.game_loop, tag=unit_tag)

    async def on_building_construction_started(self, unit):
        """Record native construction start independently of request confirmation."""
        self.logger.emit("construction_started", self.state.game_loop, tag=unit.tag, type=unit.type_id.name)

    async def on_building_construction_complete(self, unit):
        """Record native completion; Actions releases its reservations during observation."""
        self.logger.emit("construction_complete", self.state.game_loop, tag=unit.tag, type=unit.type_id.name)

    async def on_end(self, result):
        """Retain and record the final native game result."""
        self.final_result = str(result)
        self.logger.emit("game_ended", self.state.game_loop, result=self.final_result)

    def summary(self):
        """Return the final result, runtime failure and aggregate step timing for the runner."""
        return dict(result=self.final_result, bot_runtime_error=self.runtime_error, steps=self.steps,
                    phase=self.tactics.phase, mean_step_ms=1000*self.total_step_seconds/max(1, self.steps),
                    max_step_ms=1000*self.max_step_seconds)
