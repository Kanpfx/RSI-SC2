"""Verify observational editing boundaries and retained numerical analyses."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from runtime.workspace.checks import validate_sampling
from runtime.workspace.versions import Versions
from tests.fixtures import ROOT


class ObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def candidate(self):
        versions = Versions(self.root / "outputs")
        parent = versions.freeze(ROOT)
        candidate = versions.candidate(parent, self.root / "candidate")
        return parent, candidate

    def test_collector_changes_are_accepted_but_strategy_and_lifecycle_are_fixed(self):
        parent, candidate = self.candidate()
        logger = candidate / "bot/telemetry/logger.py"
        original = logger.read_text(encoding="utf-8")
        logger.write_text(original.replace("        recorder = self.recorder",
            "        recorder = self.recorder\n"
            "        recorder.add_series('gas', fields=[('gas', '<i8')], "
            "sample=lambda game: (game.vespene,), every_frames=112)"), encoding="utf-8")
        self.assertTrue(validate_sampling(parent, candidate)["passed"])
        logger.write_text(original.replace("self._configure(every_frames, start_frame, end_frame)", "pass"), encoding="utf-8")
        self.assertFalse(validate_sampling(parent, candidate)["passed"])
        logger.write_text(original, encoding="utf-8")
        (candidate / "bot/components/tactics/bc_rush.py").write_text("# changed strategy\n", encoding="utf-8")
        self.assertFalse(validate_sampling(parent, candidate)["passed"])

    def test_invalid_collector_is_reported_before_starting_games(self):
        parent, candidate = self.candidate()
        logger = candidate / "bot/telemetry/logger.py"
        source = logger.read_text(encoding="utf-8")
        logger.write_text(source.replace("        recorder = self.recorder",
            "        recorder = self.recorder\n"
            "        recorder.add_series('invalid', fields=[('value', 'O')], sample=lambda game: (game,))"),
            encoding="utf-8")
        result = validate_sampling(parent, candidate)
        self.assertFalse(result["passed"])
        check = next(item for item in result["checks"] if item["name"] == "collectors")
        self.assertIn("fixed scalar types", check["output"])
