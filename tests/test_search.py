"""Exercise branching search with real snapshots and scripted, offline matches/models."""
import copy
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from runner.artifacts import project_path, read_json, write_json
from rsi.experiment.run import Experiment, run_experiment
from rsi.diagnosis.investigate import Investigation
from rsi.experiment.run import select_parent
from rsi.experiment.iteration import Run
from rsi.experiment.batches import archive_batch
from rsi.analysis.evidence import load_batch_analysis
from rsi.analysis.logs import read_records
from runner.versions import SnapshotError
from tests.fixtures import ROOT, fixture_batch, task


class SearchTests(unittest.TestCase):
    def setUp(self):
        """Keep all generated candidates and evidence in an isolated temporary project."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("bot", "knowledge"):
            shutil.copytree(ROOT / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "environment.json", self.root / "environment.json")
        self.config = read_json(ROOT / "rsi/config.json")
        self.config.update(parallel_matches=1, max_repairs=0)
        self.tasks = [task("T1", "0"), task("T2", "1")]
        self.edits = []
        self.matches = []
        self.runtimes = []

    def model(self, runtime, job, directory):
        """Add only the assigned marker, retaining the complete diagnosis outside each candidate."""
        write_json(directory / "job.json", job)
        self.runtimes.append(runtime)
        if job["role"] == "diagnosis":
            value = dict(issue=dict(problem="fixture", evidence=["Fixture logs"]),
                         hypotheses=[dict(id=str(i), explanation="fixture",
                                          predictions=dict(support="a", refute="b")) for i in range(2)],
                         queries=[], conclusion=dict(tasks=self.tasks))
            write_json(directory / "investigation.json", value)
        else:
            assigned = job["task"]["task"]["id"]
            path = project_path(job["workspace"]) / "bot/components/tasks/economy.py"
            before = path.read_text(encoding="utf-8")
            self.edits.append((assigned, before, job["task"]))
            # Stop expanding the champion, so round three must explore the weaker sibling.
            if "# T1\n" not in before and f"# {assigned}\n" not in before:
                path.write_text(before + f"\n# {assigned}\n", encoding="utf-8")
            value = "edited"
        return dict(status="ok", value=value, usage=dict(tokens=10, cost=0.1))

    def match(self, project, version, conditions, directory, purpose, parallel, *, wall_budget=None):
        """Give T1 a win, and a combined lineage better metrics, without starting SC2."""
        batch = fixture_batch(project, version, conditions, directory, purpose, parallel)
        code = (project_path(version["source_root"]) / "bot/components/tasks/economy.py").read_text(encoding="utf-8")
        self.matches.append((purpose, code, copy.deepcopy(conditions)))
        for run_path in batch["runs"].values():
            run = Path(run_path)
            if "# T1\n" in code:
                result = read_json(run / "result.json")
                result.update(outcome="win", termination="game_result")
                write_json(run / "result.json", result)
            if "# T1\n" in code and "# T2\n" in code:
                rows = [json.loads(line) for line in (run / "state.jsonl").read_text(encoding="utf-8").splitlines()]
                for row in rows:
                    if "score" in row:
                        row["score"] = {name: [value * 1.2 for value in values] for name, values in row["score"].items()}
                (run / "state.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return batch

    def run_search(self, rounds=1, model=None, match=None):
        """Replace only external boundaries; execute normal validation and archive bookkeeping."""
        with patch("rsi.experiment.run.preflight"), patch("rsi.experiment.run.load_settings", return_value=self.config["model"]), \
                patch("rsi.tools.llm.ExperimentRuntime.call_session", autospec=True, side_effect=model or self.model), \
                patch("rsi.experiment.iteration.run_batch", side_effect=match or self.match):
            return run_experiment(self.root, self.config, rounds=rounds)

    def test_siblings_are_independent_and_rejected_versions_remain_explorable(self):
        original = (self.root / "bot/components/tasks/economy.py").read_bytes()
        report = self.run_search(rounds=3)
        self.assertEqual(report["status"], "completed", report.get("reason"))
        self.assertEqual([item["status"] for item in report["iterations"]], ["accept", "reject", "accept"])
        archive = read_json(project_path(report["archive"]))
        self.assertEqual(len(archive["nodes"]), 4)
        self.assertEqual(len(archive["edges"]), 3)
        first = report["iterations"][0]
        siblings = [edge for edge in archive["edges"] if edge["iteration_id"] == first["iteration_id"]]
        self.assertEqual({edge["parent"] for edge in siblings}, {report["initial_parent"]["version"]})
        rejected = next(edge["child"] for edge in siblings if edge["decision"] == "reject")
        self.assertEqual(report["iterations"][2]["parent"]["version"], rejected)
        self.assertNotEqual(report["iterations"][1]["parent"]["version"], rejected)
        self.assertTrue(all("# T1\n" not in before and "# T2\n" not in before
                            for _, before, _ in self.edits[:2]))
        for assigned, _, payload in self.edits:
            self.assertEqual(payload["task"]["id"], assigned)
            self.assertEqual([h["id"] for h in payload["hypotheses"]], ["0" if assigned == "T1" else "1"])
            self.assertNotIn("conclusion", payload)
            self.assertNotIn("tasks", payload)
        self.assertEqual(report["costs"]["matches"], 4)
        self.assertTrue(all([condition["seed"] for condition in conditions] == [1]
                            for _, _, conditions in self.matches))
        self.assertEqual((self.root / "bot/components/tasks/economy.py").read_bytes(), original)
        self.assertEqual(len({id(runtime) for runtime in self.runtimes}), 1)
        self.assertTrue(self.runtimes[0].closed)
        self.assertEqual(project_path(report["runtime"]), self.runtimes[0].root)

    def test_duplicate_code_reuses_evidence_and_keeps_both_lineage_edges(self):
        def same_edit(runtime, job, directory):
            if job["role"] == "editor":
                job = copy.deepcopy(job)
                job["task"]["task"]["id"] = "T1"
            return self.model(runtime, job, directory)
        report = self.run_search(model=same_edit)
        self.assertEqual(report["status"], "completed", report.get("reason"))
        archive = read_json(project_path(report["archive"]))
        self.assertEqual(len(archive["nodes"]), 2)
        self.assertEqual(len(archive["edges"]), 2)
        self.assertEqual(len(self.matches), 2)
        iteration = read_json(project_path(report["iterations"][0]["report"]))
        self.assertTrue(iteration["branches"][1]["reused_evidence"])

    def test_archived_batches_restore_for_later_diagnosis_and_are_cleaned_each_round(self):
        diagnoses = []

        def model(runtime, job, directory):
            if job["role"] == "diagnosis":
                analysis = load_batch_analysis(job["analyses"][0])
                for run_id, run in analysis["runs"].items():
                    rows, problems = read_records(project_path(run["log_path"]).parent, run_id)
                    self.assertFalse(problems)
                    self.assertTrue(any(row["event"] == "state" for row in rows))
                    self.assertTrue(any(row["event"] == "action" for row in rows))
                diagnoses.append(analysis["batch_id"])
            return self.model(runtime, job, directory)

        with patch("rsi.experiment.batches.archive_batch", wraps=archive_batch) as compress:
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

        def unresolved(runtime, job, directory):
            self.assertEqual(job["role"], "diagnosis")
            analysis = load_batch_analysis(job["analyses"][0])
            for run_id, run in analysis["runs"].items():
                rows, problems = read_records(project_path(run["log_path"]).parent, run_id)
                self.assertTrue(rows)
                self.assertFalse(problems)
            return dict(status="ok", value=dict(conclusion=None, reason="fixture"), usage=dict(tokens=10, cost=0))

        second = self.run_search(rounds=2, model=unresolved)
        self.assertEqual(second["status"], "completed", second.get("reason"))
        self.assertEqual(len(self.matches), matches)
        self.assertEqual(batch.parent.with_suffix(".zip").read_bytes(), original_zip)
        self.assertFalse(batch.parent.exists())
        self.assertNotIn("archive_errors", second)

    def test_archival_failure_does_not_change_evaluation_or_remove_data(self):
        with patch("rsi.experiment.batches.archive_batch", side_effect=OSError("Fixture disk error")):
            report = self.run_search()
        self.assertEqual(report["status"], "completed", report.get("reason"))
        self.assertEqual(report["iterations"][0]["status"], "accept")
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
        self.assertEqual(len(self.matches), 3)

    def test_failed_editor_retains_its_owned_workspace(self):
        def failed_editor(runtime, job, directory):
            if job["role"] == "editor" and job["task"]["task"]["id"] == "T1":
                return dict(status="error", reason="fixture worker failure", usage=dict(tokens=0, cost=0))
            return self.model(runtime, job, directory)
        report = self.run_search(model=failed_editor)
        self.assertEqual(report["status"], "completed")
        self.assertTrue(self.runtimes[0].closed)
        iteration = read_json(project_path(report["iterations"][0]["report"]))
        branch = iteration["branches"][0]
        self.assertEqual(iteration["branches"][1]["status"], "reject")
        self.assertEqual(branch["status"], "error")
        self.assertEqual(branch["workspace"]["status"], "retained")
        directory = project_path(branch["workspace"]["path"])
        self.assertTrue((directory / "code/bot/entry.py").is_file())
        owner = read_json(directory / "workspace.json")
        self.assertEqual(owner["task_id"], "T1")
        self.assertEqual(project_path(owner["iteration"]), project_path(report["iterations"][0]["report"]))

    def test_shared_match_budget_skips_unstarted_siblings(self):
        with patch("rsi.experiment.run.preflight"), patch("rsi.experiment.run.load_settings", return_value=self.config["model"]), \
                patch("rsi.tools.llm.ExperimentRuntime.call_session", autospec=True, side_effect=self.model), \
                patch("rsi.experiment.iteration.run_batch", side_effect=self.match):
            experiment = Experiment(self.root, self.config)
            self.addCleanup(experiment.runtime.close)
            experiment.prepare()
            run = Run(experiment)
            run.report["costs"]["matches"] = self.config["max_matches"] - 2
            report = run.execute().report
        self.assertEqual(report["status"], "accept", report.get("reason"))
        self.assertEqual([branch["status"] for branch in report["branches"]], ["accept", "skipped"])
        self.assertEqual(len(self.edits), 1)
        self.assertEqual(report["costs"]["matches"], self.config["max_matches"])
        self.assertAlmostEqual(sum(item["cost"] for item in report["costs"]["sessions"]), 0.2)

    def test_fatal_model_and_snapshot_errors_stop_immediately(self):
        for snapshot in (False, True):
            def fail(runtime, job, directory):
                if snapshot:
                    raise SnapshotError("Corrupted fixture snapshot")
                return dict(status="error", fatal=True, reason="Invalid API credentials", usage={})
            report = self.run_search(rounds=5, model=fail)
            self.assertEqual(report["status"], "error")
            self.assertEqual(len(report["iterations"]), 1)

    def test_successful_round_resets_the_consecutive_error_count(self):
        unresolved = dict(conclusion=None, reason="Insufficient evidence")
        with patch.object(Run, "_diagnose", side_effect=[RuntimeError("offline"), RuntimeError("offline"),
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
        investigation = Investigation(dict(parent=dict(source_root=str(ROOT)), context=str(ROOT),
            diagnosis=self.config["diagnosis"], analyses=[], batches=[], memory=str(self.root / "memory.json")), self.root)
        investigation.update(issue=dict(problem="fixture", evidence=["Fixture observations"]), hypotheses=[
            dict(id=str(i), explanation="fixture", predictions=dict(support="a", refute="b")) for i in range(2)])
        invalid = [[], [task()] * 2, [task("T1"), task("t1")], [task(str(i)) for i in range(4)],
                   [task("../outside")], [task(hypothesis_id="unknown")],
                   [dict(task(), hypothesis_ids=["0", "0"])], [task(verification="")],
                   [task(code_targets=["bot/core/actions.py"])]]
        for tasks in invalid:
            with self.subTest(tasks=tasks), self.assertRaises(ValueError):
                investigation.conclude(dict(tasks=tasks))
            self.assertIsNone(investigation.state["conclusion"])
        result = investigation.conclude(dict(tasks=self.tasks))
        self.assertEqual(result["conclusion"]["tasks"], self.tasks)
        with self.assertRaises(ValueError):
            investigation.update(reason="Already concluded")

    def test_parent_selection_alternates_exploitation_and_exploration(self):
        archive = dict(nodes={"champion": dict(expansions=1), "weaker": dict(expansions=0)})
        self.assertEqual(select_parent(archive, "champion", 2), "champion")
        self.assertEqual(select_parent(archive, "champion", 3), "weaker")

    def test_cleanup_failure_keeps_final_report_and_original_error(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                def model(*args, **kwargs):
                    if failed:
                        raise RuntimeError("Original model failure")
                    result = self.model(*args, **kwargs)
                    result["value"].update(conclusion=None, reason="Insufficient evidence")
                    return result

                with patch("rsi.experiment.run.ExperimentRuntime.close", side_effect=RuntimeError("Cleanup failed")):
                    report = self.run_search(model=model)
                self.assertEqual(report["status"], "error")
                self.assertIn("Cleanup failed", report["cleanup_error"])
                self.assertIn("Original model failure" if failed else "cleanup", report["reason"])
                path = project_path(report["runtime"]).parent.parent / "experiments" / report["experiment_id"] / "experiment.json"
                saved = read_json(path)
                self.assertEqual(saved["status"], report["status"])
                self.assertEqual(saved["reason"], report["reason"])
                self.assertEqual(saved["cleanup_error"], report["cleanup_error"])
