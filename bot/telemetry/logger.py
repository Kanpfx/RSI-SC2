"""Lossless execution facts and columnar native observations, indexed by game_loop."""
from enum import Enum
import json
from pathlib import Path


SCORE_FIELDS = (
    "killed_value_units", "killed_value_structures", "collected_minerals", "collected_vespene",
    "used_minerals_army", "used_vespene_army", "total_used_minerals_army", "total_used_vespene_army",
) + tuple(f"lost_{resource}_{category}" for resource in ("minerals", "vespene")
          for category in ("none", "army", "economy", "technology", "upgrade"))
UNIT_COLUMNS = ("tag", "type", "position", "health", "shield", "build_progress", "orders")
ORDER_COLUMNS = ("ability", "target", "progress")
CALL_COLUMNS = ("call_id", "parent_id", "module_ref", "priority", "status", "detail")
NATIVE_EVENTS = {"unit_created", "unit_destroyed", "construction_started", "construction_complete"}


def identity(value, prefix):
    """Store sequential IDs as integers without changing the Bot's runtime IDs."""
    if isinstance(value, str) and value.startswith(prefix) and value[len(prefix):].isdigit():
        return int(value[len(prefix):])
    return value or None


class Logger:
    def __init__(self, directory=None, chunk_size=128, frame_step=2):
        self.file = self.state_file = self.events_file = None
        self.chunk_size = chunk_size
        self.frame_step = frame_step
        self.samples = []
        self.modules, self.requests, self.errors = {}, {}, {}
        self.actual = {}
        self.last_frame = None
        self.error_ref = None
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        if type(frame_step) is not int or frame_step < 1:
            raise ValueError("frame_step must be a positive integer")
        if directory is not None:
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            try:
                self.file = (directory / "trace.jsonl").open("x", encoding="utf-8", buffering=65536)
                self.state_file = (directory / "state.jsonl").open("x", encoding="utf-8", buffering=65536)
                self.events_file = (directory / "events.jsonl").open("x", encoding="utf-8", buffering=65536)
                self._write(self.file, dict(kind="schema", version=3, call_columns=CALL_COLUMNS))
                self._write(self.state_file, dict(kind="schema", version=3, frame_step=frame_step, unit_columns=UNIT_COLUMNS,
                    order_columns=ORDER_COLUMNS, phase="on_step_start", enemy_scope="visible_only"))
                self._write(self.events_file, dict(kind="schema", version=3))
            except BaseException:
                for stream in (self.file, self.state_file, self.events_file):
                    if stream is not None:
                        stream.close()
                raise

    @staticmethod
    def _write(stream, record):
        stream.write(json.dumps(record, default=lambda x: x.name if isinstance(x, Enum) else list(x),
                                separators=(",", ":")) + "\n")

    def emit(self, event, frame, **fields):
        """Route native callbacks; lifecycle metadata belongs to the Runner's records."""
        if event == "game_started":
            self.actual.update(fields)
        if self.file is None:
            return
        if event in NATIVE_EVENTS:
            self._write(self.events_file, dict(frame=frame, kind=event, data=fields))
        elif event == "state":
            self._sample(dict(frame=frame, **fields))
        elif event == "bot_runtime_error":
            self.error_ref = self._error(frame, fields["exception"])
        elif event not in {"game_started", "game_ended"}:
            self._write(self.file, dict(frame=frame, kind=event, data=fields))

    def _error(self, frame, exception):
        if exception not in self.errors:
            ref = len(self.errors) + 1
            self.errors[exception] = ref
            self._write(self.file, dict(frame=frame, kind="exception", data=dict(id=ref, traceback=exception)))
        return dict(file="trace.jsonl", error_id=self.errors[exception])

    def _execution(self, loop, records, actions):
        calls = []
        for record in records:
            key = (record["module_path"], record["path"])
            if key not in self.modules:
                ref = len(self.modules) + 1
                self.modules[key] = ref
                self._write(self.file, dict(frame=record["frame"], kind="module", data=dict(id=ref,
                    **{name: record[name] for name in ("module_id", "module_path", "path")})))
            detail = record.get("skip_reason")
            if record["status"] == "exception":
                detail = self._error(record["frame"], record["exception"])["error_id"]
            calls.append([identity(record["call_id"], "call"), identity(record["parent_id"], "call"),
                          self.modules[key], record.get("priority", 0), record["status"], detail])
        if calls:
            self._write(self.file, dict(frame=loop, kind="module_calls", data=calls))
        for record in actions:
            if not record.get("request_id"):
                data = {k: v for k, v in record.items() if k != "frame"}
                if "candidates" in data:
                    data["candidates"] = [identity(value, "request") for value in data["candidates"]]
                self._write(self.file, dict(frame=record["frame"], kind="action_error",
                                           data=data))
                continue
            ref = identity(record["request_id"], "request")
            parameters = {k: v for k, v in record.items()
                          if k not in {"frame", "status", "reason", "source", "priority", "request_id"}}
            parameters["call_id"] = identity(parameters["call_id"], "call")
            previous = self.requests.get(ref)
            data = dict(request_id=ref, status=record["status"], reason=record.get("reason", ""))
            if previous is None:
                data.update(parameters)
            else:
                data.update({k: v for k, v in parameters.items() if previous.get(k) != v})
            self.requests[ref] = parameters
            self._write(self.file, dict(frame=record["frame"], kind="action_request" if previous is None else "action_status", data=data))

    def _sample(self, sample):
        if self.samples and sample["frame"] != self.samples[-1]["frame"] + self.frame_step:
            self._flush_samples()
        self.samples.append(sample)
        if len(self.samples) >= self.chunk_size:
            self._flush_samples()

    def _flush_samples(self):
        if not self.samples:
            return
        columns = dict(start_frame=self.samples[0]["frame"])
        columns.update({name: [row[name] for row in self.samples]
                        for name in ("minerals", "vespene", "supply_used", "supply_cap")})
        columns["score"] = {name: [row.get("score", {}).get(name) for row in self.samples]
                            for name in dict.fromkeys(name for row in self.samples for name in row.get("score", {}))}
        for name in ("own", "enemies"):
            columns[name] = [[[unit.get(key) if key != "orders" else
                [[order.get(field) for field in ORDER_COLUMNS] for order in unit["orders"]]
                for key in UNIT_COLUMNS] for unit in row[name]] for row in self.samples]
        self._write(self.state_file, columns)
        self.state_file.flush()
        self.samples.clear()

    def observe(self, game):
        """Capture observations before strategy or local command submission mutates them."""
        if self.file is None:
            return
        self.last_frame = game.state.game_loop
        self._sample(dict(frame=self.last_frame, minerals=game.minerals, vespene=game.vespene,
            supply_used=game.supply_used, supply_cap=game.supply_cap,
            score={name: getattr(game.state.score, name) for name in SCORE_FIELDS},
            own=[self.unit(u) for u in game.units + game.structures],
            enemies=[self.unit(u) for u in game.enemy_units + game.enemy_structures if u.is_visible]))

    def consume(self, frame):
        records, actions = frame.drain(), frame.actions.drain()
        if self.file is not None:
            self._execution(frame.game.state.game_loop, records, actions)
            self.file.flush()
            self.events_file.flush()

    @staticmethod
    def unit(unit):
        return dict(tag=unit.tag, type=unit.type_id.name, position=tuple(unit.position),
            health=unit.health, shield=unit.shield, build_progress=unit.build_progress,
            orders=[dict(ability=o.ability.id.name,
                         target=tuple(o.target) if hasattr(o.target, "x") else o.target,
                         progress=o.progress) for o in unit.orders])

    def close(self):
        if self.file is not None:
            try:
                self._flush_samples()
            finally:
                for stream in (self.file, self.state_file, self.events_file):
                    stream.close()
                self.file = self.state_file = self.events_file = None
