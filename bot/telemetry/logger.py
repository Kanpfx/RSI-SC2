"""Minimal Bot overview and configurable binary observations."""

from sc2.ids.unit_typeid import UnitTypeId

from .recorder import Recorder


class Logger:
    """Connect native callbacks to independent, read-only data collectors."""

    def __init__(self, directory=None, chunk_size=128, frame_step=2, *,
                 every_frames=112, start_frame=0, end_frame=None):
        if type(frame_step) is not int or frame_step < 1:
            raise ValueError("frame_step must be a positive integer")
        self.recorder = Recorder(directory, chunk_size=chunk_size)
        self.recorder.overview.update(actual=dict(game_step=frame_step), result=None,
                                      bot_runtime_error=None)
        self._configure(every_frames, start_frame, end_frame)

    def _configure(self, every_frames, start_frame, end_frame):
        """Define the example collectors; add research-specific measurements here."""
        recorder = self.recorder
        interval = dict(every_frames=every_frames, start_frame=start_frame, end_frame=end_frame)
        recorder.add_series(
            "minerals", fields=[("minerals", "<i8")],
            sample=lambda game: (game.minerals,), **interval)
        recorder.add_series(
            "kill_value", fields=[("units", "<f8"), ("structures", "<f8")],
            sample=lambda game: (game.state.score.killed_value_units,
                                 game.state.score.killed_value_structures), **interval)
        recorder.add_events(
            "construction", fields=[("tag", "<u8"), ("unit_type", "<u4"), ("stage", "u1")],
            start_frame=start_frame, end_frame=end_frame,
            labels={"stage": {0: "started", 1: "completed"}})

    @property
    def actual(self):
        """Expose engine metadata and recording failures to the existing runners."""
        actual = dict(self.recorder.overview["actual"])
        if self.recorder.failure is not None:
            actual["telemetry_error"] = self.recorder.failure
        return actual

    def observe(self, game):
        """Sample native observations before Bot decisions run."""
        self.recorder.observe(game)

    def emit(self, event, frame, **fields):
        """Keep lifecycle facts and the construction event example."""
        overview = self.recorder.overview
        if event == "game_started":
            overview["actual"].update(fields)
            self.recorder.flush()
        elif event == "game_ended":
            overview.update(result=fields["result"], end_frame=frame)
        elif event == "bot_runtime_error":
            overview["bot_runtime_error"] = fields["exception"]
            self.recorder.flush()
        else:
            try:
                self._event(event, frame, fields)
            except Exception as exc:
                self.recorder._fail("event_collector", exc)

    def _event(self, event, frame, fields):
        """Define callback measurements without changing lifecycle handling."""
        if event in {"construction_started", "construction_complete"}:
            self.recorder.emit_event(
                "construction", frame, tag=fields["tag"],
                unit_type=UnitTypeId[fields["type"]].value,
                stage=int(event == "construction_complete"))

    def consume(self, loop, records):
        """Keep framework exceptions without serializing routine execution traces."""
        for call in records["calls"]:
            if call["status"] == "exception":
                self.emit("bot_runtime_error", loop, exception=call["exception"])
        try:
            self._execution(loop, records)
        except Exception as exc:
            self.recorder._fail("execution_collector", exc)

    def _execution(self, loop, records):
        """Optionally collect selected execution events through recorder.emit_event."""

    def close(self):
        """Flush binary data and finalize the overview without affecting gameplay."""
        overview = self.recorder.overview
        status = ("error" if overview["bot_runtime_error"] else
                  "completed" if overview["result"] is not None else "interrupted")
        self.recorder.close(status=status)
