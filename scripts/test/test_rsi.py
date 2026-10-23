"""Verify evidence boundaries and one offline iteration using scripted external boundaries."""
import copy
from contextlib import chdir, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shlex
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from rsi.analyzer import Analyzer
from rsi.diagnosis.investigate import Investigation
from rsi.diagnosis.session import investigation_tools
from rsi.evaluator import check_effects, effect_value, evaluate, freeze_plan, validate_effect
from rsi.editor import make_job as editor_job
from rsi.memory import record, search
from rsi.run import Run
from rsi.tools.llm import budgeted_llm_type, call_session, execute
from rsi.tools.matches import run_batch, run_match
from rsi.retriever import Retriever, freeze_context
from rsi.tools.validate import validate
from rsi.tools.versions import Versions, path_reference, project_path, read_json, write_json

ROOT = Path(__file__).resolve().parents[2]


def fixture_batch(project, version, conditions, directory, purpose, parallel=3):
    """Produce native-shaped Bot logs for a fixed batch without launching SC2."""
    directory = Path(directory)
    directory.mkdir(parents=True)
    batch = dict(batch_id=directory.name, version=version["version"], purpose=purpose, runs={})
    for index, condition in enumerate(conditions):
        run_id = directory.name + f"_{index}"
        path = directory / run_id
        path.mkdir()
        batch["runs"][run_id] = str(path)
        write_json(path / "manifest.json", dict(condition, run_id=run_id,
                   purpose=purpose, version=version["version"]))
        write_json(path / "result.json", dict(run_id=run_id, version=version["version"],
                   execution_status="normal", termination="time_limit",
                   outcome="draw", duration_loops=3360))
        records = [dict(event="game_started", frame=0)]
        for frame in range(0, 3361, 16):
            records.append(dict(event="state", frame=frame, minerals=600, vespene=0,
                supply_used=15, supply_cap=15, own=[dict(tag=1, type="SCV", build_progress=1,
                orders=[], health=45, position=[0, 0])], enemies=[]))
            if frame == 16:
                records.extend([
                    dict(event="module_call", frame=frame, call_id="c1", parent_id="",
                         module_id="Economy", module_path="bot.components.tasks.economy",
                         path="Economy", status="invoked_true"),
                    dict(event="action", frame=frame, call_id="c1", request_id="r1",
                         status="proposed", reason="", source="Economy"),
                    dict(event="action", frame=frame, call_id="c1", request_id="r1",
                         status="submitted", reason="local_queued", source="Economy")])
            if frame == 32:
                records.append(dict(event="action", frame=frame, call_id="c1", request_id="r1",
                                    status="observed", reason="unit_order", source="Economy"))
        records.append(dict(event="game_ended", frame=3360, result="Result.Tie"))
        (path / "trace.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    write_json(directory / "batch.json", batch)
    return batch


def effects():
    """Return explicit local improvement and regression checks for the fixture."""
    return [dict(kind="metric", name="workers", statistic="mean", time=[0, 150],
                 direction="increase", role="improvement"),
            dict(kind="metric", name="supply_block_ratio", statistic="mean", time=[0, 150],
                 direction="decrease", role="regression")]


def proposal():
    """Return the four-field editing handoff used by diagnosis and evaluation fixtures."""
    return dict(change="Fixture intervention", preserve="Keep economy behavior",
                verification="Inspect worker growth and check that supply and technology remain healthy.",
                code_targets=["bot/components/tasks/economy.py"])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        """Create isolated artifacts and one reusable native-log fixture."""
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = read_json(ROOT / "rsi/config.json")
        self.versions = Versions(ROOT, self.root / "runtime")
        self.parent = self.versions.freeze(ROOT)
        conditions = [dict(self.config["matches"], seed=1)]
        self.batch = fixture_batch(ROOT, self.parent, conditions, self.root / "explore", "explore")

    def test_facts_trace_and_holdout_boundary(self):
        """Missing telemetry stays missing and trace follows confirmations beyond the query window."""
        analyzer = Analyzer(ROOT)
        report = analyzer.add_batch(self.batch)
        retriever = Retriever(self.parent, ROOT, analyses=[report])
        run = next(iter(report["runs"].values()))
        self.assertEqual(run["coverage"]["integrity"], "complete")
        self.assertIn("mineral_income", run["coverage"]["missing_metrics"])
        self.assertEqual(run["actions"]["submitted_ratio"], 1)
        self.assertTrue(any(e["kind"] == "supply_block" for e in run["events"]))
        result = retriever.query_logs("trace", batch_id=self.batch["batch_id"], call_id="c1", time=[0, 1])
        self.assertTrue(any(r.get("status") == "observed" for r in result["items"]))
        self.assertTrue(result["source_refs"])
        with self.assertRaises(ValueError):
            analyzer.add_batch(dict(self.batch, purpose="holdout"))
        retriever.max_chars = 10
        result = retriever.query_logs("overview", batch_id=self.batch["batch_id"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["items"], [])

    def test_match_result_merge_preserves_worker_output(self):
        """Exercise the real runner's result merge without launching a worker."""
        conditions = dict(self.config["matches"], seed=1)
        for with_id in (True, False):
            directory = self.root / f"match_{with_id}"
            result = dict(execution_status="normal", outcome="win", version=self.parent["version"])
            if with_id:
                result["run_id"] = directory.name
            with self.subTest(with_id=with_id), patch("rsi.tools.matches.subprocess.Popen") as process:
                process.return_value.wait.side_effect = lambda **kwargs: write_json(directory / "result.json", result)
                merged = run_match(ROOT, self.parent, conditions, directory, "explore", "batch")
            self.assertEqual(merged, dict(result, run_id=directory.name, directory=path_reference(directory)))

    def test_batch_staggers_three_submissions(self):
        """Three configured matches launch with two delays and no extra games."""
        events = []
        conditions = [dict(self.config["matches"], seed=seed) for seed in (1, 2, 3)]
        with patch("rsi.tools.matches.ThreadPoolExecutor") as executor, \
                patch("rsi.tools.matches.time.sleep", side_effect=lambda seconds: events.append(("sleep", seconds))):
            pool = executor.return_value.__enter__.return_value
            def submit(function, project, version, condition, *args):
                events.append(("submit", condition["seed"]))
                from concurrent.futures import Future
                future = Future()
                future.set_result(dict(seed=condition["seed"]))
                return future
            pool.submit.side_effect = submit
            batch = run_batch(ROOT, self.parent, conditions, self.root / "staggered", "explore")
        self.assertEqual(events, [("submit", 1), ("sleep", 3), ("submit", 2), ("sleep", 3), ("submit", 3)])
        self.assertEqual(len(batch["results"]), 3)
        executor.assert_called_once_with(max_workers=3)

    def test_analyzer_summaries_and_large_evidence_are_retrievable(self):
        """Summaries cover every requested metric and long events retain usable references."""
        batch = Analyzer(ROOT).add_batch(self.batch)
        retriever = Retriever(self.parent, ROOT, analyses=[batch], max_items=60, max_chars=24000)
        result = retriever.query_logs("metrics", batch_id=self.batch["batch_id"], names=["workers", "minerals"])
        self.assertEqual([i["name"] for i in result["items"]], ["workers", "minerals"])
        self.assertFalse(result["truncated"])
        self.assertTrue(result["source_refs"])
        raw = retriever.query_logs("metrics", batch_id=self.batch["batch_id"], names=["workers"], detail="raw")
        self.assertTrue(raw["truncated"])
        self.assertIsNotNone(raw["next_offset"])
        run = next(iter(batch["runs"].values()))
        run["events"] = [dict(kind="production_idle", start=0, end=150, details=dict(facility="STARPORT"),
                              source_refs=[f"run:L{i}" for i in range(3000)])]
        event = retriever.query_logs("events", batch_id=self.batch["batch_id"])["items"][0]
        self.assertEqual(event["source_ref_count"], 3000)
        self.assertTrue(event["source_refs"])
        self.assertLess(len(event["source_refs"]), 3000)
        self.assertEqual(len(run["events"][0]["source_refs"]), 3000)
        run["events"][0]["details"]["large"] = "x" * 30000
        oversized = retriever.query_logs("events", batch_id=self.batch["batch_id"])
        self.assertTrue(oversized["items"][0]["detail_omitted"])
        self.assertEqual(oversized["items"][0]["kind"], "production_idle")
        trace = retriever.query_logs("trace", batch_id=self.batch["batch_id"], call_id="c1")
        self.assertFalse(any(r["event"] == "state" for r in trace["items"]))
        self.assertTrue(any(r["event"] == "state" for r in retriever.query_logs(
            "trace", batch_id=self.batch["batch_id"], call_id="c1", detail="raw")["items"]))

    def test_raw_pagination_preserves_results_and_stops_at_page_limit(self):
        """Small pages preserve full-window statistics and avoid consuming the whole result."""
        batch = Analyzer(ROOT).add_batch(self.batch)
        retriever = Retriever(self.parent, ROOT, analyses=[batch], max_items=1000, max_chars=1000000)
        run = next(iter(batch["runs"].values()))
        run["metrics"]["workers"][-2]["value"] = 100
        query = dict(names=["workers", "minerals"], detail="raw")
        complete = retriever.query_logs("metrics", batch_id=self.batch["batch_id"], **query)["items"]
        retriever.max_items = 3
        for max_chars in (1000000, 600):
            with self.subTest(max_chars=max_chars):
                retriever.max_chars = max_chars
                collected, offset = [], 0
                while True:
                    page = retriever.query_logs("metrics", batch_id=self.batch["batch_id"], offset=offset, **query)
                    collected.extend(page["items"])
                    if page["next_offset"] is None:
                        break
                    self.assertGreater(page["next_offset"], offset)
                    offset = page["next_offset"]
                self.assertEqual(collected, complete)
                self.assertEqual(collected[0]["statistics"]["max"], 100)

        def events():
            """Fail if pagination consumes beyond its single lookahead row."""
            for i in range(4):
                yield dict(kind="unit_created", start=i, end=i, details={}, source_refs=[f"run:L{i}"])
            raise AssertionError("Read past the requested page")

        retriever.max_chars = 24000
        run["events"] = events()
        page = retriever.query_logs("events", batch_id=self.batch["batch_id"])
        self.assertEqual(len(page["items"]), 3)
        self.assertTrue(page["truncated"])
        self.assertEqual(page["next_offset"], 3)

    def test_reference_index_does_not_read_source_contents(self):
        """Indexes enumerate filenames while actual reference queries still read contents."""
        context = self.root / "references"
        write_json(context / "memory.json", [])
        write_json(context / "knowledge/units.json", dict(SCV={}))
        (context / "api").mkdir()
        (context / "api/example.py").write_text("# reference\n", encoding="utf-8")
        retriever = Retriever(self.parent, context)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("Index read file contents")):
            index = retriever.index()
        self.assertIn("source/entry.py", index["source"])
        self.assertEqual(index["knowledge"], ["knowledge/units.json"])
        self.assertEqual(index["api"], ["api/example.py"])
        self.assertEqual(index["memory_count"], 0)
        self.assertTrue(retriever.read_reference("source/entry.py")["items"])

    def test_coordinator_releases_raw_logs_but_keeps_analysis(self):
        """Validation data does not accumulate in the coordinator; artifacts remain readable."""
        run = Run(ROOT, self.config)
        run.root = self.root / "coordinator"
        run.directory = run.root / "iteration"
        with patch("rsi.run.run_batch", side_effect=fixture_batch):
            explore, _ = run.batch(self.parent, [dict(seed=1)], "explore")
            validation, analysis = run.batch(self.parent, [dict(seed=2)], "validation")
        self.assertFalse(hasattr(run.analyzer, "records"))
        self.assertEqual(read_json(project_path(run.report["artifacts"][validation["batch_id"] + "_analysis"])), analysis)
        for path in validation["runs"].values():
            self.assertTrue((Path(path) / "trace.jsonl").is_file())
            self.assertTrue((Path(path) / "analysis.json").is_file())
        run.finish("analysis_only", "fixture")
        self.assertFalse(hasattr(run.analyzer, "records"))

    def test_reference_reads_use_sdk_and_json_keys(self):
        """Read text with the installed SDK while JSON keys and frozen roots stay explicit."""
        from openhands.tools.file_editor.editor import FileEditor
        context = self.root / "references"
        write_json(context / "memory.json", [])
        write_json(context / "knowledge/units.json", dict(SCV=dict(cost=dict(minerals=50))))
        path = context / "api/example.py"
        path.parent.mkdir()
        source = "".join(f"# line {i}\n" for i in range(1, 251))
        path.write_text(source, encoding="utf-8")
        retriever = Retriever(self.parent, context)
        with patch.object(FileEditor, "read_file", autospec=True, side_effect=FileEditor.read_file) as read:
            result = retriever.read_reference("api/example.py", lines=[2, 4])
        read.assert_called_once()
        item = result["items"][0]
        self.assertEqual((item["start_line"], item["end_line"]), (2, 4))
        self.assertEqual(item["source"], "# line 2\n# line 3\n# line 4")
        self.assertTrue(result["truncated"])
        capped = retriever.read_reference("api/example.py", lines=[10, 250])
        self.assertEqual(capped["items"][0]["end_line"], 129)
        tail = retriever.read_reference("api/example.py", lines=[240, 300])
        self.assertEqual(tail["items"][0]["end_line"], 250)
        self.assertFalse(tail["truncated"])
        self.assertEqual(path.read_text(encoding="utf-8"), source)
        self.assertEqual(retriever.read_reference("knowledge/units.json")["items"][0]["keys"], ["SCV"])
        entry = retriever.read_reference("knowledge/units.json", key="SCV.cost")["items"][0]
        self.assertEqual(entry["value"], dict(minerals=50))
        self.assertTrue(entry["source_ref"].endswith(":SCV.cost"))
        for arguments in (dict(path="api/../knowledge/units.json"), dict(path="entry.py"),
                          dict(path="api/example.py", key="SCV"), dict(path="api/example.py", lines=[0, 10]),
                          dict(path="knowledge/units.json", lines=[1, 10])):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                retriever.read_reference(**arguments)
        retriever.max_chars = 10
        limited = retriever.read_reference("api/example.py")
        self.assertEqual(limited["items"], [])
        self.assertTrue(limited["truncated"])

    def test_project_references_resolve_outside_project_cwd(self):
        """Persist portable paths and consume them from an unrelated working directory."""
        with TemporaryDirectory(dir=ROOT / "runtime/checks") as temporary:
            directory = Path(temporary)
            versions = Versions(ROOT, directory / "rsi")
            parent = versions.freeze(ROOT)
            self.assertFalse(Path(parent["source_root"]).is_absolute())
            self.assertNotIn("\\", parent["source_root"])
            batch = fixture_batch(ROOT, parent, [dict(self.config["matches"], seed=1)],
                                  directory / "explore", "explore")
            batch["runs"] = {run_id: path_reference(path) for run_id, path in batch["runs"].items()}
            run = Run(ROOT, self.config)
            run.directory = directory / "iteration"
            run.save("fixture", dict(ok=True))
            reference = run.report["artifacts"]["fixture"]
            self.assertFalse(Path(reference).is_absolute())
            with chdir(self.root):
                self.assertEqual(read_json(project_path(reference)), dict(ok=True))
                versions.verify(parent)
                # Existing absolute version references remain readable.
                versions.verify(dict(parent, source_root=str(project_path(parent["source_root"]))))
                candidate = versions.candidate(parent, directory / "candidate")
                self.assertTrue((candidate / "bot/entry.py").is_file())
                report = Analyzer(ROOT).add_batch(batch)
                log_path = next(iter(report["runs"].values()))["log_path"]
                self.assertFalse(Path(log_path).is_absolute())
                self.assertTrue(project_path(log_path).is_file())

    def test_iteration_summary_keeps_details_in_stage_artifacts(self):
        """Small disk summaries do not mutate the complete in-memory report or stage files."""
        run = Run(ROOT, self.config)
        run.directory = self.root / "summary"
        diagnosis = dict(conclusion=proposal(), queries=[dict(payload="x" * 10000)])
        validation = dict(passed=False, checks=[dict(name="import", passed=False, output="x" * 10000)])
        evaluation = dict(decision="inconclusive", reason="fixture", supplementary=[dict(payload="x" * 10000)])
        for name, value in (("diagnosis", diagnosis), ("validation_0", validation), ("evaluation", evaluation)):
            run.save(name, value)
        run.report.update(diagnosis=diagnosis, validation=validation, evaluation=evaluation)
        run.save("final", dict(status="inconclusive"))
        summary = read_json(run.directory / "iteration.json")
        self.assertEqual(summary["diagnosis"], dict(status="repair_ready"))
        self.assertEqual(summary["validation"], dict(passed=False, failed_checks=["import"]))
        self.assertEqual(summary["evaluation"], dict(decision="inconclusive", reason="fixture"))
        for name, value in (("diagnosis", diagnosis), ("validation_0", validation), ("evaluation", evaluation)):
            self.assertEqual(read_json(project_path(summary["artifacts"][name])), value)
        self.assertIs(run.report["diagnosis"], diagnosis)
        self.assertIs(run.report["validation"], validation)
        self.assertIs(run.report["evaluation"], evaluation)

    def test_missing_effects_are_not_zero_observations(self):
        """Keep missing metrics unknown while allowing zero occurrences of known events."""
        report = dict(coverage=dict(integrity="complete", duration_seconds=150),
                      metrics=dict(workers=[dict(time=10, value=1)]), events=[])
        batch = dict(runs={"run": report})
        effect = dict(kind="metric", name="missing", statistic="count", time=[0, 150],
                      direction="stable", role="regression")
        self.assertEqual(check_effects(batch, batch, [effect])[0]["result"], "missing")
        self.assertIsNone(effect_value(report, dict(effect, name="workers", time=[20, 30])))
        self.assertIsNone(effect_value(report, dict(effect, name="workers")))
        self.assertEqual(effect_value(report, dict(effect, name="workers", statistic="mean")), 1)
        unknown_event = dict(effect, kind="event", name="misspelled_event")
        with self.assertRaises(ValueError):
            validate_effect(unknown_event)
        self.assertIsNone(effect_value(report, unknown_event))
        known_event = dict(effect, kind="event", name="worker_loss_spike")
        validate_effect(known_event)
        self.assertEqual(effect_value(report, known_event), 0)
        self.assertEqual(check_effects(batch, batch, [known_event])[0]["result"], "met")

    def test_evaluation_requires_fixed_evidence_and_improvement(self):
        """Fixed promotion rules remain mandatory; qualitative advice is not a metric gate."""
        rules = dict(self.config["evaluation"], formal=True, matches=[dict(seed=i) for i in range(50)])
        plan = freeze_plan(rules, dict(conclusion=proposal()), effects())

        def batch(name, wins, workers):
            """Provide complete paired outcomes and directly observable local effects."""
            return dict(batch_id=name, runs={str(i): dict(conditions=dict(seed=i),
                environment=dict(sc2_build=98370),
                outcome=dict(execution_status="normal", termination="game_result", outcome="win" if i < wins else "loss"),
                coverage=dict(integrity="complete", duration_seconds=150, sample_ratio=1), events=[],
                metrics=dict(workers=[dict(time=0, value=workers, seconds=150)],
                             supply_block_ratio=[dict(time=0, value=0, seconds=150)])) for i in range(50)})

        parent, candidate = batch("parent", 0, 1), batch("candidate", 50, 2)
        self.assertEqual(evaluate(parent, candidate, plan)["decision"], "accept")
        self.assertEqual(evaluate(batch("parent", 50, 1), batch("candidate", 0, 2), plan)["decision"], "reject")
        self.assertEqual(plan["verification"], proposal()["verification"])
        unchanged = evaluate(parent, batch("candidate", 50, 1), plan)
        self.assertEqual(unchanged["decision"], "accept")
        self.assertEqual(unchanged["supplementary"][0]["result"], "not_met")
        for field, value, expected in (("outcome", dict(execution_status="bot_error"), "reject"),
                                       ("environment", dict(sc2_build=0), "inconclusive"),
                                       ("conditions", dict(seed=999), "inconclusive"),
                                       ("coverage", dict(integrity="partial", duration_seconds=150), "inconclusive"),
                                       ("metrics", {}, "accept")):
            with self.subTest(field=field):
                incomplete = copy.deepcopy(candidate)
                incomplete["runs"]["0"][field] = value
                self.assertEqual(evaluate(parent, incomplete, plan)["decision"], expected)
        for overrides in (dict(formal=False), dict(min_samples=51)):
            with self.subTest(rules=overrides):
                functional = freeze_plan(dict(rules, **overrides), dict(conclusion=proposal()), effects())
                self.assertEqual(evaluate(parent, candidate, functional)["decision"], "inconclusive")

    def test_smoke_infrastructure_failure_does_not_trigger_repairs(self):
        """Only a Bot failure enters repair; incomplete verification stays inconclusive."""
        for status, integrity, expected in (("infrastructure_error", "partial", "inconclusive"),
                                            ("unknown", "partial", "inconclusive"),
                                            ("normal", "partial", "inconclusive"),
                                            ("bot_error", "partial", "reject")):
            with self.subTest(status=status):
                config = copy.deepcopy(self.config)
                config.update(mode="iterate", max_repairs=0 if status == "bot_error" else 2)
                run = Run(ROOT, config, collect=True, enable_model=True)
                run.root = self.root / status
                run.directory = run.root / "iteration"
                exploration = dict(batch_id="explore", version=self.parent["version"], purpose="explore",
                    runs={"explore": dict(conditions=dict(map="map", race="Zerg", difficulty="Easy"),
                                          coverage=dict(sampled_seconds=1))})
                smoke = dict(runs={"smoke": dict(outcome=dict(execution_status=status, termination="worker_error"),
                                                 coverage=dict(integrity=integrity))})
                run.save("explore_analysis", exploration)
                diagnosis = dict(conclusion=proposal())
                with patch("rsi.run.preflight"), patch("rsi.run.load_settings", return_value=config["model"]), \
                        patch("rsi.run.freeze_context"), patch("rsi.run.diagnosis_job"), patch("rsi.run.editor_job"), \
                        patch("rsi.run.validate", return_value=dict(passed=True, checks=[])), \
                        patch.object(run.versions, "current", return_value=self.parent), \
                        patch.object(run.versions, "candidate", return_value=self.root / "candidate"), \
                        patch.object(run.versions, "diff", return_value=["bot/components/tasks/economy.py"]), \
                        patch.object(run.versions, "freeze", return_value=self.parent), \
                        patch("rsi.run.Retriever.query_logs", return_value={}), \
                        patch.object(run, "batch", side_effect=[(dict(batch_id="explore"), exploration), ({}, smoke)]), \
                        patch.object(run, "model", side_effect=[diagnosis, "edited"]) as model:
                    result = run.execute()
                self.assertEqual(result["status"], expected, result.get("reason"))
                self.assertEqual(model.call_count, 2)
                self.assertFalse(result["validation"]["passed"])
                self.assertTrue((run.directory / "validation_0.json").exists())
                self.assertFalse((run.root / "current_version.json").exists())

    def investigation(self, policy="active"):
        """Construct an investigation from frozen references and saved static analysis."""
        context = self.root / ("context_" + policy)
        freeze_context(ROOT, context, [])
        options = dict(self.config["diagnosis"], query_policy=policy)
        analysis_path = context / "analysis.json"
        write_json(analysis_path, Analyzer(context, self.config["analysis"]).add_batch(self.batch))
        job = dict(project=str(ROOT), context=str(context), parent=self.parent,
                   batches=[self.batch], diagnosis=options, analyses=[str(analysis_path)])
        return Investigation(job, self.root), job

    def test_diagnosis_reads_static_analysis_without_recomputing(self):
        """Diagnosis reuses saved reports; only raw-log queries load trace records."""
        _, job = self.investigation()
        analysis = read_json(job["analyses"][0])
        expected = copy.deepcopy(analysis)
        with patch.object(Analyzer, "analyze_run", side_effect=AssertionError("Repeated analysis")):
            investigation = Investigation(job, self.root)
            retriever = investigation.retriever
            with patch("rsi.retriever.read_records", side_effect=AssertionError("Unexpected raw-log read")):
                for kind, params in (("metrics", dict(names=["workers"])), ("events", {})):
                    self.assertTrue(retriever.query_logs(kind, batch_id=self.batch["batch_id"], **params)["items"])
            self.assertFalse(retriever.records)
            trace = retriever.query_logs("trace", batch_id=self.batch["batch_id"], call_id="c1")
            self.assertTrue(any(item.get("status") == "observed" for item in trace["items"]))
            self.assertEqual(set(retriever.records), set(self.batch["runs"]))
        self.assertEqual(read_json(job["analyses"][0]), expected)
        self.assertEqual(retriever.batches[analysis["batch_id"]], expected)
        with self.assertRaisesRegex(ValueError, "Holdout"):
            Retriever(self.parent, ROOT, analyses=[dict(analysis, purpose="holdout")])

    def test_reused_exploration_saves_queryable_analysis(self):
        """A reused batch gets a static report in the current iteration without new matches."""
        run = Run(ROOT, self.config)
        run.directory = self.root / "reused_iteration"
        with patch("rsi.run.run_batch", side_effect=AssertionError("Unexpected match")):
            report = run.execute([self.root / "explore/batch.json"])
        self.assertEqual(report["status"], "analysis_only", report.get("reason"))
        analysis = read_json(project_path(report["artifacts"][self.batch["batch_id"] + "_analysis"]))
        retriever = Retriever(self.parent, ROOT, analyses=[analysis])
        self.assertTrue(retriever.query_logs("metrics", batch_id=self.batch["batch_id"], names=["workers"])["items"])
        self.assertEqual(report["costs"]["matches"], 0)

    def test_investigation_accepts_natural_evidence_and_autonomous_queries(self):
        """Evidence descriptions and textual verification need no citation-ID bookkeeping."""
        investigation, _ = self.investigation()
        issue = dict(problem="Worker growth stalls", evidence=[
            "Explore run: worker counts stay flat while minerals accumulate around 60-150 seconds",
            "Economy task source and SCV reference entry are relevant"])
        hypotheses = [dict(id=str(i), explanation="fixture", predictions=dict(support="a", refute="b"),
                           code_targets=["bot/components/tasks/economy.py"]) for i in range(2)]
        investigation.update(issue=issue, hypotheses=hypotheses)
        self.assertTrue(investigation.query("query_logs", kind="metrics", purpose="Distinguish the explanations",
                                            params=dict(names=["workers"]))["items"])
        self.assertTrue(investigation.query("read_reference", path="source/components/tasks/economy.py")["items"])
        result = investigation.conclude(proposal())
        self.assertEqual(result["conclusion"], proposal())
        self.assertEqual(set(result["conclusion"]), {"change", "preserve", "verification", "code_targets"})
        self.assertEqual(investigation.state["issue"], issue)
        self.assertNotIn("updated", investigation.state["queries"][0])
        self.assertNotIn("support_refs", investigation.state["hypotheses"][0])
        with self.assertRaises(ValueError):
            investigation.query("query_logs", kind="overview", purpose="Already concluded")

    def test_fixed_pack_allows_issue_selection_but_no_adaptive_query(self):
        """Fixed evidence and the unresolved exit remain available with the simpler schema."""
        investigation, _ = self.investigation("fixed")
        investigation.fixed_pack()
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
        """Queries choose their own scope; revisions retain history without status enums."""
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
                            params=dict(names=["workers"], time=[0, 20], runs="all"))
        self.assertEqual(investigation.state["queries"][-1]["arguments"]["params"]["time"], [0, 20])
        investigation.update(issue=dict(issue, evidence=issue["evidence"] + ["Worker samples from 0-20 seconds"]))
        with self.assertRaisesRegex(ValueError, "fixed"):
            investigation.update(issue=dict(issue, problem="Different problem"))
        with self.assertRaisesRegex(ValueError, "reason"):
            investigation.update(hypotheses=[dict(hypotheses[0], explanation="revised")])
        investigation.update(hypotheses=[dict(hypotheses[0], explanation="revised"),
            dict(hypotheses[1], id="2", explanation="new upstream cause")], reason="Source suggests another cause")
        revised = investigation.state["hypotheses"][0]
        self.assertEqual(revised["revisions"][0]["previous"]["explanation"], "fixture")
        self.assertEqual([h["id"] for h in investigation.state["hypotheses"]], ["0", "1", "2"])
        invalid = investigation.query("query_logs", kind="metrics", purpose="Correct a typo", params=dict(typo="workers"))
        self.assertEqual(invalid["status"], "invalid_query")
        self.assertTrue(investigation.query("query_logs", kind="metrics", purpose="Corrected query",
                                            params=dict(names=["workers"]))["items"])
        investigation.conclude(proposal())

    def test_conclusion_checks_required_fields_and_code_locations(self):
        """Keep concrete editing boundaries while allowing free-form verification advice."""
        investigation, _ = self.investigation()
        investigation.update(issue=dict(problem="fixture", evidence=["Overview and source observations"]),
            hypotheses=[dict(id=str(i), explanation="fixture", predictions=dict(support="a", refute="b"))
                        for i in range(2)])
        for change in (dict(verification=""), dict(code_targets=[]),
                       dict(code_targets=["bot/core/actions.py"]),
                       dict(code_targets=["bot/components/tasks/missing.py"]),
                       dict(code_targets=["bot.components.tasks.economy::Missing.execute"])):
            with self.subTest(change=change), self.assertRaises(ValueError):
                investigation.conclude(dict(proposal(), **change))
            self.assertIsNone(investigation.state["conclusion"])
        conclusion = dict(proposal(), code_targets=["bot.components.tasks.economy::EconomyTask.execute"])
        self.assertEqual(investigation.conclude(conclusion)["conclusion"], conclusion)

    def test_query_budget_and_explicit_batch_selection(self):
        """Ambiguous and invalid queries consume budget without imposing issue scope fields."""
        investigation, _ = self.investigation()
        investigation.update(issue=dict(problem="fixture", evidence=["Multiple exploration sources"]),
            hypotheses=[dict(id=str(i), explanation="fixture", predictions=dict(support="a", refute="b"))
                        for i in range(2)])
        investigation.options["max_queries"] = 2
        investigation.job["batches"].append(dict(self.batch, batch_id="another_batch"))
        invalid = investigation.query("query_logs", kind="metrics", purpose="Choose a batch", params=dict(names=["workers"]))
        self.assertEqual(invalid["status"], "invalid_query")
        self.assertIn("batch_id", invalid["error"]["message"])
        valid = investigation.query("query_logs", kind="metrics", purpose="Explicit scope",
            params=dict(batch_id=self.batch["batch_id"], names=["workers"], time=[0, 20]))
        self.assertTrue(valid["items"])
        exhausted = investigation.query("read_reference", path="source/entry.py")
        self.assertEqual(exhausted["missing_reason"], "query_budget_exhausted")
        self.assertEqual(len(investigation.state["queries"]), 2)

    def test_three_query_tools_share_budget_and_memory_switch(self):
        """Reference and memory reads obey the same experiment controls as log queries."""
        investigation, job = self.investigation()
        investigation.update(issue=dict(problem="fixture", evidence=["Initial overview"]),
            hypotheses=[dict(id=str(i), explanation="fixture", predictions=dict(support="a", refute="b"))
                        for i in range(2)])
        investigation.options["max_queries"] = 5
        write_json(Path(job["context"]) / "memory.json", [dict(iteration_id="past", issue="Worker stall")])
        investigation.query("query_logs", kind="metrics", purpose="Inspect worker growth", params=dict(names=["workers"]))
        reference = investigation.query("read_reference", path="source/components/tasks/economy.py", lines=[1, 10])
        self.assertTrue(reference["items"])
        invalid = investigation.query("read_reference", path="knowledge/mechanics/economy.json", key="MISSING")
        self.assertEqual(invalid["status"], "invalid_query")
        investigation.options["use_memory"] = False
        disabled = investigation.query("query_memory", text="worker")
        self.assertIn("Memory disabled", disabled["error"]["message"])
        investigation.options["use_memory"] = True
        memory = investigation.query("query_memory", text="WORKER")
        self.assertEqual(memory["items"][0]["iteration_id"], "past")
        with patch.object(investigation.retriever, "read_reference", side_effect=AssertionError("Budget bypass")):
            exhausted = investigation.query("read_reference", path="source/entry.py")
        self.assertEqual(exhausted["missing_reason"], "query_budget_exhausted")
        queries = investigation.state["queries"]
        self.assertEqual([q["tool"] for q in queries],
                         ["query_logs", "read_reference", "read_reference", "query_memory", "query_memory"])
        self.assertTrue(all(set(q) == {"tool", "arguments", "result"} for q in queries))
        self.assertEqual(read_json(self.root / "investigation.json")["queries"], json.loads(json.dumps(queries)))

    def test_unresolved_diagnosis_skips_editor_and_evaluation(self):
        """An absent conclusion is a normal unresolved result with no candidate or promotion."""
        config = dict(self.config, mode="iterate")
        run = Run(ROOT, config, collect=True, enable_model=True)
        run.root = self.root / "unresolved_rsi"
        run.versions = Versions(ROOT, run.root)
        run.directory = run.root / "iteration"
        diagnosis = dict(issue=dict(problem="fixture", evidence=["Ambiguous observations"]),
                         hypotheses=[], queries=[], conclusion=None, reason="Competing explanations remain")
        with patch("rsi.run.preflight"), patch("rsi.run.load_settings", return_value=config["model"]), \
                patch("rsi.run.run_batch", side_effect=fixture_batch), \
                patch.object(run, "model", return_value=diagnosis) as model:
            result = run.execute()
        self.assertEqual(result["status"], "unresolved", result.get("reason"))
        self.assertEqual(result["reason"], diagnosis["reason"])
        self.assertEqual(model.call_count, 1)
        self.assertNotIn("validation", result)
        self.assertFalse((run.directory / "candidate").exists())
        memory = read_json(run.root / "memory" / (result["iteration_id"] + ".json"))
        self.assertIsNone(memory["proposal"])

    def test_editor_and_memory_keep_compact_context_and_full_query_records(self):
        """Editor can inspect all evidence without embedding query payloads in its prompt."""
        issue = dict(problem="worker issue", evidence=["Run worker curve and source observations"])
        conclusion = proposal()
        hypothesis = dict(id="0", explanation="fixture", predictions=dict(support="a", refute="b"),
                          code_targets=["bot/components/tasks/economy.py"])
        queries = [dict(tool="query_logs", arguments=dict(kind="metrics", params=dict(names=["workers"]), purpose="Inspect workers"),
                        result=dict(source_refs=["run:L1"], items=[dict(name="workers", source_refs=["run:L1"], payload="x"*50000)]))]
        diagnosis = dict(issue=issue, conclusion=conclusion,
                         hypotheses=[dict(hypothesis, revisions=[dict(reason="History stays out of prompt")])], queries=queries)
        feedback = dict(passed=False, checks=[dict(name="syntax", passed=True),
                                              dict(name="import", passed=False, output="x" * 10000)])
        write_json(self.root / "context/memory.json", [])
        write_json(self.root / "context/knowledge/fixture.json", dict(SCV={}))
        (self.root / "context/api").mkdir()
        job = editor_job(self.root / "candidate", diagnosis, feedback, context=self.root / "context")
        payload = json.loads(job["prompt"].split("\n")[-1])
        self.assertEqual(payload["issue"], issue)
        self.assertEqual(payload["hypotheses"], [hypothesis])
        self.assertEqual(payload["proposal"], conclusion)
        self.assertNotIn("x"*5000, job["prompt"])
        full_evidence = read_json((self.root / "candidate" / payload["evidence_path"]).resolve())
        self.assertEqual(full_evidence, queries)
        self.assertEqual(payload["reference_directory"], "../context")
        self.assertEqual(payload["references"]["knowledge"], ["knowledge/fixture.json"])
        self.assertEqual(project_path(job["context"]), self.root / "context")
        self.assertEqual(len(payload["validator_feedback"]["checks"]), 1)
        self.assertEqual(len(payload["validator_feedback"]["checks"][0]["output"]), 4000)
        report = dict(iteration_id="fixture", status="inconclusive", conditions=dict(race="Zerg"), diagnosis=diagnosis,
                      validation=feedback, changed_files=conclusion["code_targets"],
                      artifacts=dict(diagnosis="diagnosis.json", patch="patch.diff"))
        record(self.root, report)
        memory = search(self.root, dict(race="Zerg"))[0]
        self.assertNotIn("queries", json.dumps(memory))
        self.assertEqual(memory["issue"], issue)
        self.assertEqual(memory["proposal"], conclusion)
        self.assertEqual(memory["hypotheses"], [hypothesis])
        self.assertEqual(memory["artifacts"]["patch"], "patch.diff")

    def test_scope_and_snapshot_integrity(self):
        """Protected edits are rejected and tampered snapshots cannot be reused."""
        candidate = self.versions.candidate(self.parent, self.root / "candidate")
        (candidate / "bot/entry.py").write_text("# forbidden\n", encoding="utf-8")
        result = validate(self.parent, candidate)
        self.assertFalse(result["passed"])
        self.assertIn("bot/entry.py", result["checks"][0]["forbidden"])
        (Path(self.parent["source_root"]) / "bot/entry.py").write_text("# tampered\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.versions.verify(self.parent)

    def test_offline_iteration_retains_parent_and_all_stages(self):
        """Exercise editing, real import validation and fixed comparison with scripted model/matches."""
        project = self.root / "project"
        for name in ("bot", "knowledge"):
            shutil.copytree(ROOT / name, project / name, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "environment.json", project / "environment.json")
        config = copy.deepcopy(self.config)
        config["mode"] = "iterate"
        config["explore"] = [{"seed": 1}]
        config["evaluation"]["matches"] = [{"seed": 2}]

        def model(project, job, directory):
            """Return one fixed diagnosis or apply a scoped comment-only candidate patch."""
            if job["role"] == "diagnosis":
                self.assertFalse(hasattr(run.analyzer, "records"))
                investigation = Investigation(job, Path(job["context"]))
                self.assertTrue(investigation.retriever.query_logs("overview", batch_id=job["batches"][0]["batch_id"])["items"])
                value = dict(issue=dict(problem="fixture", evidence=["Worker counts in fixture logs"]),
                             hypotheses=[], queries=[], conclusion=proposal())
            else:
                path = Path(job["workspace"]) / "bot/components/tasks/economy.py"
                path.write_text(path.read_text(encoding="utf-8") + "\n# Offline pipeline fixture.\n", encoding="utf-8")
                value = "edited"
            return dict(status="ok", value=value, usage=dict(tokens=100, cost=None))

        with patch("rsi.run.preflight"), patch("rsi.run.call_session", side_effect=model), \
                patch("rsi.run.run_batch", side_effect=fixture_batch):
            run = Run(project, config, collect=True, enable_model=True)
            report = run.execute()
        self.assertEqual(report["status"], "inconclusive", report.get("reason"))
        self.assertTrue(report["validation"]["passed"])
        self.assertEqual(report["costs"]["matches"], 4)
        self.assertIn("evaluation_plan", report["artifacts"])
        self.assertFalse((project / "runtime/rsi/current_version.json").exists())
        self.assertTrue((project / "runtime/rsi/memory" / (report["iteration_id"] + ".json")).exists())
        analysis = Analyzer(project)
        p = analysis.add_batch(self.batch)
        plan = freeze_plan(config["evaluation"], dict(conclusion=proposal()))
        self.assertEqual(evaluate(p, p, plan)["decision"], "inconclusive")

    def test_diagnosis_tool_token_budget(self):
        """Count real SDK tool schemas offline before applying the request budget."""
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        from openhands.sdk.llm import Message, TextContent
        from pydantic import ValidationError
        investigation, _ = self.investigation()
        tools = investigation_tools(investigation)
        self.assertEqual([tool.name for tool in tools],
                         ["select_issue", "query_logs", "read_reference", "query_memory", "conclude"])
        settings = dict(self.config["model"])
        llm = budgeted_llm_type(settings)(model="openai/gpt-4o", api_key="offline",
                                        max_output_tokens=settings["max_output_tokens"])
        messages = [Message(role="user", content=[TextContent(text="Investigate the evidence.")])]
        self.assertGreater(llm.get_token_count(messages, tools), 0)
        llm.check_request(messages, tools)
        settings["max_total_tokens"] = 1
        with self.assertRaisesRegex(ValueError, "token budget"):
            llm.check_request(messages, tools)
        issue_type = tools[0].action_type.model_fields["issue"].annotation.__args__[0]
        hypothesis_type = tools[0].action_type.model_fields["hypotheses"].annotation.__args__[0]
        conclusion_type = tools[-1].action_type.model_fields["conclusion"].annotation.__args__[0]
        self.assertEqual(set(issue_type.model_fields), {"problem", "evidence"})
        self.assertEqual(set(hypothesis_type.model_fields), {"id", "explanation", "predictions", "code_targets"})
        self.assertEqual(set(conclusion_type.model_fields), {"change", "preserve", "verification", "code_targets"})
        issue = dict(problem="fixture", evidence=["Run one, worker curve near 60-150 seconds", "SCV knowledge entry"])
        self.assertEqual(issue_type(**issue).evidence, issue["evidence"])
        with self.assertRaises(ValidationError):
            issue_type(**issue, window=[0, 1])
        with self.assertRaises(ValidationError):
            conclusion_type(**proposal(), expected_effect=[])
        self.assertEqual(set(tools[2].action_type.model_fields), {"path", "lines", "key"})
        self.assertEqual(set(tools[3].action_type.model_fields), {"text"})
        self.assertNotIn("updates", tools[1].action_type.to_mcp_schema()["properties"])
        self.assertNotIn("updates", tools[-1].action_type.to_mcp_schema()["properties"])


    def test_model_worker_console_relay_keeps_stderr_in_file(self):
        """Forward worker progress without leaking SDK stderr into the console."""
        import subprocess
        import sys
        popen = subprocess.Popen
        directory = self.root / "relay"
        code = ("import json,sys; from pathlib import Path; "
                "print('[diagnosis] evidence progress', flush=True); "
                "print('SDK internal noise', file=sys.stderr); "
                "Path(sys.argv[1]).write_text(json.dumps(dict(status='ok')), encoding='utf-8')")

        def worker(args, **kwargs):
            """Use a real local worker without calling any model endpoint."""
            return popen([sys.executable, "-B", "-c", code, str(directory / "response.json")], **kwargs)

        output = StringIO()
        with patch("rsi.tools.llm.subprocess.Popen", side_effect=worker), redirect_stdout(output):
            result = call_session(ROOT, dict(model=dict(wall_timeout=10)), directory)
        self.assertEqual(result["status"], "ok")
        self.assertIn("[diagnosis] evidence progress", output.getvalue())
        self.assertNotIn("SDK internal noise", output.getvalue())
        self.assertIn("SDK internal noise", (directory / "stderr.txt").read_text(encoding="utf-8"))

    def test_sdk_tools_with_scripted_transport(self):
        """Use the installed SDK with TestLLM; no network/model endpoint is called."""
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
        from openhands.sdk.testing import TestLLM
        from openhands.sdk.llm import Message, MessageToolCall, TextContent
        investigation, job = self.investigation()
        definitions = {tool.name: tool for tool in investigation_tools(investigation)}
        schema = definitions["query_logs"].action_type.to_mcp_schema()
        self.assertIn("kind", schema["properties"])
        self.assertIn("kind", schema["required"])
        self.assertNotIn("channel", schema["properties"])
        self.assertNotIn("distinguishes", schema["properties"])
        self.assertNotIn("params", definitions["select_issue"].action_type.to_mcp_schema()["properties"])
        query_params = dict(batch_id=self.batch["batch_id"], names=["workers"], time=[0, 2])
        job.update(role="diagnosis", model=self.config["model"], prompt="Offline fixture: use the prescribed tool sequence.")
        calls = [
            ("select_issue", dict(issue=dict(problem="fixture", evidence=["Fixture overview, worker counts"]),
                hypotheses=[dict(id=str(i), explanation="fixture", predictions=dict(support="a", refute="b")) for i in range(2)])),
            ("query_logs", dict(kind="metrics", params=dict(batch_id=self.batch["batch_id"], typo="workers"), purpose="fixture")),
            ("query_logs", dict(kind="metrics", params=query_params, purpose="fixture")),
            ("read_reference", dict(path="source/components/tasks/economy.py", lines=[1, 20])),
            ("read_reference", dict(path="knowledge/mechanics/economy.json", key="WORKER_SATURATION")),
            ("query_memory", dict(text="workers")),
            ("conclude", dict(conclusion=proposal())),
        ]
        # Reproduce the real failure: schema rejection emits ActionEvent(action=None).
        invalid_selection = copy.deepcopy(calls[0][1])
        invalid_selection["hypotheses"][0]["predictions"]["support"] = ["must be a string"]
        calls.insert(0, ("select_issue", invalid_selection))
        messages = [Message(role="assistant", content=[TextContent(text="Offline fixture")],
            tool_calls=[MessageToolCall(id=str(i), name=name, arguments=json.dumps(params), origin="completion")])
            for i, (name, params) in enumerate(calls)]
        messages.append(Message(role="assistant", content=[TextContent(text="Done.")],
            tool_calls=[MessageToolCall(id="finish", name="finish", arguments=json.dumps(dict(message="Done.")), origin="completion")]))
        directory = self.root / "sdk"
        write_json(directory / "job.json", job)
        # Global SDK vision profiles are unrelated to this text-only fixture and live outside the sandbox.
        output = StringIO()
        with patch("openhands.sdk.agent.base.has_vision_profile_available", return_value=False), redirect_stdout(output):
            result = execute(directory / "job.json", llm_factory=lambda: TestLLM.from_messages(messages))
        self.assertEqual(result["status"], "ok", result.get("reason"))
        self.assertEqual(result["value"]["conclusion"], proposal())
        self.assertEqual(len(result["value"]["queries"]), 5)
        self.assertEqual(result["value"]["queries"][0]["result"]["status"], "invalid_query")
        self.assertIn("Issue: fixture", output.getvalue())
        self.assertIn("Hypothesis 0: fixture", output.getvalue())
        self.assertIn("Query metrics", output.getvalue())
        self.assertIn("Read source/components/tasks/economy.py", output.getvalue())
        self.assertIn("Query memory: workers", output.getvalue())
        self.assertEqual(result["value"]["queries"][3]["result"]["items"][0]["key"], "WORKER_SATURATION")
        self.assertIn("Tool rejected:", output.getvalue())
        self.assertIn("Conclusion validated: Fixture intervention", output.getvalue())
        self.assertIn("Tool rejected (select_issue):", output.getvalue())
        for failure, target in (("render", "rsi.console._tool_progress"), ("write", "rsi.console.print")):
            with self.subTest(failure=failure):
                failed_directory = self.root / ("sdk_display_" + failure)
                write_json(failed_directory / "job.json", job)
                with patch("openhands.sdk.agent.base.has_vision_profile_available", return_value=False), \
                        patch(target, side_effect=RuntimeError("Display failure"), create=True):
                    recovered = execute(failed_directory / "job.json", llm_factory=lambda: TestLLM.from_messages(messages))
                self.assertEqual(recovered["status"], "ok", recovered.get("reason"))
                self.assertEqual(recovered["value"]["conclusion"], proposal())
                self.assertTrue((failed_directory / "usage.json").is_file())
                self.assertTrue((failed_directory / "investigation.json").is_file())

    def test_sdk_unresolved_preserves_reason_and_handles_missing_conclusion(self):
        """Exercise explicit abstention and normal session end without a proposal."""
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
        from openhands.sdk.testing import TestLLM
        from openhands.sdk.llm import Message, MessageToolCall, TextContent
        _, job = self.investigation()
        job.update(role="diagnosis", model=self.config["model"], prompt="Offline unresolved fixture.")
        for explicit in (True, False):
            with self.subTest(explicit=explicit):
                calls = [("conclude", dict(reason="Evidence cannot distinguish the causes"))] if explicit else []
                calls.append(("finish", dict(message="Done.")))
                messages = [Message(role="assistant", content=[TextContent(text="Offline fixture")],
                    tool_calls=[MessageToolCall(id=str(i), name=name, arguments=json.dumps(params), origin="completion")])
                    for i, (name, params) in enumerate(calls)]
                directory = self.root / ("sdk_unresolved_" + str(explicit))
                write_json(directory / "job.json", job)
                with patch("openhands.sdk.agent.base.has_vision_profile_available", return_value=False), redirect_stdout(StringIO()):
                    result = execute(directory / "job.json", llm_factory=lambda: TestLLM.from_messages(messages))
                self.assertEqual(result["status"], "ok", result.get("reason"))
                self.assertIsNone(result["value"]["conclusion"])
                expected = "Evidence cannot distinguish the causes" if explicit else "Session ended without an actionable conclusion"
                self.assertEqual(result["value"]["reason"], expected)
                self.assertEqual(read_json(directory / "investigation.json"), result["value"])

    def test_sdk_editor_can_edit_and_run_candidate_tests(self):
        """Use real SDK editing and terminal tools with a scripted, offline model."""
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
        import sys
        from openhands.sdk.testing import TestLLM
        from openhands.sdk.llm import Message, MessageToolCall, TextContent
        from openhands.tools.file_editor import FileEditorTool
        from openhands.tools.terminal import TerminalExecutor, TerminalTool

        candidate = self.versions.candidate(self.parent, self.root / "editor_candidate")
        context = self.root / "editor_context"
        freeze_context(ROOT, context, [])
        api_source = (context / "api/bot_ai.py").read_bytes()
        path = candidate / "bot/components/tasks/economy.py"
        quote = (lambda value: "'" + str(value).replace("'", "''") + "'") if sys.platform == "win32" else shlex.quote
        code = ("from pathlib import Path; import bot, sys; "
                "assert Path(bot.__file__).resolve().parent == Path.cwd() / 'bot'; "
                f"assert Path(sys.executable).resolve() == Path({sys.executable!r}).resolve(); "
                "print('CANDIDATE_IMPORT_OK')")
        test_command = ("python -B -m unittest discover -s " + quote(str(ROOT / "scripts/test"))
                        + " -p test_bot.py -v")
        calls = [
            ("read_reference", dict(path="api/../../outside.py")),
            ("read_reference", dict(path="api/bot_ai.py", lines=[1, 12])),
            ("read_reference", dict(path="knowledge/mechanics/economy.json", key="WORKER_SATURATION")),
            (TerminalTool.name, dict(command="python -B -c " + quote(code), timeout=30)),
            (FileEditorTool.name, dict(command="str_replace", path=str(path), old_str="class EconomyTask:",
                                      new_str="class EconomyTask\n    # Offline SDK editor fixture.")),
            (TerminalTool.name, dict(command=test_command, timeout=60)),
            (FileEditorTool.name, dict(command="str_replace", path=str(path), old_str="class EconomyTask\n",
                                      new_str="class EconomyTask:\n")),
            (TerminalTool.name, dict(command=test_command, timeout=60)),
        ]
        messages = [Message(role="assistant", content=[TextContent(text="Offline editor fixture")],
            tool_calls=[MessageToolCall(id=str(i), name=name, arguments=json.dumps(params), origin="completion")])
            for i, (name, params) in enumerate(calls)]
        messages.append(Message(role="assistant", content=[TextContent(text="Done.")],
            tool_calls=[MessageToolCall(id="finish", name="finish", arguments=json.dumps(dict(message="Done.")), origin="completion")]))
        job = editor_job(candidate, dict(conclusion=proposal(), queries=[]), context=context)
        payload = json.loads(job["prompt"].split("\n")[-1])
        self.assertIn("api/bot_ai.py", payload["references"]["api"])
        self.assertIn("source/components/tasks/economy.py", payload["references"]["source"])
        job["model"] = self.config["model"]
        directory = self.root / "sdk_editor"
        write_json(directory / "job.json", job)
        observations = []
        reference_results = []
        read_reference = Retriever.read_reference
        execute_terminal = TerminalExecutor.__call__

        def observe_reference(retriever, **arguments):
            """Capture actual reference results returned through the shared SDK tool."""
            value = read_reference(retriever, **arguments)
            reference_results.append(value)
            return value

        def observe_terminal(executor, action, conversation=None):
            """Capture results while executing the official terminal unchanged."""
            observation = execute_terminal(executor, action, conversation)
            observations.append(observation)
            return observation

        output = StringIO()
        with patch("openhands.sdk.agent.base.has_vision_profile_available", return_value=False), redirect_stdout(output), \
                patch.object(TerminalExecutor, "__call__", new=observe_terminal), \
                patch.object(Retriever, "read_reference", new=observe_reference):
            result = execute(directory / "job.json", llm_factory=lambda: TestLLM.from_messages(messages))
        self.assertEqual(result["status"], "ok", result.get("reason"))
        self.assertEqual(len(observations), 3)
        self.assertEqual(observations[0].exit_code, 0, observations[0].text)
        self.assertIn("CANDIDATE_IMPORT_OK", observations[0].text)
        self.assertNotEqual(observations[1].exit_code, 0)
        self.assertIn("SyntaxError", observations[1].text)
        self.assertEqual(observations[2].exit_code, 0, observations[2].text)
        self.assertIn("OK", observations[2].text)
        self.assertIn("# Offline SDK editor fixture.", path.read_text(encoding="utf-8"))
        self.assertIn("File str_replace: bot/components/tasks/economy.py", output.getvalue())
        self.assertIn("Command:", output.getvalue())
        self.assertIn("Command result: exit_code=0", output.getvalue())
        self.assertIn("Read api/bot_ai.py", output.getvalue())
        self.assertIn("Tool rejected: Reference path exceeds scope", output.getvalue())
        self.assertEqual(len(reference_results), 2)
        self.assertEqual(reference_results[0]["items"][0]["source"],
                         "\n".join(api_source.decode("utf-8").splitlines()[:12]))
        self.assertEqual(reference_results[1]["items"][0]["key"], "WORKER_SATURATION")
        self.assertEqual((context / "api/bot_ai.py").read_bytes(), api_source)
        self.assertTrue(validate(self.parent, candidate)["passed"])


if __name__ == "__main__":
    unittest.main()
