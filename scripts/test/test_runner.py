"""Check shared execution contracts without starting SC2 or calling a model."""
import hashlib
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from runner.artifacts import path_reference, project_path, read_json, write_json
from runner.matches import run_batch, run_match, worker
from runner.versions import Versions, bot_inventory, hash_files, source_hash, write_diff


ROOT = Path(__file__).resolve().parents[2]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        """Create a tiny source tree with binary and nested files to exercise version identity."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        (self.source / "bot/nested").mkdir(parents=True)
        (self.source / "bot/entry.py").write_bytes(b"# fixture\r\n")
        (self.source / "bot/nested/data.bin").write_bytes(b"\x00\xff\x01")
        self.environment = dict(sc2_path="fixture", sc2_build=98370)
        write_json(self.source / "environment.json", self.environment)
        self.conditions = dict(map="AbyssalReefLE", race="Zerg", difficulty="Easy", seed=1,
                               game_time_limit=150, wall_timeout=10)

    def test_hash_matches_the_previous_snapshot_format(self):
        legacy = hashlib.sha256()
        for path in sorted((self.source / "bot").rglob("*")):
            if path.is_file():
                legacy.update(("bot/" + path.relative_to(self.source / "bot").as_posix()).encode()
                              + b"\0" + path.read_bytes() + b"\0")
        version = Versions(self.source, self.root / "versions").freeze(self.source)
        self.assertEqual(version["version"], legacy.hexdigest())
        self.assertEqual(source_hash(project_path(version["source_root"])), legacy.hexdigest())
        self.assertEqual(hash_files(bot_inventory(self.source)), legacy.hexdigest())

    def test_artifact_paths_support_an_explicit_project_root(self):
        path = self.source / "runtime/result.json"
        reference = path_reference(path, root=self.source)
        self.assertEqual(reference, "runtime/result.json")
        self.assertEqual(project_path(reference, root=self.source), path)

    def test_diff_ignores_line_endings_and_writes_lf(self):
        output = self.root / "patch.diff"
        before = {"file.py": b"keep\nold\nend\n"}
        write_diff(before, {"file.py": b"keep\r\nold\r\nend\r\n"}, ["file.py"], output)
        self.assertEqual(output.read_bytes(), b"")
        write_diff(before, {"file.py": b"keep\r\nnew\r\nend\r\n"}, ["file.py"], output)
        self.assertEqual(output.read_bytes(),
                         b"--- a/file.py\n+++ b/file.py\n@@ -1,3 +1,3 @@\n keep\n-old\n+new\n end\n")

    def test_source_and_snapshot_runs_share_the_result_contract(self):
        snapshot = Versions(self.source, self.root / "versions").freeze(self.source)
        for name, source in (("source", self.source), ("snapshot", snapshot)):
            with self.subTest(name=name):
                output = self.root / name
                output = output / "match"
                with patch("runner.matches.subprocess.Popen") as popen:
                    popen.return_value.wait.side_effect = lambda **kwargs: write_json(
                        output / "result.json", dict(execution_status="normal", outcome="win",
                                                     termination="game_result", steps=3))
                    result = run_match(self.source, source, self.conditions, output)
                manifest = read_json(output / "manifest.json")
                self.assertEqual(result, read_json(output / "result.json"))
                self.assertEqual(manifest["version"], snapshot["version"])
                self.assertEqual(manifest["run_id"], output.name)
                self.assertEqual(result["steps"], 3)
                self.assertNotIn("version", result)
                self.assertNotIn("output_dir", manifest)
                self.assertEqual(manifest["environment"], self.environment)
                self.assertEqual(popen.call_args.args[0][3], "runner.matches")

    def test_cancelled_and_timed_out_runs_keep_the_same_required_fields(self):
        required = {"schema_version", "execution_status", "outcome", "termination", "last_frame",
                    "steps", "phase", "mean_step_ms", "max_step_ms", "error"}
        cancelled = Event()
        cancelled.set()
        output = self.root / "cancelled"
        with patch("runner.matches.subprocess.Popen") as popen:
            result = run_match(self.source, self.source, self.conditions, output, cancel=cancelled)
        popen.assert_not_called()
        self.assertEqual(set(result), required)
        self.assertEqual(result["termination"], "cancelled")
        self.assertEqual(result, read_json(output / "result.json"))
        self.assertTrue((output / "manifest.json").exists())
        output = self.root / "timeout"
        with patch("runner.matches.subprocess.Popen"), patch("runner.matches.stop_process") as stop:
            result = run_match(self.source, self.source, dict(self.conditions, wall_timeout=0), output)
        stop.assert_called_once()
        self.assertEqual(set(result), required)
        self.assertEqual(result["termination"], "wall_timeout")
        self.assertIsNone(result["last_frame"])

    def test_standalone_cli_delegates_to_the_same_runner(self):
        from bot.run import main
        output = self.root / "cli"
        with patch("sys.argv", ["bot.run", "--output", str(output), "--no-log"]), \
                patch("runner.matches.run_match", return_value=dict(execution_status="normal")) as run, \
                patch("builtins.print"):
            self.assertEqual(main(), 0)
        self.assertEqual(run.call_args.args[:2], (ROOT, ROOT))
        self.assertEqual(run.call_args.args[2], dict(map="AbyssalReefLE", race="Zerg", difficulty="Easy",
                                                   seed=1, game_time_limit=150, wall_timeout=600))
        self.assertFalse(run.call_args.kwargs["log"])
        with patch("sys.argv", ["bot.run", "--difficulty", "CheatInsane"]), \
                patch("runner.matches.run_match", return_value=dict(execution_status="normal")) as run, \
                patch("builtins.print"):
            self.assertEqual(main(), 0)
        self.assertEqual(run.call_args.args[2]["difficulty"], "CheatInsane")

    def test_batch_keeps_existing_startup_reserve_and_worker_timeout(self):
        source = dict(version="fixture", source_root=str(self.source))
        conditions = [dict(self.conditions, seed=seed) for seed in (1, 2, 3)]
        with patch("runner.matches.run_match", return_value={}) as run, \
                patch("runner.matches.time.sleep") as sleep:
            run_batch(self.source, source, conditions, self.root / "budget", "parent",
                      parallel=2, wall_budget=14)
        submitted = sorted((call.args[2] for call in run.call_args_list), key=lambda item: item["seed"])
        self.assertEqual(submitted, [dict(condition, wall_timeout=8) for condition in conditions])
        self.assertEqual([call.args for call in sleep.call_args_list], [(3,), (3,)])
        self.assertEqual([Path(call.args[3]).name for call in sorted(run.call_args_list, key=lambda call: call.args[2]['seed'])],
                         ['run_01', 'run_02', 'run_03'])
        output = self.root / "no_budget"
        with self.assertRaisesRegex(RuntimeError, "Insufficient wall budget"):
            run_batch(self.source, source, conditions, output, "parent", wall_budget=6)
        self.assertFalse(output.exists())

    def test_worker_uses_selected_source_and_preserves_game_arguments(self):
        output = self.root / "worker"
        output.mkdir()
        spec = dict(self.conditions, source_root=str(self.source), version="fixture", run_id="worker",
                    output_dir=str(output), environment=self.environment, log=False)
        write_json(output / "manifest.json", spec)
        bot = SimpleNamespace(state=SimpleNamespace(game_loop=2240), steps=3, runtime_error=None,
                              summary=lambda: dict(result="Result.Victory", steps=3))
        logger = SimpleNamespace(close=lambda: None, actual={}, error_ref=None)
        with patch("sys.path", list(sys.path)), patch.dict("os.environ"), \
                patch("bot.entry.ResearchBot", return_value=bot) as create_bot, \
                patch("bot.telemetry.logger.Logger", return_value=logger) as create_logger, \
                patch("sc2.maps.get", return_value=SimpleNamespace(data=b"map")), \
                patch("sc2.player.Bot", return_value="own"), \
                patch("sc2.player.Computer", return_value="opponent"), \
                patch("sc2.main.run_game") as run_game:
            worker(output / "manifest.json")
        create_bot.assert_called_once_with(logger=logger, sc2_build=98370)
        create_logger.assert_called_once_with(None, frame_step=2)
        self.assertEqual(run_game.call_args.kwargs, dict(realtime=False, random_seed=1, game_time_limit=150,
                                                        save_replay_as=str(output / "match.SC2Replay")))
        self.assertEqual(read_json(output / "manifest.json")["actual"]["map_sha256"], hashlib.sha256(b"map").hexdigest())
        result = read_json(output / "result.json")
        self.assertEqual(result["outcome"], "win")
        self.assertEqual(result["execution_status"], "normal")
        self.assertEqual(result["last_frame"], 2240)
