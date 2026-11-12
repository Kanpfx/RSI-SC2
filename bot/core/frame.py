"""Explicit planning and execution phases with small, sequential call scopes."""

from dataclasses import dataclass
import traceback

from .actions import Actions
from .units import UnitPool
from ..spatial import Spatial


@dataclass(frozen=True, slots=True)
class Call:
    """Current tactic/behavior scope used for ownership, priority and trace records."""

    id: str = ""
    key: str = ""
    group: str = ""
    priority: int = 0


class Frame:
    """Coordinate planning, unit allocation, behavior calls and action resolution."""

    def __init__(self, game):
        self.game = game
        self.spatial = Spatial(game)
        self.units = UnitPool(game)
        self.actions = Actions(self)
        self.records = []
        self.current = Call()
        self._tactic = ""
        self._sequence = 0
        self._active = False
        self._resolved = False

    def begin(self):
        """Refresh units and reconcile pending commands before collecting new claims."""
        if self._active:
            raise RuntimeError("End the previous frame before beginning another")
        self._active, self._resolved = True, False
        self.units.begin()
        self.actions.begin()

    def _qualified_key(self, name):
        if not name or any(part in {"", ".", ".."} for part in name.split("/")):
            raise ValueError("Use a nonempty local group/key")
        return f"{self._tactic}/{name}" if self._tactic else name

    @property
    def group(self):
        return self.current.group

    def claim(self, group, **demand):
        """Declare a demand under the current tactic's group namespace."""
        self.units.request(self._qualified_key(group), **demand)

    def tags(self, group):
        """Read the current tactic's allocated group tags."""
        return self.units.tags(self._qualified_key(group))

    def request_units(self, tactic, *, key):
        """Collect a tactic's demands under its stable key before allocation."""
        if not self._active or self.units.resolved:
            raise RuntimeError("Declare tactic demands during the planning phase")
        previous, self._tactic = self._tactic, key
        try:
            tactic.request_units(self)
        finally:
            self._tactic = previous

    async def execute(self, tactic, *, key):
        """Execute a tactic after all unit demands have been allocated."""
        if not self._active or self._resolved or not self.units.resolved:
            raise RuntimeError("Allocate all unit claims before executing tactics")
        previous, self._tactic = self._tactic, key
        try:
            await self._invoke(tactic.execute, key, "", 0, tactic=True)
        finally:
            self._tactic = previous

    async def run(self, behavior, *, key, group, priority, **inputs):
        """Run a behavior with a scoped key, allocated group and action priority."""
        if not self._active or self._resolved or not self.units.resolved:
            raise RuntimeError("Allocate unit claims before running behaviors")
        return await self._invoke(behavior, self._qualified_key(key), self._qualified_key(group),
                                  priority, **inputs)

    async def _invoke(self, operation, key, group, priority, *, tactic=False, **inputs):
        """Record a call and restore its parent scope even when execution raises."""
        parent = self.current
        self._sequence += 1
        self.current = Call(f"call{self._sequence}", key, group, priority)
        record = dict(frame=self.game.state.game_loop, call_id=self.current.id,
            parent_id=parent.id,
            module_id=operation.__qualname__.split(".")[-2] if tactic else operation.__name__,
            module_path=operation.__module__, path=key, priority=priority)
        try:
            result = await operation(self, **inputs)
            if tactic:
                if result is not None:
                    raise TypeError("Tactic.execute must return None")
                record["status"] = "invoked"
            else:
                if type(result) is not bool:
                    raise TypeError("Behavior must return bool")
                record["status"] = "invoked_true" if result else "invoked_false"
            return result
        except Exception:
            record.update(status="exception", exception=traceback.format_exc())
            raise
        finally:
            self.records.append(record)
            self.current = parent

    async def resolve(self):
        """Arbitrate proposed commands once, after tactic execution."""
        if not self._active or self._resolved or not self.units.resolved:
            raise RuntimeError("Unit allocation is required before action resolution")
        self._resolved = True
        await self.actions.resolve()

    def end(self):
        """Detach telemetry independently of the consumer; retain gameplay state."""
        calls, self.records = self.records, []
        self._active = False
        return dict(calls=calls, actions=self.actions.drain(), units=self.units.drain(),
                    active_requests=tuple(self.actions.pending))
