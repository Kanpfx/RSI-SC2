"""Check bootstrap deployment and outcome boundaries without starting Docker."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bot.telemetry.logger import Logger
from runtime.matches.runner import collect_result, main, prepare_match, run_match, validate_match
from runtime.workspace.storage import read_json, write_json
from runtime.workspace.versions import bot_inventory, hash_files, inventory
from runtime.matches.runner import run_batch


class MatchTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bootstrap = self.root / "runtime/matches/local-play-bootstrap"
        for name in ("bots/basic_bot", "maps"):
            (self.bootstrap / name).mkdir(parents=True)
        (self.bootstrap / "bots/basic_bot/run.py").write_text("pass\n")
        (self.bootstrap / "maps/AcropolisAIE.SC2Map").write_bytes(b"map")
        (self.bootstrap / "config.toml").write_text(
            "MAX_GAME_TIME = 1\nMAX_REAL_TIME = 1\nROUNDS_PER_RUN = -1\nREALTIME = true\n")
        source = self.root / "source"
        (source / "bot").mkdir(parents=True)
        (source / "bot/entry.py").write_text("# Bot\n")
        (source / "bot/run.py").write_text("# Ladder entry\n")
        self.source = dict(source_root=str(source), version=hash_files(bot_inventory(source)))
        self.conditions = dict(map="AcropolisAIE", opponent="basic_bot", opponent_race="T",
                               trial=1, game_time_limit=10, wall_timeout=60)
        names = ("sc2_controller", "bot_controller1", "bot_controller2", "proxy_controller")
        self.environment = dict(image_ids=dict.fromkeys(names, "sha256:test"),
            map_sha256=hashlib.sha256(b"map").hexdigest(),
            opponent_sha256=hash_files(inventory(self.bootstrap / "bots/basic_bot")))

    def output(self, directory, *, error=None):
        logs = directory / "logs/bot_controller1/research_bot"
        logs.mkdir(parents=True, exist_ok=True)
        summary = dict(result="Result.Victory", bot_runtime_error=error, steps=3,
                       game_version="4.10.0.75689", sc2_build=75689, game_step=2)
        (logs / "stderr.log").write_text("log prefix\n" + json.dumps(summary, indent=2))
        trace = directory / "logs/bot_controller1/telemetry/session"
        trace.mkdir(parents=True, exist_ok=True)
        logger = Logger(trace)
        for frame in (0, 2, 4):
            logger.observe(SimpleNamespace(minerals=50, state=SimpleNamespace(game_loop=frame,
                score=SimpleNamespace(killed_value_units=0, killed_value_structures=0))))
        logger.emit("game_ended", 20, result="Result.Victory")
        logger.close()

    def test_cli_uses_shared_config_for_native_and_bootstrap_matches(self):
        config = self.root / "config.json"
        settings = dict(map="AcropolisAIE", opponent_race="Z", game_time_limit=77,
                        wall_timeout=88, difficulty="Easy")
        write_json(config, dict(matches=settings, evaluation_styles=["Rush"]))
        for arguments, expected in (
            ([], dict(settings, opponent="builtin", trial=1, ai_build="Rush")),
            (["--opponent", "basic_bot", "--time-limit", "99"],
             dict(map="AcropolisAIE", opponent="basic_bot", opponent_race="Z",
                  game_time_limit=99, wall_timeout=88, trial=1)),
        ):
            with self.subTest(arguments=arguments), \
                    patch("sys.argv", ["matches", "--config", str(config), "--check", *arguments]), \
                    patch("runtime.matches.runner.load_environment", return_value={}) as environment, \
                    patch("builtins.print"):
                self.assertEqual(main(), 0)
                self.assertEqual(environment.call_args.args[1], expected)

    def test_deployment_is_isolated_and_uses_pinned_images(self):
        before = inventory(self.bootstrap)
        directory = self.root / "match"
        directory.mkdir()
        prepare_match(self.root, self.source, self.conditions, directory, self.environment)
        self.assertEqual(inventory(self.bootstrap), before)
        compose = read_json(directory / "compose.json")
        self.assertTrue(all(s["image"] == "sha256:test" for s in compose["services"].values()))
        self.assertIn("MAX_GAME_TIME = 224", (directory / "config.toml").read_text())
        self.assertIn("REALTIME = false", (directory / "config.toml").read_text())
        self.assertEqual((directory / "matches").read_text(),
                         "1,research_bot,T,python,2,opponent,T,python,AcropolisAIE\n")

    def test_invalid_and_legacy_conditions_are_rejected(self):
        for change in (dict(opponent="../other"), dict(map="map,other"), dict(opponent_race="R"),
                       dict(seed=1), dict(difficulty="Easy"), dict(trial=0), dict(game_time_limit=True)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_match(dict(self.conditions, **change))

    def test_opponent_changes_abort_deployment(self):
        (self.bootstrap / "bots/basic_bot/run.py").write_text("# changed\n")
        with self.assertRaisesRegex(ValueError, "Opponent changed"):
            prepare_match(self.root, self.source, self.conditions, self.root / "match", self.environment)

    def test_timeout_crash_and_missing_evidence_are_distinct(self):
        directory = self.root / "results"
        self.output(directory)
        for kind, frame, status, termination, outcome in (
            ("Player1Win", 20, "normal", "game_result", "win"),
            ("Tie", 224, "normal", "time_limit", "draw"),
            ("Player1Crash", 20, "bot_error", "bot_error", None),
            ("Player2Crash", 20, "unknown", "opponent_error", None),
            ("Error", 20, "unknown", "infrastructure_error", None),
        ):
            manifest = dict(self.conditions, environment=self.environment, actual={})
            result = collect_result(directory, manifest, dict(type=kind, game_steps=frame))
            self.assertEqual((result["execution_status"], result["termination"], result["outcome"]),
                             (status, termination, outcome))
        (directory / "logs/bot_controller1/research_bot/stderr.log").unlink()
        result = collect_result(directory, manifest, dict(type="Player1Win", game_steps=20))
        self.assertIsNone(result["outcome"])

    def test_startup_failure_still_stops_compose_and_saves_failure(self):
        calls = []
        def control(arguments, **kwargs):
            calls.append(arguments)
            if "up" in arguments:
                raise RuntimeError("startup failed")
            return "logs"
        directory = self.root / "failed"
        with patch("runtime.matches.runner.docker", side_effect=control):
            result = run_match(self.root, self.source, self.conditions, directory, environment=self.environment)
        self.assertEqual(result["termination"], "infrastructure_error")
        self.assertTrue(any("down" in args for args in calls))
        self.assertEqual(read_json(directory / "result.json"), result)

    def test_result_and_replay_collection_uses_the_same_entry(self):
        directory = self.root / "successful"
        def control(arguments, **kwargs):
            if "up" in arguments:
                self.output(directory)
                write_json(directory / "arena_results.json", dict(results=[dict(type="Player1Win", game_steps=20)]))
                (directory / "replays/game.SC2Replay").write_bytes(b"replay")
            return "logs"
        with patch("runtime.matches.runner.docker", side_effect=control):
            result = run_match(self.root, self.source, self.conditions, directory, environment=self.environment)
        self.assertEqual(result["outcome"], "win")
        self.assertEqual((directory / "match.SC2Replay").read_bytes(), b"replay")
        self.assertEqual(read_json(directory / "manifest.json")["actual"]["sc2_build"], 75689)

    def test_cancellation_does_not_launch_a_match(self):
        cancel = Event()
        cancel.set()
        with patch("runtime.matches.runner.docker") as control:
            result = run_match(self.root, self.source, self.conditions, self.root / "cancelled",
                               environment=self.environment, cancel=cancel)
        control.assert_not_called()
        self.assertEqual(result["termination"], "cancelled")

    def test_builtin_match_writes_one_artifact_set_and_preserves_bot_summary(self):
        conditions = dict(self.conditions, opponent="builtin", ai_build="Macro", difficulty="Hard")
        environment = dict(map_path=str(self.bootstrap / "maps/AcropolisAIE.SC2Map"),
                           map_sha256=self.environment["map_sha256"])
        for error in (None, "Fixture Bot failure"):
            with self.subTest(error=error):
                directory = self.root / ("native_error" if error else "native_success")

                def launch(command, **kwargs):
                    output = Path(command[command.index("--output") + 1])
                    self.assertEqual(output, directory)
                    logger = Logger(output)
                    logger.observe(SimpleNamespace(minerals=50, state=SimpleNamespace(game_loop=0,
                        score=SimpleNamespace(killed_value_units=0, killed_value_structures=0))))
                    if error:
                        logger.emit("bot_runtime_error", 20, exception=error)
                    logger.emit("game_ended", 20, result="Result.Victory")
                    logger.close()
                    write_json(output / "result.json", dict(result="Result.Victory",
                               bot_runtime_error=error, steps=1))
                    (output / "match.SC2Replay").write_bytes(b"replay")
                    return SimpleNamespace(poll=lambda: 0, returncode=0)

                with patch("runtime.matches.builtin.subprocess.Popen", side_effect=launch):
                    result = run_match(self.root, self.source, conditions, directory, environment=environment)
                self.assertEqual(result["execution_status"], "bot_error" if error else "normal")
                self.assertEqual(read_json(directory / "result.json"), result)
                self.assertEqual(read_json(directory / "bot_result.json")["result"], "Result.Victory")
                self.assertEqual(len(list(directory.rglob("match.SC2Replay"))), 1)
                self.assertEqual(len(list(directory.rglob("minerals.bin"))), 1)
                self.assertFalse((directory / "telemetry").exists())

    def test_fifteen_builtin_games_share_inputs_and_limit_process_concurrency(self):
        from rsi.evaluation import conditions
        config = read_json(Path(__file__).resolve().parents[1] / "rsi/config.json")
        suite = conditions(config)
        environment = dict(backend="builtin", map_path=str(self.bootstrap / "maps/AcropolisAIE.SC2Map"),
                           map_sha256=self.environment["map_sha256"])
        before = inventory(self.root / "source")
        lock, first_wave = Lock(), Barrier(2)
        active, peak, commands = 0, 0, []

        def launch(source, command, directory, timeout, cancel):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                commands.append(command)
                first = len(commands) <= 2
            try:
                if first:
                    first_wave.wait(timeout=5)
                logger = Logger(directory)
                logger.observe(SimpleNamespace(minerals=50, state=SimpleNamespace(game_loop=0,
                    score=SimpleNamespace(killed_value_units=0, killed_value_structures=0))))
                logger.emit("game_ended", 20, result="Result.Victory")
                logger.close()
                write_json(directory / "result.json", dict(result="Result.Victory", steps=1))
                (directory / "match.SC2Replay").write_bytes(b"replay")
                return dict(termination="process_exit", returncode=0)
            finally:
                with lock:
                    active -= 1

        with patch("runtime.matches.runner.load_environment", return_value=environment) as prepare, \
                patch("runtime.matches.builtin._run_local", side_effect=launch), \
                patch("runtime.matches.runner.docker") as docker:
            batch = run_batch(self.root, self.source, suite, self.root / "batch", "evaluation",
                              parallel=config["match_parallelism"])
        prepare.assert_called_once()
        docker.assert_not_called()
        self.assertEqual(len(commands), 15)
        self.assertEqual(peak, 2)
        self.assertEqual(inventory(self.root / "source"), before)
        self.assertTrue(all(result["outcome"] == "win" for result in batch["results"]))
        self.assertEqual({(c[c.index("--ai-build") + 1], int(c[c.index("--seed") + 1])) for c in commands},
                         {(style, trial) for style in ("Macro", "Timing", "Rush") for trial in range(1, 6)})
        for condition, path in zip(suite, batch["runs"].values(), strict=True):
            self.assertTrue((Path(path) / "match.SC2Replay").is_file())
            self.assertEqual(read_json(Path(path) / "manifest.json")["ai_build"], condition["ai_build"])
