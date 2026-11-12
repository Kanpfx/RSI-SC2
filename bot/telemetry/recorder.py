"""Buffered NumPy structured records with a small JSON index."""

import json
from pathlib import Path
import re

import numpy as np


class Recorder:
    """Schedule scalar series and append events using explicit, fixed column types."""

    def __init__(self, directory=None, *, chunk_size=128):
        if type(chunk_size) is not int or chunk_size < 1:
            raise ValueError("chunk_size must be a positive integer")
        self.directory = Path(directory) if directory is not None else None
        self.chunk_size = chunk_size
        self.failure = None
        self.closed = False
        self.datasets = {}
        self.overview = dict(schema_version=4, format="numpy-structured", frame_unit="game_loop",
                             sample_phase="on_step_start", status="recording", steps=0,
                             last_frame=None, error=None, datasets={})
        if self.directory is not None:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                with (self.directory / "overview.json").open("x", encoding="utf-8") as stream:
                    json.dump(self.overview, stream)
            except OSError as exc:
                self._fail("open", exc)
                self.directory = None

    def _add(self, name, fields, kind, start_frame, end_frame, *, sample=None,
             every_frames=None, labels=None):
        """Register one fixed schema; frame is always a little-endian int64."""
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
            raise ValueError("Use a simple dataset name")
        if self.closed or name in self.datasets:
            raise ValueError("Cannot register a duplicate dataset or a closed recorder")
        if type(start_frame) is not int or start_frame < 0:
            raise ValueError("start_frame must be a nonnegative integer")
        if end_frame is not None and (type(end_frame) is not int or end_frame <= start_frame):
            raise ValueError("end_frame must be greater than start_frame")
        dtype = np.dtype([("frame", "<i8"), *fields])
        if len(dtype.names) < 2 or dtype.hasobject or any(
                dtype.fields[field][0].subdtype is not None
                or dtype.fields[field][0].kind not in "biufSU" for field in dtype.names):
            raise ValueError("Fields must have fixed scalar types; object arrays are unsupported")
        info = dict(kind=kind, path=name + ".bin", dtype=dtype.descr,
                    range=[start_frame, end_frame], rows=0, error=None)
        if kind == "series":
            info["every_frames"] = every_frames
        if labels is not None:
            info["labels"] = labels
        dataset = dict(info=info, dtype=dtype, sample=sample, next_frame=start_frame,
                       file=None, buffer=np.empty(self.chunk_size, dtype=dtype), used=0,
                       last_frame=None)
        self.datasets[name] = dataset
        self.overview["datasets"][name] = info
        if self.directory is not None:
            try:
                dataset["file"] = (self.directory / info["path"]).open("xb")
            except OSError as exc:
                self._fail("open", exc, dataset)
            self._save()

    def add_series(self, name, *, fields, sample, every_frames=112,
                   start_frame=0, end_frame=None):
        """Register a sampler returning a tuple in fields order over [start, end)."""
        if type(every_frames) is not int or every_frames < 1 or not callable(sample):
            raise ValueError("Provide a callable sample and a positive integer every_frames")
        self._add(name, fields, "series", start_frame, end_frame,
                  sample=sample, every_frames=every_frames)

    def add_events(self, name, *, fields, start_frame=0, end_frame=None, labels=None):
        """Register an irregular event table, optionally describing categorical codes."""
        self._add(name, fields, "events", start_frame, end_frame, labels=labels)

    @staticmethod
    def _active(dataset, frame):
        start, end = dataset["info"]["range"]
        return (dataset["file"] is not None and dataset["info"]["error"] is None
                and frame >= start and (end is None or frame < end))

    def observe(self, game):
        """Sample at the first observation due; never invent missed observations."""
        if self.closed:
            return
        frame = game.state.game_loop
        self.overview["steps"] += 1
        self.overview["last_frame"] = frame
        for dataset in self.datasets.values():
            info = dataset["info"]
            if info["kind"] != "series" or not self._active(dataset, frame) or frame < dataset["next_frame"]:
                continue
            try:
                self._append(dataset, frame, dataset["sample"](game))
                start = info["range"][0]
                dataset["next_frame"] = start + ((frame - start) // info["every_frames"] + 1) * info["every_frames"]
            except Exception as exc:
                self._fail("sample", exc, dataset)

    def emit_event(self, name, frame, **values):
        """Append a callback event without sampling or inferred transitions."""
        if self.closed:
            return
        dataset = self.datasets[name]
        if dataset["info"]["kind"] != "events":
            raise ValueError("emit_event requires an event dataset")
        if not self._active(dataset, frame):
            return
        try:
            fields = dataset["dtype"].names[1:]
            if set(values) != set(fields):
                raise ValueError("Event values must match the registered fields")
            self._append(dataset, frame, tuple(values[field] for field in fields))
        except Exception as exc:
            self._fail("event", exc, dataset)

    def _append(self, dataset, frame, values):
        if type(frame) is not int or frame < 0 or (
                dataset["last_frame"] is not None and frame < dataset["last_frame"]):
            raise ValueError("Frames must be nonnegative, nondecreasing integers")
        row = np.array((frame, *values), dtype=dataset["dtype"])
        dataset["buffer"][dataset["used"]] = row
        dataset["used"] += 1
        dataset["last_frame"] = frame
        if dataset["used"] == self.chunk_size:
            self._flush(dataset)
            self._save()

    def _flush(self, dataset):
        if dataset["file"] is None or not dataset["used"]:
            return
        try:
            dataset["buffer"][:dataset["used"]].tofile(dataset["file"])
            dataset["file"].flush()
            dataset["info"]["rows"] += dataset["used"]
            dataset["used"] = 0
        except (OSError, ValueError) as exc:
            self._fail("write", exc, dataset)
            try:
                dataset["file"].close()
            except OSError:
                pass
            finally:
                dataset["file"] = None

    def _fail(self, stage, exc, dataset=None):
        error = dict(stage=stage, type=type(exc).__name__, message=str(exc))
        if dataset is not None:
            dataset["info"]["error"] = error
        if self.failure is None:
            self.failure = self.overview["error"] = error

    def _save(self):
        if self.directory is None:
            return
        try:
            temporary = self.directory / "overview.json.tmp"
            temporary.write_text(json.dumps(self.overview, separators=(",", ":")), encoding="utf-8")
            temporary.replace(self.directory / "overview.json")
        except OSError as exc:
            self._fail("overview", exc)

    def flush(self):
        """Publish buffered records and refresh the overview without closing files."""
        if self.closed:
            return
        for dataset in self.datasets.values():
            self._flush(dataset)
        self._save()

    def close(self, *, status="completed"):
        """Flush bounded buffers and finalize the index, including partial failures."""
        if self.closed:
            return
        self.closed = True
        for dataset in self.datasets.values():
            self._flush(dataset)
            if dataset["file"] is not None:
                try:
                    dataset["file"].close()
                except OSError as exc:
                    self._fail("close", exc, dataset)
        self.overview["status"] = "error" if self.failure else status
        self._save()


def read_dataset(directory, name, *, mmap=False):
    """Read a table directly with NumPy, validating its indexed byte count."""
    directory = Path(directory)
    overview = json.loads((directory / "overview.json").read_text(encoding="utf-8"))
    if overview["schema_version"] != 4 or overview["format"] != "numpy-structured":
        raise ValueError("Unsupported telemetry format")
    info = overview["datasets"][name]
    dtype = np.dtype([tuple(field) for field in info["dtype"]])
    path = directory / info["path"]
    if not path.resolve().is_relative_to(directory.resolve()) or dtype.hasobject or dtype.itemsize == 0:
        raise ValueError("Invalid dataset path or dtype")
    if path.stat().st_size != info["rows"] * dtype.itemsize:
        raise ValueError("Dataset byte count differs from overview")
    if mmap and info["rows"]:
        return np.memmap(path, dtype=dtype, mode="r", shape=(info["rows"],))
    return np.fromfile(path, dtype=dtype)
