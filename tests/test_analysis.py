"""Verify evidence integrity and win-rate frontier selection without running games."""
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from rsi.evaluation import batch_problem, conditions, frontier, sampling_conditions, score_batch, select_parent
from rsi.config import ROLES, role_settings, validate_config
from runtime.matches.evidence import analyze_batch, read_dataset
from runtime.workspace.storage import read_json, write_json
from tests.fixtures import ROOT, fixture_batch


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = read_json(ROOT / "rsi/config.json")
        self.conditions = conditions(self.config)
        self.version = dict(version="fixture", source_root=str(ROOT))

    def batch(self, name):
        batch = fixture_batch(ROOT, self.version, self.conditions[:1], self.root / name, name)
        return batch, analyze_batch(batch)

    def test_native_index_has_no_inferred_metrics_or_events(self):
        batch, index = self.batch("sampling")
        run = next(iter(index["runs"].values()))
        self.assertEqual(run["coverage"]["integrity"], "complete")
        self.assertNotIn("metrics", run)
        self.assertNotIn("events", run)
        self.assertIsNone(batch_problem(index))
        self.assertNotIn("evaluation", run["datasets"])
        self.assertEqual(read_dataset(next(iter(batch["runs"].values())), "construction")["stage"].tolist(), [0, 1])

    def test_truncated_binary_is_incomplete_evidence(self):
        batch, _ = self.batch("parent")
        path = Path(next(iter(batch["runs"].values()))) / "minerals.bin"
        path.write_bytes(path.read_bytes()[:-1])
        self.assertEqual(batch_problem(analyze_batch(batch))["code"], "incomplete_execution")

    def test_event_only_collectors_do_not_require_periodic_samples(self):
        batch, _ = self.batch("events")
        directory = Path(next(iter(batch["runs"].values())))
        overview = read_json(directory / "overview.json")
        overview["datasets"] = {"construction": overview["datasets"]["construction"]}
        write_json(directory / "overview.json", overview)
        index = analyze_batch(batch)
        self.assertIsNone(next(iter(index["runs"].values()))["coverage"]["sample_ratio"])
        self.assertIsNone(batch_problem(index))

    def test_fixed_suite_scores_only_real_wins_and_requires_all_games(self):
        self.assertEqual(len(self.conditions), 15)
        runs = {}
        for i, condition in enumerate(self.conditions):
            won = condition["ai_build"] == "Macro"
            runs[str(i)] = dict(conditions=condition, environment={}, coverage=dict(integrity="complete"),
                outcome=dict(execution_status="normal", outcome="win" if won else "draw",
                             termination="game_result"))
        batch = dict(runs=runs)
        self.assertEqual(score_batch(batch, self.conditions)["scores"], dict(Macro=1.0, Rush=0.0, Timing=0.0))
        runs["0"]["outcome"]["termination"] = "time_limit"
        self.assertEqual(score_batch(batch, self.conditions)["scores"]["Macro"], 0.8)
        incomplete = copy.deepcopy(batch)
        incomplete["runs"].pop("0")
        with self.assertRaisesRegex(ValueError, "fixed opponent suite"):
            score_batch(incomplete, self.conditions)

    def test_config_supports_partial_candidate_budgets_and_role_overrides(self):
        validate_config(self.config)
        required = len(self.conditions) * 2
        validate_config(dict(self.config, max_matches=required))
        with self.assertRaisesRegex(ValueError, "max_matches"):
            validate_config(dict(self.config, max_matches=required - 1))
        validate_config(dict(self.config, parent_batch="existing/batch.json", max_matches=len(self.conditions)))
        for role in ROLES:
            self.assertEqual(role_settings(self.config["model"], role), self.config["model"])
        model = dict(self.config["model"], investigation=dict(max_output_tokens=4096))
        self.assertEqual(role_settings(model, "investigation")["max_output_tokens"], 4096)
        self.assertEqual(role_settings(model, "discovery"), self.config["model"])
        for key in ("trials", "max_analyses", "resolution_max_tasks"):
            for value in (0, True, 1.5):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, key):
                    validate_config(dict(self.config, **{key: value}))

    def test_trials_control_all_style_repetitions_and_new_sampling_seeds(self):
        config = dict(self.config, trials=2)
        suite = conditions(config)
        self.assertEqual(len(suite), 6)
        for style in config["evaluation_styles"]:
            self.assertEqual([c["trial"] for c in suite if c["ai_build"] == style], [1, 2])
        repeated = sampling_conditions(config, dict(matches=1, mode="repeat", ai_build="Rush"))
        self.assertEqual(repeated[0]["trial"], 3)

    def test_sampling_replays_or_adds_unique_targeted_conditions_without_changing_evaluation(self):
        request = dict(matches=1, mode="reobserve", ai_build=None)
        first = sampling_conditions(self.config, request)
        second = sampling_conditions(self.config, request, first)
        self.assertEqual((first[0]["ai_build"], first[0]["trial"]), ("Macro", 1))
        self.assertEqual((second[0]["ai_build"], second[0]["trial"]), ("Timing", 1))
        repeat = dict(request, mode="repeat", ai_build="Rush")
        extra = sampling_conditions(self.config, repeat, self.conditions)
        more = sampling_conditions(self.config, repeat, self.conditions + extra)
        self.assertEqual((extra[0]["ai_build"], extra[0]["trial"]), ("Rush", 6))
        self.assertEqual((more[0]["ai_build"], more[0]["trial"]), ("Rush", 7))
        self.assertEqual(conditions(self.config), self.conditions)

    def test_frontier_weights_and_equal_vectors_prefer_deeper_versions(self):
        archive = dict(nodes={
            "A": dict(depth=0, scores=dict(Macro=0.8, Timing=0.4, Rush=0.6)),
            "B": dict(depth=1, scores=dict(Macro=0.6, Timing=0.8, Rush=0.6)),
            "C": dict(depth=1, scores=dict(Macro=0.8, Timing=0.6, Rush=0.4)),
            "D": dict(depth=2, scores=dict(Macro=0.8, Timing=0.4, Rush=0.6)),
        })
        self.assertEqual(frontier(archive), {"B": 2, "C": 1, "D": 2})
        rng = Mock()
        rng.choices.return_value = ["D"]
        self.assertEqual(select_parent(archive, rng), "D")
        rng.choices.assert_called_once_with(["B", "C", "D"], weights=[2, 1, 2], k=1)
