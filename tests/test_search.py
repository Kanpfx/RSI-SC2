"""Exercise branching search with real snapshots and scripted, offline matches/models."""
import copy
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from runtime.workspace.storage import project_path, read_json, write_json
from rsi.experiment import Experiment, run_experiment
from rsi.agents.discovery import Discovery
from rsi.agents.hypotheses import Hypotheses
from rsi.agents.investigation import Investigation
from rsi.agents.resolution import Resolution
from rsi.evaluation import frontier, select_parent
from rsi.workflow import Run
from runtime.workspace.archives import archive_batch
from runtime.matches.evidence import read_telemetry, read_dataset
from runtime.workspace.versions import SnapshotError
from tests.fixtures import ROOT, fixture_batch, task, metric, problem, hypotheses, diagnosis, evidence, tests


class SearchTests(unittest.TestCase):
    def setUp(self):
        """Keep all generated candidates and evidence in an isolated temporary project."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(ROOT / "bot", self.root / "bot", ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "knowledge").mkdir()
        self.config = read_json(ROOT / "rsi/config.json")
        self.config.update(trials=1, max_repairs=0, evaluation_styles=["Macro"])
        self.tasks = [task("T1", "0"), task("T2", "1")]
        self.edits = []
        self.matches = []
        self.runtimes = []

    def collect(self, job, on_sampling, mode="reobserve"):
        """Exercise the controller's sampling callback without a model or games."""
        if mode == "reobserve":
            path = project_path(job["workspace"]) / "bot/telemetry/logger.py"
            source = path.read_text(encoding="utf-8")
            if "add_series('gas'" not in source:
                source = source.replace("        recorder = self.recorder", "        recorder = self.recorder\n"
                    "        recorder.add_series('gas', fields=[('vespene', '<i8')], "
                    "sample=lambda game: (game.vespene,), every_frames=112)")
                path.write_text(source, encoding="utf-8")
        value = on_sampling(dict(matches=1, mode=mode, ai_build=None))
        self.assertNotIn("error", value)
        return value

    def result(self, job, directory, value, analyses=None):
        """Validate the same final JSON contract as the real SDK boundary."""
        classes = dict(discovery=Discovery, hypotheses=Hypotheses, investigation=Investigation, resolution=Resolution)
        stage = classes[job["role"]](job, directory)
        if analyses is not None:
            stage.state["analyses"] = analyses
        result = stage.summary(json.dumps(value))
        return dict(status="ok", value=result, result_path=str(stage.path),
                    execution_path=str(stage.execution_path), usage=dict(tokens=10, cost=0.1))

    def supported(self, **changes):
        value = dict(action="diagnosis", diagnosis=diagnosis(), tests=tests())
        return dict(value, **changes)

    def model(self, runtime, job, directory, *, on_sampling=None):
        """Use final JSON for research and edit only the assigned candidate task."""
        write_json(directory / "job.json", job)
        self.runtimes.append(runtime)
        if job.get("mode") == "implement":
            assigned = job["task"]["task"]["id"]
            path = project_path(job["workspace"]) / "bot/components/tactics/bc_rush.py"
            before = path.read_text(encoding="utf-8")
            self.edits.append((assigned, before, job["task"]))
            if f"# {assigned}\n" not in before:
                path.write_text(before + f"\n# {assigned}\n", encoding="utf-8")
            return dict(status="ok", value="implemented", usage=dict(tokens=10, cost=0.1))
        self.assertEqual(job["max_analyses"], self.config["max_analyses"])
        if job["role"] == "discovery":
            overview = read_json(project_path(job["task"]["evidence_index"]))
            if not job["task"].get("metrics") and job["sampling_remaining"]:
                overview = read_json(project_path(self.collect(job, on_sampling)["evidence_index"]))
            run = overview["runs"][-1]
            table = read_dataset(project_path(run["log_path"]).parent, "gas")
            artifact = directory / "fixture_analysis.json"
            write_json(artifact, dict(rows=len(table)))
            analyses = job.get("analysis_records", []) + [
                dict(command="fixture read_dataset", returncode=0, artifact=str(artifact))]
            return self.result(job, directory, dict(action="problem", **problem(), metrics=[metric()]), analyses)
        if job["role"] == "hypotheses":
            value = dict(action="hypotheses", hypotheses=hypotheses())
        elif job["role"] == "investigation":
            value = self.supported()
        else:
            value = dict(action="resolution", tasks=self.tasks)
        return self.result(job, directory, value)

    def match(self, project, version, conditions, directory, purpose, parallel, *, wall_budget=None):
        """Give T1 a win without starting SC2 or computing expert metrics."""
        batch = fixture_batch(project, version, conditions, directory, purpose, parallel)
        code = (project_path(version["source_root"]) / "bot/components/tactics/bc_rush.py").read_text(encoding="utf-8")
        self.matches.append((purpose, code, copy.deepcopy(conditions)))
        for run_path in batch["runs"].values():
            run = Path(run_path)
            if "# T1\n" in code:
                result = read_json(run / "result.json")
                result.update(outcome="win", termination="game_result")
                write_json(run / "result.json", result)
        return batch

    def run_search(self, rounds=1, model=None, match=None):
        """Replace only external boundaries; execute normal validation and archive bookkeeping."""
        with patch("runtime.matches.runner.load_environment", return_value={}), patch("rsi.experiment.preflight"), patch("rsi.experiment.load_settings", return_value=self.config["model"]), \
                patch("runtime.models.sessions.ExperimentRuntime.call_session", autospec=True, side_effect=model or self.model), \
                patch("rsi.workflow.run_batch", side_effect=match or self.match):
            return run_experiment(self.root, self.config, rounds=rounds)

    def test_siblings_are_independent_and_equal_scores_prefer_deeper_versions(self):
        original = (self.root / "bot/components/tactics/bc_rush.py").read_bytes()
        report = self.run_search(rounds=2)
        self.assertEqual(report["status"], "completed", report.get("reason"))
        archive = read_json(project_path(report["archive"]))
        self.assertEqual(len(archive["nodes"]), 4)
        self.assertEqual(len(archive["edges"]), 3)
        self.assertTrue(all("# T1\n" not in before and "# T2\n" not in before
                            for _, before, _ in self.edits[:2]))
        self.assertEqual(len(archive["frontier"]), 1)
        selected = next(iter(archive["frontier"]))
        self.assertEqual(archive["nodes"][selected]["depth"], 2)
        self.assertEqual(archive["nodes"][selected]["scores"], {"Macro": 1.0})
        self.assertEqual((self.root / "bot/components/tactics/bc_rush.py").read_bytes(), original)
        self.assertTrue(self.runtimes[0].closed)

    def test_duplicate_code_reuses_evidence_and_keeps_both_lineage_edges(self):
        def same_edit(runtime, job, directory, *, on_sampling=None):
            if job.get("mode") == "implement":
                job = copy.deepcopy(job)
                job["task"]["task"]["id"] = "T1"
            return self.model(runtime, job, directory, on_sampling=on_sampling)
        report = self.run_search(model=same_edit)
        self.assertEqual(report["status"], "completed", report.get("reason"))
        archive = read_json(project_path(report["archive"]))
        self.assertEqual(len(archive["nodes"]), 2)
        self.assertEqual(len(archive["edges"]), 2)
        self.assertEqual(len(self.matches), 3)
        iteration = read_json(project_path(report["iterations"][0]["report"]))
        self.assertTrue(iteration["branches"][1]["reused_evidence"])

    def test_multi_style_evaluation_archives_vectors_without_champion(self):
        self.config["trials"] = 5
        self.config["evaluation_styles"] = ["Macro", "Timing", "Rush"]

        def matches(*args, **kwargs):
            self.assertEqual(args[5], min(len(args[2]), self.config["match_parallelism"]))
            batch = self.match(*args, **kwargs)
            code = (project_path(args[1]["source_root"]) / "bot/components/tactics/bc_rush.py").read_text(encoding="utf-8")
            for path in batch["runs"].values():
                condition = read_json(Path(path) / "manifest.json")
                win = ("# T1\n" in code and condition["ai_build"] == "Macro"
                       or "# T2\n" in code and condition["ai_build"] == "Timing")
                result = read_json(Path(path) / "result.json")
                result.update(outcome="win" if win else "loss", termination="game_result")
                write_json(Path(path) / "result.json", result)
            return batch

        report = self.run_search(match=matches)
        self.assertEqual(report["status"], "completed", report.get("reason"))
        self.assertEqual(report["iterations"][0]["status"], "evaluated")
        archive = read_json(project_path(report["archive"]))
        self.assertEqual(len(archive["nodes"]), 3)
        scores = [node["scores"] for node in archive["nodes"].values()]
        self.assertIn(dict(Macro=1.0, Timing=0.0, Rush=0.0), scores)
        self.assertIn(dict(Macro=0.0, Timing=1.0, Rush=0.0), scores)
        self.assertEqual(sorted(archive["frontier"].values()), [1, 2, 2])
        self.assertEqual(report["costs"]["matches"], 46)
        iteration = read_json(project_path(report["iterations"][0]["report"]))
        self.assertTrue(all(b["status"] == "evaluated" for b in iteration["branches"]))
        self.assertTrue(all("promotion" not in b for b in iteration["branches"]))

    def test_archived_batches_restore_for_later_diagnosis_and_are_cleaned_each_round(self):
        diagnoses = []

        def model(runtime, job, directory, *, on_sampling=None):
            if job["role"] == "investigation":
                runs = read_json(project_path(job["task"]["evidence_index"]))["runs"]
                batch_id = runs[-1]["batch_id"]
                for run in (r for r in runs if r["batch_id"] == batch_id):
                    brief, tables = read_telemetry(project_path(run["log_path"]).parent)
                    self.assertEqual(brief["status"], "completed")
                    self.assertIn("gas", tables)
                    self.assertNotIn("evaluation", tables)
                diagnoses.append(batch_id)
            return self.model(runtime, job, directory, on_sampling=on_sampling)

        with patch("runtime.workspace.archives.archive_batch", wraps=archive_batch) as compress:
            report = self.run_search(rounds=3, model=model)
        self.assertEqual(report["status"], "completed", report.get("reason"))
        self.assertEqual(len(diagnoses), 3)
        self.assertEqual(compress.call_count, len(self.matches))
        experiment = project_path(report["archive"]).parent
        self.assertEqual(len(list(experiment.glob("iterations/*/batches/*.zip"))), len(self.matches))
        self.assertFalse(any(p.is_dir() for p in experiment.glob("iterations/*/batches/batch_*")))
        self.assertNotIn("archive_errors", report)

    def test_archived_initial_batch_reuse_keeps_the_original_zip(self):
        first = self.run_search()
        archive = read_json(project_path(first["archive"]))
        batch = project_path(archive["nodes"][first["initial_parent"]["version"]]["batch"])
        original_zip = batch.parent.with_suffix(".zip").read_bytes()
        self.config["parent_batch"] = str(batch)
        matches = len(self.matches)

        def unresolved(runtime, job, directory, *, on_sampling=None):
            if job["role"] != "investigation":
                return self.model(runtime, job, directory, on_sampling=on_sampling)
            self.assertEqual(job["role"], "investigation")
            runs = read_json(project_path(job["task"]["evidence_index"]))["runs"]
            for run in runs:
                brief, tables = read_telemetry(project_path(run["log_path"]).parent)
                self.assertTrue(tables)
                self.assertEqual(brief["status"], "completed")
            return self.result(job, directory, dict(action="unresolved", reason="fixture"))

        second = self.run_search(rounds=2, model=unresolved)
        self.assertEqual(second["status"], "completed", second.get("reason"))
        self.assertEqual(len(self.matches), matches + 2)
        self.assertEqual(batch.parent.with_suffix(".zip").read_bytes(), original_zip)
        self.assertFalse(batch.parent.exists())
        self.assertNotIn("archive_errors", second)

    def test_archival_failure_does_not_change_evaluation_or_remove_data(self):
        with patch("runtime.workspace.archives.archive_batch", side_effect=OSError("Fixture disk error")):
            report = self.run_search()
        self.assertEqual(report["status"], "completed", report.get("reason"))
        self.assertEqual(report["iterations"][0]["status"], "evaluated")
        self.assertEqual(len(report["archive_errors"]), len(self.matches))
        for error in report["archive_errors"]:
            self.assertIn("Fixture disk error", error["error"])
            self.assertTrue((project_path(error["batch"]) / "batch.json").is_file())

    def test_bot_failure_rejects_only_its_branch(self):
        def matches(*args, **kwargs):
            batch = self.match(*args, **kwargs)
            if args[4] == "candidate_t01":
                for path in batch["runs"].values():
                    write_json(Path(path) / "result.json", dict(read_json(Path(path) / "result.json"), execution_status="bot_error", outcome=None))
            return batch
        report = self.run_search(match=matches)
        self.assertEqual(report["status"], "completed", report.get("reason"))
        archive = read_json(project_path(report["archive"]))
        self.assertEqual([edge["task_id"] for edge in archive["edges"]], ["T2"])
        self.assertEqual(len(self.matches), 4)

    def test_failed_editor_retains_its_owned_workspace(self):
        def failed_editor(runtime, job, directory, *, on_sampling=None):
            if job.get("mode") == "implement" and job["task"]["task"]["id"] == "T1":
                return dict(status="error", reason="fixture worker failure", usage=dict(tokens=0, cost=0))
            return self.model(runtime, job, directory, on_sampling=on_sampling)
        report = self.run_search(model=failed_editor)
        self.assertEqual(report["status"], "completed")
        self.assertTrue(self.runtimes[0].closed)
        iteration = read_json(project_path(report["iterations"][0]["report"]))
        branch = iteration["branches"][0]
        self.assertEqual(iteration["branches"][1]["status"], "evaluated")
        self.assertEqual(branch["status"], "error")
        self.assertEqual(branch["workspace"]["status"], "retained")
        directory = project_path(branch["workspace"]["path"])
        self.assertTrue((directory / "code/bot/entry.py").is_file())
        owner = read_json(directory / "workspace.json")
        self.assertEqual(owner["task_id"], "T1")
        self.assertEqual(project_path(owner["iteration"]), project_path(report["iterations"][0]["report"]))

    def test_shared_match_budget_skips_unstarted_siblings(self):
        self.config["max_matches"] = 3
        with patch("runtime.matches.runner.load_environment", return_value={}), patch("rsi.experiment.preflight"), patch("rsi.experiment.load_settings", return_value=self.config["model"]), \
                patch("runtime.models.sessions.ExperimentRuntime.call_session", autospec=True, side_effect=self.model), \
                patch("rsi.workflow.run_batch", side_effect=self.match):
            experiment = Experiment(self.root, self.config)
            self.addCleanup(experiment.runtime.close)
            experiment.prepare()
            run = Run(experiment)
            report = run.execute().report
        self.assertEqual(report["status"], "evaluated", report.get("reason"))
        self.assertEqual([branch["status"] for branch in report["branches"]], ["evaluated", "skipped"])
        self.assertEqual(len(self.edits), 1)
        self.assertEqual(report["costs"]["matches"], self.config["max_matches"])
        self.assertAlmostEqual(sum(item["cost"] for item in report["costs"]["sessions"]), 0.5)

    def test_discovery_can_sample_twice_and_retains_every_evidence_batch(self):
        self.config["trials"] = 2
        observations = []

        def model(runtime, job, directory, *, on_sampling=None):
            if job["role"] == "discovery":
                observations.append(len(read_json(project_path(job["task"]["evidence_index"]))["runs"]))
                first = self.collect(job, on_sampling)
                observations.append(len(read_json(project_path(first["evidence_index"]))["runs"]))
                second = self.collect(job, on_sampling, mode="repeat")
                observations.append(len(read_json(project_path(second["evidence_index"]))["runs"]))
                self.assertEqual(second["sampling_remaining"], 0)
                job["task"]["metrics"] = [metric()]
                job["task"]["evidence_index"] = second["evidence_index"]
            return self.model(runtime, job, directory, on_sampling=on_sampling)

        report = self.run_search(model=model)
        self.assertEqual(report["iterations"][0]["status"], "evaluated")
        self.assertEqual(observations, [2, 3, 4])
        iteration = read_json(project_path(report["iterations"][0]["report"]))
        self.assertEqual(len(iteration["sampling"]), 2)
        self.assertEqual([s["conditions"][0]["trial"] for s in iteration["sampling"]], [1, 3])
        self.assertEqual(len(iteration["costs"]["sessions"]), 6)
        parent = report["initial_parent"]["version"]
        self.assertTrue(all(s["parent"] == parent for s in iteration["sampling"]))

    def test_diagnosis_samples_and_continues_without_an_extra_session(self):
        self.config["trials"] = 2
        visits = []

        def model(runtime, job, directory, *, on_sampling=None):
            if job["role"] == "investigation":
                visits.append(job)
                result = self.collect(job, on_sampling, mode="repeat")
                self.assertEqual(len(read_json(project_path(result["evidence_index"]))["runs"]), 4)
                self.assertEqual(result["sampling_remaining"], 0)
                revised = hypotheses()
                revised[0]["explanation"] = "A refined mechanism supported by new observations"
                value = self.supported(hypotheses=revised)
                value["tests"].insert(0, dict(value["tests"][0], outcome="inconclusive"))
                return self.result(job, directory, value)
            return self.model(runtime, job, directory, on_sampling=on_sampling)

        report = self.run_search(model=model)
        self.assertEqual(report["iterations"][0]["status"], "evaluated")
        self.assertEqual(len(visits), 1)
        self.assertEqual(report["costs"]["matches"], 8)

    def test_sampling_validation_error_can_be_repaired_without_spending_matches(self):
        visits = []

        def model(runtime, job, directory, *, on_sampling=None):
            if job["role"] == "discovery":
                visits.append(job)
                logger = project_path(job["workspace"]) / "bot/telemetry/logger.py"
                original = logger.read_text(encoding="utf-8")
                logger.write_text(original.replace("self._configure(every_frames, start_frame, end_frame)", "pass"),
                    encoding="utf-8")
                rejected = on_sampling(dict(matches=1, mode="reobserve", ai_build=None))
                self.assertIn("error", rejected)
                self.assertEqual(rejected["sampling_remaining"], self.config["trials"])
                logger.write_text(original, encoding="utf-8")
            return self.model(runtime, job, directory, on_sampling=on_sampling)

        report = self.run_search(model=model)
        self.assertEqual(report["iterations"][0]["status"], "evaluated")
        self.assertEqual(len(visits), 1)
        self.assertEqual(report["costs"]["matches"], 4)

    def test_refuted_explanations_are_revised_without_an_extra_generation_session(self):
        visits = []

        def model(runtime, job, directory, *, on_sampling=None):
            visits.append(job["role"])
            if job["role"] == "investigation":
                revised = hypotheses()
                for item in revised:
                    item["explanation"] += " revised"
                value = self.supported(hypotheses=revised)
                value["tests"][0]["evidence"] = evidence("New trace supporting revised mechanisms")
                return self.result(job, directory, value)
            return self.model(runtime, job, directory, on_sampling=on_sampling)

        report = self.run_search(model=model)
        self.assertEqual(report["iterations"][0]["status"], "evaluated")
        self.assertEqual(visits.count("hypotheses"), 1)
        self.assertEqual(visits.count("investigation"), 1)

    def test_research_step_limit_stops_revisits_before_editing(self):
        self.config["max_research_steps"] = 4
        def model(runtime, job, directory, *, on_sampling=None):
            if job["role"] == "investigation":
                return self.result(job, directory, dict(action="discovery", reason="Problem needs refinement"))
            return self.model(runtime, job, directory, on_sampling=on_sampling)
        report = self.run_search(model=model)
        self.assertEqual(report["iterations"][0]["status"], "unresolved")
        self.assertFalse(self.edits)
        iteration = read_json(project_path(report["iterations"][0]["report"]))
        history = read_json(project_path(iteration["artifacts"]["research"]))["history"]
        self.assertEqual(len(history), 4)
        self.assertIn("step budget", iteration["reason"])

    def test_invalid_problem_returns_to_discovery_without_resetting_analysis_budget(self):
        visits = []

        def model(runtime, job, directory, *, on_sampling=None):
            if job["role"] == "discovery" and visits:
                current = read_json(project_path(job["iteration"]))
                self.assertIn("feedback", job["task"])
                for key in ("problem", "hypotheses", "diagnosis", "resolution"):
                    self.assertNotIn(key, job["task"])
                    self.assertNotIn(key, current)
                    self.assertNotIn(key, current["artifacts"])
            if job["role"] == "hypotheses" and visits:
                self.assertNotIn("feedback", job["task"])
            if job["role"] == "investigation":
                visits.append(job)
                if len(visits) == 1:
                    return self.result(job, directory, dict(action="discovery", reason="The comparison basis needs refinement"),
                        [dict(command="fixture analysis", returncode=0)])
                self.assertEqual(len(job["analysis_records"]), 1)
                self.assertFalse(job["task"].get("tests"))
            return self.model(runtime, job, directory, on_sampling=on_sampling)

        report = self.run_search(model=model)
        self.assertEqual(report["iterations"][0]["status"], "evaluated")
        self.assertEqual(len(visits), 2)

    def test_solution_design_can_return_for_further_diagnosis(self):
        visits = []

        def model(runtime, job, directory, *, on_sampling=None):
            if job["role"] == "investigation" and visits:
                self.assertNotIn("diagnosis", job["task"])
                self.assertNotIn("previous_tests", job["task"])
                self.assertNotIn("state", job["task"])
                current = read_json(project_path(job["iteration"]))
                self.assertNotIn("diagnosis", current)
                self.assertNotIn("diagnosis", current["artifacts"])
            if job["role"] == "resolution" and job.get("mode") != "implement":
                visits.append(job)
                if len(visits) == 1:
                    return self.result(job, directory, dict(action="investigation", reason="The mechanism needs more precise intervention conditions"))
                self.assertNotIn("feedback", job["task"])
            return self.model(runtime, job, directory, on_sampling=on_sampling)

        report = self.run_search(model=model)
        self.assertEqual(report["iterations"][0]["status"], "evaluated")
        self.assertEqual(len(visits), 2)


    def test_fatal_model_and_snapshot_errors_stop_immediately(self):
        for snapshot in (False, True):
            def fail(runtime, job, directory, *, on_sampling=None):
                if snapshot:
                    raise SnapshotError("Corrupted fixture snapshot")
                return dict(status="error", fatal=True, reason="Invalid API credentials", usage={})
            report = self.run_search(rounds=5, model=fail)
            self.assertEqual(report["status"], "error")
            self.assertEqual(len(report["iterations"]), 1)

    def test_successful_round_resets_the_consecutive_error_count(self):
        unresolved = (None, "Insufficient evidence")
        with patch.object(Run, "_research", side_effect=[RuntimeError("offline"), RuntimeError("offline"),
                                                       unresolved, RuntimeError("offline"), RuntimeError("offline")]):
            report = self.run_search(rounds=5)
        self.assertEqual(report["status"], "completed")
        self.assertEqual([r["status"] for r in report["iterations"]], ["error", "error", "unresolved", "error", "error"])
        self.assertEqual(report["consecutive_errors"], 2)
        self.assertEqual(len(self.matches), 1)

    def test_infrastructure_failure_skips_round_and_keeps_completed_children(self):
        self.tasks.append(task("T3", "0"))
        def matches(*args, **kwargs):
            batch = self.match(*args, **kwargs)
            if args[4] == "candidate_t02":
                for path in batch["runs"].values():
                    write_json(Path(path) / "result.json", dict(read_json(Path(path) / "result.json"), execution_status="unknown", outcome=None))
            return batch
        report = self.run_search(rounds=3, match=matches)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(len(report["iterations"]), 3)
        self.assertEqual(report["iterations"][0]["status"], "error")
        self.assertEqual([assigned for assigned, _, _ in self.edits[:2]], ["T1", "T2"])
        first = read_json(project_path(report["iterations"][0]["report"]))
        self.assertEqual([branch["task_id"] for branch in first["branches"]], ["T1", "T2"])
        archive = read_json(project_path(report["archive"]))
        self.assertEqual([edge["task_id"] for edge in archive["edges"]
                          if edge["iteration_id"] == first["iteration_id"]], ["T1"])
        self.assertNotEqual(report["current_parent"], report["initial_parent"])

    def test_task_plan_rejects_bad_links_ids_bounds_and_targets(self):
        write_json(self.root / "memory.json", [])
        resolution = Resolution(dict(role="resolution", parent=dict(source_root=str(ROOT)),
            max_tasks=3, task=dict(problem=problem(), hypotheses=hypotheses(), diagnosis=diagnosis())), self.root)
        invalid = [[], [task()] * 2, [task("T1"), task("t1")], [task(str(i)) for i in range(4)],
                   [task("../outside")], [task(hypothesis_id="unknown")],
                   [dict(task(), hypothesis_ids=["0", "0"])], [task(verification="")],
                   [task(code_targets=["bot/core/actions.py"])]]
        for tasks in invalid:
            with self.subTest(tasks=tasks), self.assertRaises(ValueError):
                resolution.summary(json.dumps(dict(action="resolution", tasks=tasks)))
        result = resolution.summary(json.dumps(dict(action="resolution", tasks=self.tasks)))
        self.assertEqual(result["tasks"], self.tasks)

    def test_parent_selection_uses_per_style_leadership_weights(self):
        archive = dict(nodes={
            "macro": dict(depth=1, scores=dict(Macro=1.0, Timing=0.0)),
            "timing": dict(depth=1, scores=dict(Macro=0.0, Timing=1.0)),
            "both": dict(depth=2, scores=dict(Macro=1.0, Timing=1.0)),
        })
        rng = Mock()
        rng.choices.return_value = ["both"]
        self.assertEqual(select_parent(archive, rng), "both")
        rng.choices.assert_called_once_with(["both", "macro", "timing"], weights=[2, 1, 1], k=1)

    def test_cleanup_failure_keeps_final_report_and_original_error(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                def model(runtime, job, directory, *, on_sampling=None):
                    if failed:
                        raise RuntimeError("Original model failure")
                    return self.result(job, directory, dict(action="unresolved", reason="Insufficient evidence"))

                with patch("rsi.experiment.ExperimentRuntime.close", side_effect=RuntimeError("Cleanup failed")):
                    report = self.run_search(model=model)
                self.assertEqual(report["status"], "error")
                self.assertIn("Cleanup failed", report["cleanup_error"])
                self.assertIn("Original model failure" if failed else "cleanup", report["reason"])
                path = project_path(report["runtime"]).parent.parent / "experiments" / report["experiment_id"] / "experiment.json"
                saved = read_json(path)
                self.assertEqual(saved["status"], report["status"])
                self.assertEqual(saved["reason"], report["reason"])
                self.assertEqual(saved["cleanup_error"], report["cleanup_error"])
