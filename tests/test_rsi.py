"""Verify evidence boundaries and one offline iteration using scripted external boundaries."""
import copy
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from rsi.analysis.analyzer import Analyzer
from rsi.analysis.evidence import analyze_batch, save_batch_analysis
from rsi.diagnosis.investigate import Investigation
from rsi.diagnosis.session import fixed_pack, investigation_tools
from rsi.evaluator import METRICS, evaluate
from rsi.editor import make_job as editor_job
from rsi.experiment.memory import record
from rsi.experiment.run import Experiment, run_experiment
from rsi.experiment.iteration import Run
from rsi.tools.llm import execute
from rsi.tools.sdk import BudgetedLLM
from rsi.experiment.config import validate_config
from rsi.analysis.retriever import Retriever
from rsi.references import References, freeze_context
from rsi.tools.validate import validate
from runner.artifacts import project_path, read_json, write_json
from runner.versions import Versions, inventory

from tests.fixtures import ROOT, fixture_batch, task, plan, sdk_messages


class PipelineTests(unittest.TestCase):
    def make_run(self, config=None, *, root=None):
        """Build an explicitly owned experiment for single-round checks."""
        experiment = Experiment(ROOT, config or self.config, root=root or self.root / "experiment")
        self.addCleanup(experiment.runtime.close)
        return Run(experiment)

    def setUp(self):
        """Create isolated artifacts and one reusable native-log fixture."""
        self.enterContext(patch.dict(os.environ, LITELLM_LOCAL_MODEL_COST_MAP="True", OPENHANDS_SUPPRESS_BANNER="1"))
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = read_json(ROOT / "rsi/config.json")
        self.versions = Versions(ROOT, self.root / "runtime")
        self.parent = self.versions.freeze(ROOT)
        conditions = [dict(self.config["matches"], seed=1)]
        self.batch = fixture_batch(ROOT, self.parent, conditions, self.root / "parent", "parent")

    def test_facts_trace_and_holdout_boundary(self):
        analyzer = Analyzer(ROOT)
        report = analyze_batch(analyzer, self.batch)
        retriever = Retriever(self.parent["source_root"], ROOT, analyses=[report])
        run = next(iter(report["runs"].values()))
        self.assertEqual(run["coverage"]["integrity"], "complete")
        self.assertIn("mineral_income", run["coverage"]["missing_metrics"])
        self.assertEqual(run["actions"]["submitted_ratio"], 1)
        self.assertTrue(any(e["kind"] == "supply_block" for e in run["events"]))
        result = retriever.query_logs("trace", batch_id=self.batch["batch_id"], call_id=1, frame=[0, 22])
        self.assertTrue(any(r.get("data", {}).get("status") == "observed" for r in result["items"]))
        self.assertTrue(all("run_id" in i and "batch_id" in i for i in result["items"]))
        with self.assertRaises(ValueError):
            analyze_batch(analyzer, dict(self.batch, purpose="holdout"))
        retriever.max_chars = 10
        result = retriever.query_logs("overview", batch_id=self.batch["batch_id"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["items"], [])

    def test_raw_pagination_preserves_results_and_stops_at_page_limit(self):
        batch = analyze_batch(Analyzer(ROOT), self.batch)
        retriever = Retriever(self.parent["source_root"], ROOT, analyses=[batch], max_items=1000, max_chars=1000000)
        run = next(iter(batch["runs"].values()))
        run["metrics"]["workers"][-2] = 100
        query = dict(names=["workers", "minerals"], detail="raw")
        complete = retriever.query_logs("metrics", batch_id=self.batch["batch_id"], **query)["items"]
        retriever.max_items = 3
        for max_chars in (1000000, 6000):
            with self.subTest(max_chars=max_chars):
                retriever.max_chars = max_chars
                collected, offset = [], 0
                while True:
                    page = retriever.query_logs("metrics", batch_id=self.batch["batch_id"], offset=offset, **query)
                    collected.extend(page["items"])
                    if "next_offset" not in page:
                        break
                    self.assertGreater(page["next_offset"], offset)
                    offset = page["next_offset"]
                self.assertEqual(collected, complete)
                self.assertEqual(collected[0]["statistics"]["max"], 100)

        def events():
            """Fail if pagination consumes beyond its single lookahead row."""
            for i in range(4):
                yield dict(kind="unit_created", frame=i, data={})
            raise AssertionError("Read past the requested page")

        retriever.max_chars = 24000
        run["events"] = events()
        page = retriever.query_logs("events", batch_id=self.batch["batch_id"])
        self.assertEqual(len(page["items"]), 3)
        self.assertNotIn("truncated", page)
        self.assertEqual(page["next_offset"], 3)

    def test_raw_log_cache_separates_batches_with_the_same_run_name(self):
        """run_01 in two batches must retain each batch's original trace."""
        retriever = Retriever(self.parent['source_root'], ROOT)
        first = dict(run_id='run_01', log_path=str(self.root / 'batch_01/run_01/trace.jsonl'))
        second = dict(run_id='run_01', log_path=str(self.root / 'batch_02/run_01/trace.jsonl'))
        with patch('rsi.analysis.retriever.read_records', side_effect=[([dict(value=1)], []), ([dict(value=2)], [])]) as read:
            self.assertEqual(retriever._records(first), [dict(value=1)])
            self.assertEqual(retriever._records(second), [dict(value=2)])
            self.assertEqual(retriever._records(first), [dict(value=1)])
        self.assertEqual(read.call_count, 2)

    def test_reference_reads_are_scoped_and_bounded(self):
        context = self.root / "references"
        write_json(context / "knowledge/units.json", dict(SCV=dict(cost=dict(minerals=50))))
        path = context / "api/example.py"
        path.parent.mkdir()
        path.write_text("".join(f"# line {i}\n" for i in range(1, 251)), encoding="utf-8")
        references = References(self.parent["source_root"], context)
        result = references.read_reference("api/example.py", lines=[2, 4])
        self.assertEqual(result["source"], "# line 2\n# line 3\n# line 4")
        self.assertEqual(result["next_line"], 5)
        self.assertEqual(references.read_reference("api/example.py", lines=[10, 250])["end_line"], 129)
        self.assertEqual(references.read_reference("knowledge/units.json", key="SCV.cost")["value"], dict(minerals=50))
        for arguments in (dict(path="api/../knowledge/units.json"), dict(path="api/example.py", lines=[0, 10]),
                          dict(path="knowledge/units.json", lines=[1, 10])):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                references.read_reference(**arguments)
        references.max_chars = 10
        self.assertTrue(references.read_reference("api/example.py")["truncated"])

    def test_memory_returns_stored_records_and_marks_output_limits(self):
        values = [dict(iteration_id="older", status="inconclusive", conclusion=dict(change="Unconfirmed idea")),
                  dict(iteration_id="newer", status="reject", conclusion=dict(change="Another idea"))]
        context = self.root / "memory_context"
        memory_path = self.root / "iteration/memory.json"
        write_json(memory_path, values)
        write_json(context / "memory.json", [dict(iteration_id="stale_context")])
        with self.assertRaisesRegex(ValueError, "explicit frozen iteration path"):
            Retriever(self.parent["source_root"], context).query_memory()
        retriever = Retriever(self.parent["source_root"], context, memory_path=memory_path,
                              max_chars=max(len(json.dumps(v)) for v in values) + 5)
        result = retriever.query_memory()
        self.assertEqual(result["items"], values[:1])
        self.assertTrue(result["truncated"])
        self.assertNotIn("next_offset", result)
        self.assertEqual(retriever.query_memory("NEWER"), dict(items=values[1:]))
        self.assertEqual(retriever.query_memory("unmatched"), dict(items=[], reason="No matching experiences"))
        self.assertEqual(read_json(memory_path), values)

    def test_evaluation_uses_win_rate_then_core_metrics(self):

        def batch(name, wins, values=(100, 0.4, 100, 100, 100)):
            return dict(batch_id=name, runs={str(i): dict(conditions=dict(seed=i), environment={},
                outcome=dict(execution_status="normal", termination="game_result", outcome="win" if i < wins else "loss"),
                coverage=dict(integrity="complete", sample_ratio=1, last_frame=3360),
                frame=[0, 2016, 3136, 3360], counts={}, analysis=dict(config=dict(sample_gap_frames=3360)),
                metrics={rule["name"]: [value] * 4
                         for rule, value in zip(METRICS, values)}) for i in range(3)})
        parent = batch("parent", 0)
        for values, expected in (((100, 0.4, 100, 100, 100), "reject"),
                                 ((105, 0.42, 105, 100, 100), "accept"),
                                 ((120, 0.4, 120, 100, 100), "reject"),
                                 ((120, 0.5, 120, 50, 50), "accept"),
                                 ((104, 0.416, 104, 104, 104), "reject")):
            with self.subTest(values=values):
                self.assertEqual(evaluate(parent, batch("candidate", 0, values))["decision"], expected)
        self.assertEqual(evaluate(parent, batch("candidate", 1))["decision"], "accept")
        self.assertEqual(evaluate(batch("parent", 1), batch("candidate", 0, (200, 0.8, 200, 200, 200)))["decision"], "reject")
        missing = batch("candidate", 0, (120, 0.5, 120, 100, 100))
        missing["runs"]["0"]["metrics"].pop("army_value")
        result = evaluate(parent, missing)
        self.assertEqual(result["decision"], "reject")
        self.assertEqual(result["improved_fraction"], 0.4)
        zero = batch("parent", 0, (0, 0, 0, 0, 0))
        result = evaluate(zero, parent)
        self.assertEqual(result["decision"], "accept")
        self.assertIsNone(result["metrics"][0]["improvement_ratio"])
        self.assertTrue(result["metrics"][0]["improved"])
        self.assertEqual(evaluate(zero, zero)["decision"], "reject")
        for field, value in (("conditions", dict(seed=999)), ("environment", dict(build=0)),
                             ("coverage", dict(integrity="partial", sample_ratio=1)),
                             ("outcome", dict(execution_status="normal", outcome=None))):
            candidate = batch("candidate", 3)
            candidate["runs"]["0"][field] = value
            result = evaluate(parent, candidate)
            self.assertEqual(result["decision"], "reject")
            self.assertFalse(result["comparable"])
        reordered = batch("candidate", 1)
        reordered["runs"] = dict(reversed(list(reordered["runs"].items())))
        self.assertEqual(evaluate(parent, reordered)["decision"], "accept")
        candidate = batch("candidate", 0)
        for run in candidate["runs"].values():
            run["coverage"]["last_frame"] = 2240
        for run in parent["runs"].values():
            run["metrics"]["combat_value"][2:] = [1000, 1000]
            run["metrics"]["army_value"] = [100, 200, 200, 200]
        result = evaluate(parent, candidate)
        self.assertEqual(result["metrics"][0]["parent"], 100)
        self.assertEqual(result["metrics"][2]["parent"], 110)

    def test_native_score_metrics_and_missing_data(self):
        analyzer = Analyzer(ROOT)
        report = next(iter(analyze_batch(analyzer, self.batch)["runs"].values()))
        expected = dict(combat_value=1200, trade_efficiency=1200 / 1350, army_value=700,
                        army_investment=1400, resource_collection_rate=1600)
        for name, value in expected.items():
            self.assertAlmostEqual(report["metrics"][name][-1], value)
        self.assertNotIn("army_value", report["coverage"]["missing_metrics"])
        self.assertEqual(report["metrics"]["score.killed_value_units"][-1], 1000)
        path = Path(next(iter(self.batch["runs"].values()))) / "state.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for row in rows:
            if "score" in row:
                row["score"] = {}
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        missing = next(iter(analyze_batch(analyzer, self.batch)["runs"].values()))
        self.assertTrue(all(value is None for value in missing["metrics"]["combat_value"]))
        self.assertIn("army_value", missing["coverage"]["missing_metrics"])

    def investigation(self, policy="active"):
        """Construct an investigation from frozen references and saved static analysis."""
        context = self.root / ("context_" + policy)
        context = freeze_context(ROOT, context)
        memory_path = self.root / "memory.json"
        write_json(memory_path, [])
        options = dict(self.config["diagnosis"], query_policy=policy)
        analysis_path = self.root / policy / "analysis.json"
        save_batch_analysis(analysis_path, analyze_batch(Analyzer(context), self.batch))
        job = dict(project=str(ROOT), context=str(context), parent=self.parent,
                   batches=[self.batch], diagnosis=options, analyses=[str(analysis_path)], memory=str(memory_path))
        return Investigation(job, self.root), job

    def test_initial_batch_reuse_is_read_only_and_validated(self):
        analyze_batch(Analyzer(ROOT), self.batch)
        history = {p: p.read_bytes() for p in (self.root / "parent").rglob("*") if p.is_file()}
        project = self.root / "reuse_project"
        shutil.copytree(ROOT / "bot", project / "bot", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "knowledge", project / "knowledge")
        config = copy.deepcopy(self.config)
        config["parallel_matches"] = 1
        config["parent_batch"] = str(self.root / "parent/batch.json")
        diagnosis = dict(conclusion=None, reason="fixture", queries=[])
        with patch("rsi.experiment.run.preflight"), patch("rsi.experiment.run.load_settings", return_value=config["model"]), \
                patch("rsi.experiment.iteration.run_batch", side_effect=AssertionError("Unexpected match")), \
                patch.object(Run, "_diagnose", return_value=diagnosis):
            report = run_experiment(project, config)
        self.assertEqual(report["status"], "completed", report.get("reason"))
        iteration = read_json(project_path(report["iterations"][0]["report"]))
        analysis = read_json(project_path(iteration["artifacts"][self.batch["batch_id"] + "_analysis"]))
        self.assertGreater(analysis["summary"]["event_counts"].get("resource_float", 0), 0)
        self.assertEqual(report["costs"]["matches"], 0)
        self.assertEqual({p: p.read_bytes() for p in (self.root / "parent").rglob("*") if p.is_file()}, history)
        config["parallel_matches"] = 2
        with patch("rsi.experiment.iteration.run_batch", side_effect=AssertionError("Unexpected match")):
            invalid = run_experiment(project, config)
        self.assertEqual(invalid["status"], "error")
        self.assertIn("conditions differ", invalid["reason"])
        self.assertFalse(invalid["iterations"])

    def test_fixed_pack_allows_issue_selection_but_no_adaptive_query(self):
        investigation, _ = self.investigation("fixed")
        pack = fixed_pack(investigation.retriever, investigation.job["batches"], investigation.options["max_queries"])
        investigation.state["queries"].extend(pack)
        investigation.save()
        investigation.update(issue=dict(problem="fixture", evidence=["Predeclared fixture observations"]),
            hypotheses=[dict(id=str(i), explanation="fixture", predictions=dict(support="a", refute="b"))
                        for i in range(2)])
        for tool, arguments in (("query_logs", dict(kind="overview", purpose="test")),
                                ("read_reference", dict(path="source/entry.py")), ("query_memory", {})):
            with self.subTest(tool=tool), self.assertRaisesRegex(ValueError, "Fixed policy"):
                investigation.query(tool, **arguments)
        result = investigation.conclude(reason="No discriminating evidence")
        self.assertIsNone(result["conclusion"])
        self.assertEqual(result["reason"], "No discriminating evidence")
        with self.assertRaises(ValueError):
            investigation.update(hypotheses=[])

    def test_query_defaults_and_hypothesis_revision(self):
        investigation, _ = self.investigation()
        issue = dict(problem="fixture", evidence=["Initial overview"])
        hypotheses = [dict(id=str(i), explanation="fixture", predictions=dict(support="a", refute="b"))
                      for i in range(2)]
        investigation.update(issue=issue, hypotheses=hypotheses)
        investigation.query("query_logs", kind="metrics", purpose="Inspect workers", params=dict(names=["workers"]))
        filters = investigation.state["queries"][-1]["arguments"]["params"]
        self.assertEqual(filters["batch_id"], self.batch["batch_id"])
        self.assertNotIn("runs", filters)
        self.assertNotIn("time", filters)
        investigation.query("query_logs", kind="metrics", purpose="Inspect a specific interval",
                            params=dict(names=["workers"], frame=[0, 448], runs="all"))
        self.assertEqual(investigation.state["queries"][-1]["arguments"]["params"]["frame"], [0, 448])
        investigation.update(issue=dict(issue, evidence=issue["evidence"] + ["Worker samples from 0-20 seconds"]))
        with self.assertRaisesRegex(ValueError, "reason"):
            investigation.update(issue=dict(issue, problem="Different problem"))
        revised_issue = dict(issue, problem="Resources are not converted into army")
        accepted = investigation.update(issue=revised_issue, reason="Production logs identify a different bottleneck")
        self.assertEqual(accepted["issue"], revised_issue)
        self.assertEqual(read_json(investigation.directory / "investigation.json")["issue"], revised_issue)
        with self.assertRaisesRegex(ValueError, "reason"):
            investigation.update(hypotheses=[dict(hypotheses[0], explanation="revised")])
        accepted = investigation.update(hypotheses=[dict(hypotheses[0], explanation="revised"),
            dict(hypotheses[1], id="2", explanation="new upstream cause")], reason="Source suggests another cause")
        self.assertEqual([h["id"] for h in accepted["hypotheses"]], ["0", "1", "2"])
        self.assertEqual(accepted["hypotheses"][0]["explanation"], "revised")
        self.assertTrue(all("revisions" not in h and "introduced_reason" not in h for h in accepted["hypotheses"]))
        revised = investigation.state["hypotheses"][0]
        self.assertEqual(revised["revisions"][0]["previous"]["explanation"], "fixture")
        self.assertEqual([h["id"] for h in investigation.state["hypotheses"]], ["0", "1", "2"])
        invalid = investigation.query("query_logs", kind="metrics", purpose="Correct a typo", params=dict(typo="workers"))
        self.assertEqual(invalid["error"]["code"], "invalid_query")
        self.assertTrue(investigation.query("query_logs", kind="metrics", purpose="Corrected query",
                                            params=dict(names=["workers"]))["items"])
        investigation.conclude(plan())

    def test_three_query_tools_share_budget_and_memory_switch(self):
        investigation, job = self.investigation()
        investigation.update(issue=dict(problem="fixture", evidence=["Initial overview"]),
            hypotheses=[dict(id=str(i), explanation="fixture", predictions=dict(support="a", refute="b"))
                        for i in range(2)])
        investigation.options["max_queries"] = 5
        write_json(project_path(job["memory"]), [dict(iteration_id="past", issue="Worker stall")])
        investigation.query("query_logs", kind="metrics", purpose="Inspect worker growth", params=dict(names=["workers"]))
        reference = investigation.query("read_reference", path="source/components/tasks/economy.py", lines=[1, 10])
        self.assertTrue(reference["source"])
        invalid = investigation.query("read_reference", path="knowledge/mechanics/economy.json", key="MISSING")
        self.assertEqual(invalid["error"]["code"], "invalid_query")
        investigation.options["use_memory"] = False
        disabled = investigation.query("query_memory", text="worker")
        self.assertIn("Memory disabled", disabled["error"]["message"])
        investigation.options["use_memory"] = True
        memory = investigation.query("query_memory", text="WORKER")
        self.assertEqual(memory["items"][0]["iteration_id"], "past")
        self.assertEqual(memory["items"], read_json(project_path(job["memory"])))
        with patch.object(investigation.retriever, "read_reference", side_effect=AssertionError("Budget bypass")):
            exhausted = investigation.query("read_reference", path="source/entry.py")
        self.assertEqual(exhausted["error"]["code"], "query_budget_exhausted")
        queries = investigation.state["queries"]
        self.assertEqual([q["tool"] for q in queries],
                         ["query_logs", "read_reference", "read_reference", "query_memory", "query_memory"])
        self.assertTrue(all(set(q) == {"tool", "arguments", "result"} for q in queries))
        self.assertEqual(read_json(self.root / "investigation.json")["queries"], json.loads(json.dumps(queries)))

    def test_unresolved_diagnosis_skips_editor_and_evaluation(self):
        config = copy.deepcopy(self.config)
        run = self.make_run(config)
        run.root = self.root / "unresolved_rsi"
        run.versions = Versions(ROOT, run.root)
        run.directory = run.root / "iteration"
        diagnosis = dict(issue=dict(problem="fixture", evidence=["Ambiguous observations"]),
                         hypotheses=[], queries=[], conclusion=None, reason="Competing explanations remain")
        with patch("rsi.experiment.run.preflight"), patch("rsi.experiment.run.load_settings", return_value=config["model"]), \
                patch("rsi.experiment.iteration.run_batch", side_effect=fixture_batch), \
                patch.object(run, "model", return_value=diagnosis) as model:
            run.experiment.prepare()
            result = run.execute().report
        self.assertEqual(result["status"], "unresolved", result.get("reason"))
        self.assertEqual(result["reason"], diagnosis["reason"])
        self.assertEqual(model.call_count, 1)
        self.assertNotIn("validation", result)
        self.assertFalse((run.directory / "candidate").exists())
        memory = read_json(run.root / "memory" / (result["iteration_id"] + ".json"))
        self.assertIsNone(memory["conclusion"])
        self.assertNotIn("validation", memory)
        self.assertNotIn("evaluation", memory)

    def test_editor_handoff_keeps_only_assigned_task_and_references_full_evidence(self):
        hypothesis = dict(id="0", explanation="fixture", predictions=dict(support="a", refute="b"))
        diagnosis = dict(issue=dict(problem="worker issue", evidence=["Worker curve"]), conclusion=plan(),
                         hypotheses=[hypothesis, dict(hypothesis, id="1")],
                         queries=[dict(tool="query_logs", result=dict(items=["x" * 50000]))])
        evidence = self.root / "investigation.json"
        write_json(evidence, diagnosis)
        feedback = dict(passed=False, checks=[dict(name="syntax", passed=True),
                                              dict(name="import", passed=False, output="x" * 10000)])
        candidate = self.root / "candidate"
        job = editor_job(candidate, diagnosis, task(), feedback, evidence_path=evidence)
        payload = job["task"]
        self.assertEqual(payload["hypotheses"], [hypothesis])
        self.assertEqual(payload["task"], task())
        self.assertNotIn("queries", payload)
        self.assertNotIn("x" * 5000, json.dumps(job))
        self.assertEqual(read_json(candidate / payload["paths"]["evidence"]), diagnosis)
        checks = payload["validator_feedback"]["checks"]
        self.assertEqual(len(checks), 1)
        self.assertEqual(len(checks[0]["output"]), 4000)

    def test_memory_keeps_branch_paths_without_repeating_artifact_entries(self):
        branch = dict(task_id="T1", status="accept", reason="Improved over parent", validation_passed=True,
                      promoted=False, patch="patch_t01.diff", evaluation="evaluation_t01.json", promotion="promotion_t01.json")
        report = dict(iteration_id="fixture", parent=self.parent, status="reject", branches=[branch],
                      artifacts=dict(diagnosis="investigation.json", evaluation_t01=branch["evaluation"],
                                     promotion_t01=branch["promotion"], validation_t01_0="validation_t01_0.json"))
        original = copy.deepcopy(report)
        record(self.root, report)
        memory = read_json(self.root / "memory/fixture.json")
        self.assertEqual(memory["branches"], [branch])
        self.assertEqual(memory["artifacts"], dict(diagnosis="investigation.json", validation_t01_0="validation_t01_0.json"))
        self.assertNotIn("validation", memory)
        self.assertNotIn("evaluation", memory)
        self.assertEqual(report, original)

    def test_scope_and_snapshot_integrity(self):
        candidate = self.versions.candidate(self.parent, self.root / "candidate")
        (candidate / "bot/entry.py").write_text("# forbidden\n", encoding="utf-8")
        patch_path = self.root / "patch.diff"
        with patch("rsi.tools.validate.inventory", wraps=inventory) as scanned:
            result = validate(self.parent, candidate, patch_path=patch_path)
        self.assertEqual(scanned.call_count, 2)
        self.assertFalse(result["passed"])
        self.assertIn("bot/entry.py", result["checks"][0]["forbidden"])
        self.assertIn("+++ b/bot/entry.py", patch_path.read_text(encoding="utf-8"))
        self.assertIn("+# forbidden", patch_path.read_text(encoding="utf-8"))
        (Path(self.parent["source_root"]) / "bot/entry.py").write_text("# tampered\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.versions.verify(self.parent)


    def experiment_fixture(self):
        """Prepare a tiny offline experiment with real snapshots and import checks."""
        project = self.root / "chain_project"
        for name in ("bot", "knowledge"):
            shutil.copytree(ROOT / name, project / name, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "environment.json", project / "environment.json")
        config = copy.deepcopy(self.config)
        config["parallel_matches"] = 1
        return project, config

    @staticmethod
    def scripted_model(runtime, job, directory):
        """Return a diagnosis or append a scoped change without any model endpoint."""
        write_json(directory / "job.json", job)
        if job["role"] == "diagnosis":
            value = dict(issue=dict(problem="fixture", evidence=["fixture logs"]),
                         hypotheses=[], queries=[], conclusion=plan())
            write_json(directory / "investigation.json", value)
        else:
            path = project_path(job["workspace"]) / "bot/components/tasks/economy.py"
            path.write_text(path.read_text(encoding="utf-8") + "\n# Chain fixture.\n", encoding="utf-8")
            value = "edited"
        return dict(status="ok", value=value, usage=dict(tokens=100, cost=0))

    def test_static_failure_and_model_failure_do_not_run_candidates(self):
        project, config = self.experiment_fixture()
        config["max_repairs"] = 1
        with patch("rsi.experiment.run.preflight"), patch("rsi.experiment.run.load_settings", return_value=config["model"]), \
                patch("rsi.tools.llm.ExperimentRuntime.call_session", autospec=True, side_effect=self.scripted_model) as model, \
                patch("rsi.experiment.iteration.run_batch", side_effect=fixture_batch) as matches, \
                patch("rsi.experiment.iteration.validate", return_value=dict(passed=False, checks=[dict(name="import", passed=False)])):
            report = run_experiment(project, config)
        self.assertEqual(report["iterations"][0]["status"], "reject")
        self.assertEqual(matches.call_count, 1)
        self.assertEqual(model.call_count, 3)
        with patch("rsi.experiment.run.preflight"), patch("rsi.experiment.run.load_settings", return_value=config["model"]), \
                patch("rsi.tools.llm.ExperimentRuntime.call_session", autospec=True, return_value=dict(status="error", reason="Connection failed after retry", usage={})) as model, \
                patch("rsi.experiment.iteration.run_batch", side_effect=fixture_batch) as matches:
            failed = run_experiment(project, config, rounds=3)
        self.assertEqual(failed["status"], "error")
        self.assertEqual(len(failed["iterations"]), 3)
        self.assertEqual(model.call_count, 3)
        self.assertEqual(failed["consecutive_errors"], 3)
        self.assertEqual(matches.call_count, 1)
        self.assertIn("Connection failed", failed["reason"])

    def test_parent_selection_and_configuration_boundaries(self):
        project, config = self.experiment_fixture()
        source = self.root / "custom_parent"
        shutil.copytree(project / "bot", source / "bot")
        path = source / "bot/components/tasks/economy.py"
        path.write_text(path.read_text(encoding="utf-8") + "\n# Explicit parent.\n", encoding="utf-8")
        selected = Versions(project, project / "runtime/rsi").freeze(source)
        with patch("rsi.experiment.run.preflight"), patch("rsi.experiment.run.load_settings", return_value=config["model"]), \
                patch("rsi.experiment.iteration.run_batch", side_effect=fixture_batch), \
                patch.object(Run, "_diagnose", return_value=dict(conclusion=None, reason="fixture")):
            report = run_experiment(project, config, parent=source)
        self.assertEqual(report["initial_parent"], selected)
        self.assertEqual(report["current_parent"], selected)
        for mutate in (lambda c: c.update(max_matches=1),
                       lambda c: c.update(parallel_matches=0),
                       lambda c: c["model"]["editor"].update(max_output_tokens=0)):
            invalid = copy.deepcopy(config)
            mutate(invalid)
            with self.assertRaises(ValueError):
                validate_config(invalid)
        with self.assertRaises(ValueError):
            run_experiment(project, config, rounds=0)

    def test_parallel_matches_generates_the_same_five_seeds_for_both_versions(self):
        config = copy.deepcopy(self.config)
        self.assertEqual(config["parallel_matches"], 5)
        self.assertNotIn("batch", config)
        run = self.make_run(config, root=self.root / "five_matches")
        candidate = self.versions.candidate(self.parent, self.root / "seed_candidate")
        path = candidate / "bot/components/tasks/economy.py"
        path.write_text(path.read_text(encoding="utf-8") + "\n# Distinct seed fixture.\n", encoding="utf-8")
        child = self.versions.freeze(candidate)
        with patch("rsi.experiment.run.preflight"), patch("rsi.experiment.run.load_settings", return_value=config["model"]), \
                patch("rsi.experiment.iteration.run_batch", side_effect=fixture_batch) as matches, \
                patch.object(run, "_diagnose", return_value=dict(conclusion=plan())), \
                patch.object(run, "_edit_and_validate", return_value=(child, dict(passed=True, checks=[]))):
            run.experiment.prepare()
            report = run.execute(self.parent).report
        self.assertEqual(report["status"], "reject", report.get("reason"))
        self.assertEqual(matches.call_count, 2)
        for call in matches.call_args_list:
            self.assertEqual([condition["seed"] for condition in call.args[2]], [1, 2, 3, 4, 5])
            self.assertEqual(call.args[5], 5)
        self.assertEqual(report["costs"]["matches"], 10)

    def test_role_llm_overrides_keep_independent_session_limits_and_saved_settings(self):
        project, config = self.experiment_fixture()
        config["model"]["diagnosis"] = dict(model="openai/diagnosis", base_url="https://diagnosis.invalid/v1",
                                            reasoning_effort="low", max_output_tokens=2048)
        config["model"]["editor"] = dict(model="openai/editor", base_url="https://editor.invalid/v1",
            reasoning_effort="high", max_output_tokens=4096, max_total_tokens=2000000, max_cost=10)
        jobs = []

        def model(runtime, job, directory):
            jobs.append(job)
            result = self.scripted_model(runtime, job, directory)
            if job["role"] == "diagnosis":
                result["value"]["conclusion"] = dict(tasks=[task("T1"), task("T2")])
            result["usage"] = dict(tokens=job["model"]["max_total_tokens"] // 2,
                                   cost=job["model"]["max_cost"] / 2)
            return result

        with patch.dict(os.environ, LLM_MODEL="openai/environment", LLM_BASE_URL="https://environment.invalid/v1"), \
                patch("rsi.experiment.run.preflight") as preflight, patch("rsi.experiment.iteration.run_batch", side_effect=fixture_batch), \
                patch("rsi.tools.llm.ExperimentRuntime.call_session", autospec=True, side_effect=model), \
                patch("rsi.experiment.iteration.validate", return_value=dict(passed=True, checks=[])):
            report = run_experiment(project, config)
        self.assertEqual(report["iterations"][0]["status"], "reject")
        self.assertEqual(preflight.call_count, 2)
        self.assertEqual([job["model"]["model"] for job in jobs], ["openai/diagnosis", "openai/editor", "openai/editor"])
        for job in jobs:
            role = job["role"]
            for key in ("base_url", "reasoning_effort", "max_output_tokens"):
                self.assertEqual(job["model"][key], config["model"][role][key])
            self.assertNotIn("diagnosis", job["model"])
            self.assertNotIn("editor", job["model"])
            for key in ("max_total_tokens", "max_cost"):
                self.assertEqual(job["model"][key], config["model"][role].get(key, config["model"][key]))
        iteration = read_json(project_path(report["iterations"][0]["report"]))
        self.assertGreater(sum(session["tokens"] for session in iteration["costs"]["sessions"]), config["model"]["max_total_tokens"])

    def test_tool_requests_respect_token_budget(self):
        from openhands.sdk.llm import Message, TextContent
        investigation, _ = self.investigation()
        tools = investigation_tools(investigation)
        llm = BudgetedLLM(token_budget=self.config["model"]["max_total_tokens"], model="openai/gpt-4o",
                         api_key="offline", max_output_tokens=self.config["model"]["max_output_tokens"])
        messages = [Message(role="user", content=[TextContent(text="Investigate the evidence.")])]
        self.assertGreater(llm.get_token_count(messages, tools), 0)
        llm.check_request(messages, tools)
        llm.token_budget = 1
        with self.assertRaisesRegex(ValueError, "token budget"):
            llm.check_request(messages, tools)


    def test_llm_settings_reach_sdk(self):
        from rsi.tools.llm import _create_llm
        settings = dict(self.config["model"], model="openai/gpt-4o", api_key_env="RSI_TEST_API_KEY",
                        reasoning_effort="low", temperature=0.2, top_p=0.8, top_k=20, seed=43)
        with patch.dict(os.environ, RSI_TEST_API_KEY="offline"):
            llm = _create_llm(dict(role="diagnosis", model=settings))
        for field in ("reasoning_effort", "temperature", "top_p", "top_k", "seed", "max_output_tokens"):
            self.assertEqual(getattr(llm, field), settings[field])
        self.assertEqual(llm.timeout, settings["request_timeout"])
        self.assertEqual(llm.num_retries, 3)
        from litellm.exceptions import APIConnectionError
        llm.retry_min_wait = llm.retry_max_wait = 0
        for error, expected in ((APIConnectionError(message="offline", llm_provider="openai", model="fixture"), 3),
                                (ValueError("Token budget exhausted"), 1)):
            calls = []
            @llm._make_retry_decorator()
            def fail():
                calls.append(None)
                raise error
            with self.assertRaises(type(error)):
                fail()
            self.assertEqual(len(calls), expected)

    def test_worker_preserves_fatal_api_errors_through_exception_chains(self):
        from openhands.sdk.llm.exceptions import LLMAuthenticationError
        from openhands.sdk.testing import TestLLM
        _, job = self.investigation()
        job.update(role="diagnosis", model=self.config["model"], prompt="fixture")
        directory = self.root / "fatal"
        write_json(directory / "job.json", job)
        llm = TestLLM.from_messages(sdk_messages([("finish", dict(message="unused"))]))
        with patch.object(TestLLM, "completion", side_effect=LLMAuthenticationError("Invalid API credentials")) as call, \
                patch("openhands.sdk.agent.base.has_vision_profile_available", return_value=False), redirect_stdout(StringIO()):
            result = execute(directory / "job.json", llm_factory=lambda: llm)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["fatal"])
        call.assert_called_once()

    def test_unfinished_sdk_session_is_rejected(self):
        from openhands.sdk.testing import TestLLM
        _, job = self.investigation()
        job.update(role="diagnosis", model=dict(self.config["model"], max_iterations=1), prompt="Offline fixture")
        messages = sdk_messages([("select_issue", dict(issue=dict(problem="fixture", evidence=["Worker curve"]),
            hypotheses=[dict(id=str(i), explanation="fixture", predictions=dict(support="a", refute="b")) for i in range(2)]))])
        directory = self.root / "unfinished"
        write_json(directory / "job.json", job)
        with patch("openhands.sdk.agent.base.has_vision_profile_available", return_value=False), redirect_stdout(StringIO()):
            result = execute(directory / "job.json", llm_factory=lambda: TestLLM.from_messages(messages))
        self.assertEqual(result["status"], "error")
        self.assertIn("MaxIterationsReached", result["reason"])
        self.assertNotIn("value", result)

    def test_sdk_tools_with_scripted_transport(self):
        from openhands.sdk.testing import TestLLM
        investigation, job = self.investigation()
        query_params = dict(batch_id=self.batch["batch_id"], names=["workers"], frame=[0, 44])
        job.update(role="diagnosis", model=self.config["model"], prompt="Offline fixture: use the prescribed tool sequence.")
        calls = [
            ("select_issue", dict(issue=dict(problem="fixture", evidence=["Fixture overview, worker counts"]),
                hypotheses=[dict(id=str(i), explanation="fixture", predictions=dict(support="a", refute="b")) for i in range(2)])),
            ("query_logs", dict(kind="metrics", params=dict(batch_id=self.batch["batch_id"], typo="workers"), purpose="fixture")),
            ("query_logs", dict(kind="metrics", params=query_params, purpose="fixture")),
            ("read_reference", dict(path="source/components/tasks/economy.py", lines=[1, 20])),
            ("read_reference", dict(path="knowledge/mechanics/economy.json", key="WORKER_SATURATION")),
            ("query_memory", dict(text="workers")),
            ("conclude", dict(conclusion=plan())),
        ]
        # Reproduce the real failure: schema rejection emits ActionEvent(action=None).
        invalid_selection = copy.deepcopy(calls[0][1])
        invalid_selection["hypotheses"][0]["predictions"]["support"] = ["must be a string"]
        calls.insert(0, ("select_issue", invalid_selection))
        messages = sdk_messages(calls + [("finish", dict(message="Done."))])
        directory = self.root / "sdk"
        write_json(directory / "job.json", job)
        # Global SDK vision profiles are unrelated to this text-only fixture and live outside the sandbox.
        with patch("openhands.sdk.agent.base.has_vision_profile_available", return_value=False), redirect_stdout(StringIO()):
            result = execute(directory / "job.json", llm_factory=lambda: TestLLM.from_messages(messages))
        self.assertEqual(result["status"], "ok", result.get("reason"))
        self.assertEqual(result["value"]["conclusion"], plan())
        saved = read_json(directory / "response.json")
        self.assertEqual(saved["value"], result["value"])
        self.assertNotIn("queries", result["value"])
        self.assertEqual(result["value"]["query_count"], 5)
        investigation_result = read_json(project_path(saved["result_path"]))
        self.assertEqual(investigation_result["queries"][0]["result"]["error"]["code"], "invalid_query")


if __name__ == "__main__":
    unittest.main()
