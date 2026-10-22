"""Persist facts produced by execution; disabling logging never changes decisions."""
from enum import Enum
import json
from pathlib import Path


class Logger:
    def __init__(self, directory=None, sample_period=16):
        """Open a new trace file or disable persistence when directory is None."""
        self.file = None
        if directory is not None:
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            self.file = (directory / "trace.jsonl").open("x", encoding="utf-8", buffering=65536)
        self.sample_period, self.next_sample = sample_period, 0

    def emit(self, event, frame, **fields):
        """Persist one fact with its game frame; do nothing when logging is disabled."""
        if self.file:
            self.file.write(json.dumps(dict(event=event, frame=frame, **fields),
                default=lambda x: x.name if isinstance(x, Enum) else list(x),
                separators=(",", ":")) + "\n")

    def consume(self, frame):
        """Drain call/action facts and periodically sample native state without influencing decisions."""
        for record in frame.drain():
            self.emit("module_call", **record)
        for record in frame.actions.drain():
            self.emit("action", **record)
        if self.file is None:
            return
        game, loop = frame.game, frame.game.state.game_loop
        if loop >= self.next_sample:
            self.next_sample = loop + self.sample_period
            self.emit("state", loop, minerals=game.minerals, vespene=game.vespene,
                      supply_used=game.supply_used, supply_cap=game.supply_cap,
                      own=[self.unit(u) for u in game.units + game.structures],
                      enemies=[self.unit(u) for u in game.enemy_units + game.enemy_structures
                               if u.is_visible], enemy_scope="visible_only")
        if self.file:
            self.file.flush()

    @staticmethod
    def unit(unit):
        """Serialize a native unit and its orders for offline analysis, not runtime decision state."""
        return dict(tag=unit.tag, type=unit.type_id.name, position=tuple(unit.position),
                    health=unit.health, shield=unit.shield, build_progress=unit.build_progress,
                    orders=[dict(ability=o.ability.id.name,
                                 target=tuple(o.target) if hasattr(o.target, "x") else o.target,
                                 progress=o.progress) for o in unit.orders])

    def close(self):
        """Close the trace stream safely; repeated calls are harmless."""
        if self.file:
            self.file.close()
            self.file = None
