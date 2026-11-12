"""Verify binary sampling, native events and independent recording failures."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import numpy as np

from bot.telemetry.logger import Logger
from bot.telemetry.recorder import Recorder, read_dataset


def game(frame, minerals=50):
    return SimpleNamespace(minerals=minerals, state=SimpleNamespace(game_loop=frame,
        score=SimpleNamespace(killed_value_units=150, killed_value_structures=200)))


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_example_tables_are_binary_and_preserve_native_values(self):
        logger = Logger(self.directory, chunk_size=2)
        logger.emit("game_started", 0, sc2_build=75689, game_version="4.10", game_step=2)
        for frame in (0, 2, 112, 224):
            logger.observe(game(frame, 50 + frame))
        logger.emit("construction_started", 5, tag=2**60 + 1, type="BARRACKS")
        logger.emit("construction_complete", 151, tag=2**60 + 1, type="BARRACKS")
        logger.emit("game_ended", 226, result="Result.Victory")
        logger.close()
        logger.close()

        overview = json.loads((self.directory / "overview.json").read_text())
        tables = {name: read_dataset(self.directory, name) for name in overview["datasets"]}
        self.assertEqual(overview["status"], "completed")
        self.assertEqual((overview["steps"], overview["last_frame"], overview["end_frame"]), (4, 224, 226))
        self.assertEqual(overview["actual"]["sc2_build"], 75689)
        np.testing.assert_array_equal(tables["minerals"]["frame"], [0, 112, 224])
        np.testing.assert_array_equal(tables["minerals"]["minerals"], [50, 162, 274])
        np.testing.assert_array_equal(tables["kill_value"]["units"], [150, 150, 150])
        np.testing.assert_array_equal(tables["construction"]["tag"], [2**60 + 1, 2**60 + 1])
        np.testing.assert_array_equal(tables["construction"]["stage"], [0, 1])
        self.assertEqual(overview["datasets"]["construction"]["labels"]["stage"]["1"], "completed")
        for name, table in tables.items():
            self.assertEqual((self.directory / f"{name}.bin").stat().st_size, table.nbytes)
            self.assertFalse(table.dtype.hasobject)
        self.assertFalse(list(self.directory.glob("*.jsonl")))

    def test_interval_is_half_open_and_skipped_frames_are_not_invented(self):
        recorder = Recorder(self.directory, chunk_size=2)
        self.addCleanup(recorder.close)
        sampled = []

        def sample(observation):
            sampled.append(observation.state.game_loop)
            return (observation.minerals,)

        recorder.add_series("selected", fields=[("value", "<i8")], sample=sample,
                            every_frames=4, start_frame=3, end_frame=13)
        recorder.add_events("events", fields=[("value", "u1")], start_frame=3, end_frame=13)
        for frame in (0, 2, 4, 6, 12, 12, 14):
            recorder.observe(game(frame))
            recorder.emit_event("events", frame, value=1)
        recorder.close()
        self.assertEqual(sampled, [4, 12])
        np.testing.assert_array_equal(read_dataset(self.directory, "selected")["frame"], [4, 12])
        np.testing.assert_array_equal(read_dataset(self.directory, "events")["frame"], [4, 6, 12, 12])

    def test_bad_sampler_preserves_other_series_and_prior_samples(self):
        recorder = Recorder(self.directory)
        self.addCleanup(recorder.close)

        def sample(observation):
            if observation.state.game_loop > 0:
                raise RuntimeError("collector failed")
            return (7,)

        recorder.add_series("bad", fields=[("value", "<i8")], sample=sample, every_frames=2)
        recorder.add_series("good", fields=[("value", "<i8")], sample=lambda observation: (1,), every_frames=2)
        for frame in (0, 2, 4):
            recorder.observe(game(frame))
        recorder.close()
        self.assertEqual(read_dataset(self.directory, "bad")["value"].tolist(), [7])
        self.assertEqual(len(read_dataset(self.directory, "good")), 3)
        overview = json.loads((self.directory / "overview.json").read_text())
        self.assertEqual(overview["status"], "error")
        self.assertEqual(overview["datasets"]["bad"]["error"]["message"], "collector failed")

    def test_disabled_recording_does_not_evaluate_samplers(self):
        recorder = Recorder()
        def sample(observation):
            self.fail("Disabled recorder evaluated a sampler")
        recorder.add_series("disabled", fields=[("value", "<i8")], sample=sample)
        recorder.observe(game(0))
        recorder.close()
        self.assertEqual(recorder.overview["datasets"]["disabled"]["rows"], 0)

    def test_existing_output_is_not_overwritten(self):
        path = self.directory / "overview.json"
        path.write_bytes(b"existing")
        logger = Logger(self.directory)
        logger.observe(game(0))
        logger.close()
        self.assertEqual(path.read_bytes(), b"existing")
        self.assertEqual(logger.actual["telemetry_error"]["stage"], "open")

    def test_write_failure_is_reported_without_escaping(self):
        recorder = Recorder(self.directory, chunk_size=1)
        self.addCleanup(recorder.close)
        recorder.add_series("value", fields=[("value", "<i8")], sample=lambda observation: (1,))
        dataset = recorder.datasets["value"]
        dataset["file"].close()
        recorder.observe(game(0))
        recorder.close()
        self.assertIsNotNone(recorder.failure)

    def test_reader_rejects_truncated_data(self):
        logger = Logger(self.directory)
        logger.observe(game(0))
        logger.close()
        with (self.directory / "minerals.bin").open("ab") as stream:
            stream.write(b"x")
        with self.assertRaisesRegex(ValueError, "byte count"):
            read_dataset(self.directory, "minerals")

    def test_schema_and_sampling_options_are_validated(self):
        recorder = Recorder()
        for change in ({"every_frames": 0}, {"start_frame": -1}, {"end_frame": 0},
                       {"fields": [("value", "O")]}, {"name": "../outside"}):
            options = dict(name="value", fields=[("value", "<i8")], sample=lambda observation: (1,))
            options.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                recorder.add_series(**options)
