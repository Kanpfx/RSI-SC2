"""One frame of native observations and explicitly scoped module calls."""
from contextvars import ContextVar
from dataclasses import dataclass
import traceback


@dataclass(frozen=True, slots=True)
class Call:
    id: str = ""
    path: str = ""
    priority: int = 0


class Frame:
    def __init__(self, game, spatial, units, actions):
        """Bind native game/services and connect Actions to the current module call scope."""
        self.game, self.spatial, self.units, self.actions = game, spatial, units, actions
        self._current = ContextVar("module_call", default=Call())
        self._sequence = 0
        self.records = []
        actions.source = lambda: self._current.get()

    async def run(self, module, *, enabled=True, priority=None, skip_reason="", **inputs):
        """Call execute with explicit inputs, inherited priority and automatic source tracking.

        Disabled calls require skip_reason. Enforce a bool result, record exceptions
        and restore the parent scope even on failure. Return control intent, not
        arbitration success; larger explicit priorities override inherited values.
        """
        parent = self._current.get()
        self._sequence += 1
        module_id = type(module).__name__
        path = f"{parent.path}/{module_id}" if parent.path else module_id
        call = Call(f"call{self._sequence}", path,
                    parent.priority if priority is None else priority)
        record = dict(frame=self.game.state.game_loop, call_id=call.id, parent_id=parent.id,
                      module_id=module_id, module_path=type(module).__module__,
                      path=path, priority=call.priority)
        if not enabled:
            if not skip_reason:
                raise ValueError("Disabled modules require an explicit skip_reason")
            self.records.append(dict(record, status="skipped", skip_reason=skip_reason))
            return False
        token = self._current.set(call)
        try:
            result = await module.execute(self, **inputs)
            if type(result) is not bool:
                raise TypeError(f"{module_id}.execute must return bool")
            record["status"] = "invoked_true" if result else "invoked_false"
            if not result:
                record["reason"] = "no_intent"
            return result
        except Exception:
            record.update(status="exception", exception=traceback.format_exc())
            raise
        finally:
            self.records.append(record)
            self._current.reset(token)

    def drain(self):
        """Transfer accumulated module-call records to the telemetry consumer and clear them."""
        records, self.records = self.records, []
        return records
