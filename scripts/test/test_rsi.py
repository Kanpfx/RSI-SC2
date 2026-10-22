"""Verify evidence boundaries and one offline iteration using scripted external boundaries."""
import copy
import json
import os
from pathlib import Path
import shlex
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from rsi.analyzer import Analyzer
from rsi.diagnosis.investigate import Investigation, validate_effect
from rsi.evaluator import check_effects, effect_value, evaluate, freeze_plan
from rsi.editor import make_job as editor_job
from rsi.memory import record, search
from rsi.run import Run
from rsi.tools.llm import execute, investigation_tools
from rsi.tools.matches import run_match
from rsi.tools.retriever import Retriever, freeze_context
from rsi.tools.validate import validate
from rsi.tools.versions import Versions, read_json, write_json

ROOT = Path(__file__).resolve().parents[2]


def fixture_batch(project, version, conditions, directory, purpose, parallel=5):
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
        run = next(iter(report["runs"].values()))
        self.assertEqual(run["coverage"]["integrity"], "complete")
        self.assertIn("mineral_income", run["coverage"]["missing_metrics"])
        self.assertEqual(run["actions"]["submitted_ratio"], 1)
        self.assertTrue(any(e["kind"] == "supply_block" for e in run["events"]))
        result = analyzer.query("trace", self.batch["batch_id"], call_id="c1", time=[0, 1])
        self.assertTrue(any(r.get("status") == "observed" for r in result["items"]))
        self.assertTrue(result["source_refs"])
        with self.assertRaises(ValueError):
            analyzer.add_batch(dict(self.batch, purpose="holdout"))
        analyzer.max_chars = 10
        result = analyzer.query("overview", self.batch["batch_id"])
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
            self.assertEqual(merged, dict(result, run_id=directory.name, directory=str(directory)))

    def test_analyzer_summaries_and_large_evidence_are_retrievable(self):
        """Summaries cover every requested metric and long events retain usable references."""
        analyzer = Analyzer(ROOT, max_items=60, max_chars=24000)
        batch = analyzer.add_batch(self.batch)
        result = analyzer.query("metrics", self.batch["batch_id"], names=["workers", "minerals"])
        self.assertEqual([i["name"] for i in result["items"]], ["workers", "minerals"])
        self.assertFalse(result["truncated"])
        self.assertTrue(result["source_refs"])
        raw = analyzer.query("metrics", self.batch["batch_id"], names=["workers"], detail="raw")
        self.assertTrue(raw["truncated"])
        self.assertIsNotNone(raw["next_offset"])
        run = next(iter(batch["runs"].values()))
        run["events"] = [dict(kind="production_idle", start=0, end=150, details=dict(facility="STARPORT"),
                              source_refs=[f"run:L{i}" for i in range(3000)])]
        event = analyzer.query("events", self.batch["batch_id"])["items"][0]
        self.assertEqual(event["source_ref_count"], 3000)
        self.assertTrue(event["source_refs"])
        self.assertLess(len(event["source_refs"]), 3000)
        self.assertEqual(len(run["events"][0]["source_refs"]), 3000)
        run["events"][0]["details"]["large"] = "x" * 30000
        oversized = analyzer.query("events", self.batch["batch_id"])
        self.assertTrue(oversized["items"][0]["detail_omitted"])
        self.assertEqual(oversized["items"][0]["kind"], "production_idle")
        trace = analyzer.query("trace", self.batch["batch_id"], call_id="c1")
        self.assertFalse(any(r["event"] == "state" for r in trace["items"]))
        self.assertTrue(any(r["event"] == "state" for r in analyzer.query(
            "trace", self.batch["batch_id"], call_id="c1", detail="raw")["items"]))

    def test_raw_pagination_preserves_results_and_stops_at_page_limit(self):
        """Small pages preserve full-window statistics and avoid consuming the whole result."""
        analyzer = Analyzer(ROOT, max_items=1000, max_chars=1000000)
        batch = analyzer.add_batch(self.batch)
        run = next(iter(batch["runs"].values()))
        run["metrics"]["workers"][-2]["value"] = 100
        query = dict(names=["workers", "minerals"], detail="raw")
        complete = analyzer.query("metrics", self.batch["batch_id"], **query)["items"]
        analyzer.max_items = 3
        for max_chars in (1000000, 600):
            with self.subTest(max_chars=max_chars):
                analyzer.max_chars = max_chars
                collected, offset = [], 0
                while True:
                    page = analyzer.query("metrics", self.batch["batch_id"], offset=offset, **query)
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

        analyzer.max_chars = 24000
        run["events"] = events()
        page = analyzer.query("events", self.batch["batch_id"])
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
        self.assertIn("entry.py", index["source"])
        self.assertEqual(index["knowledge"], ["units.json"])
        self.assertEqual(index["api"], ["example.py"])
        self.assertEqual(index["memory_count"], 0)
        self.assertTrue(retriever.query("source", path="entry.py")["items"])

    def test_coordinator_releases_raw_logs_but_keeps_analysis(self):
        """Validation data does not accumulate in the coordinator; artifacts remain readable."""
        run = Run(ROOT, self.config)
        run.root = self.root / "coordinator"
        run.directory = run.root / "iteration"
        with patch("rsi.run.run_batch", side_effect=fixture_batch):
            explore, _ = run.batch(self.parent, [dict(seed=1)], "explore")
            validation, analysis = run.batch(self.parent, [dict(seed=2)], "validation")
        self.assertEqual(set(run.analyzer.records), set(explore["runs"]))
        self.assertEqual(run.analyzer.batches[validation["batch_id"]], analysis)
        for path in validation["runs"].values():
            self.assertTrue((Path(path) / "trace.jsonl").is_file())
            self.assertTrue((Path(path) / "analysis.json").is_file())
        run.finish("analysis_only", "fixture")
        self.assertFalse(run.analyzer.records)

    def test_iteration_summary_keeps_details_in_stage_artifacts(self):
        """Small disk summaries do not mutate the complete in-memory report or stage files."""
        run = Run(ROOT, self.config)
        run.directory = self.root / "summary"
        diagnosis = dict(conclusion=dict(status="repair_ready"), queries=[dict(payload="x" * 10000)])
        validation = dict(passed=False, checks=[dict(name="import", passed=False, output="x" * 10000)])
        evaluation = dict(decision="inconclusive", reason="fixture", expected_effects=[dict(payload="x" * 10000)])
        for name, value in (("diagnosis", diagnosis), ("validation_0", validation), ("evaluation", evaluation)):
            run.save(name, value)
        run.report.update(diagnosis=diagnosis, validation=validation, evaluation=evaluation)
        run.save("final", dict(status="inconclusive"))
        summary = read_json(run.directory / "iteration.json")
        self.assertEqual(summary["diagnosis"], dict(status="repair_ready"))
        self.assertEqual(summary["validation"], dict(passed=False, failed_checks=["import"]))
        self.assertEqual(summary["evaluation"], dict(decision="inconclusive", reason="fixture"))
        for name, value in (("diagnosis", diagnosis), ("validation_0", validation), ("evaluation", evaluation)):
            self.assertEqual(read_json(summary["artifacts"][name]), value)
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
        """Promotion requires formal paired evidence and both overall and local gains."""
        rules = dict(self.config["evaluation"], formal=True, matches=[dict(seed=i) for i in range(50)])
        plan = freeze_plan(rules, dict(conclusion=dict(expected_effect=effects())))

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
        self.assertEqual(evaluate(parent, batch("candidate", 50, 1), plan)["decision"], "inconclusive")
        for field, value, expected in (("outcome", dict(execution_status="bot_error"), "reject"),
                                       ("environment", dict(sc2_build=0), "inconclusive"),
                                       ("conditions", dict(seed=999), "inconclusive"),
                                       ("coverage", dict(integrity="partial", duration_seconds=150), "inconclusive"),
                                       ("metrics", {}, "inconclusive")):
            with self.subTest(field=field):
                incomplete = copy.deepcopy(candidate)
                incomplete["runs"]["0"][field] = value
                self.assertEqual(evaluate(parent, incomplete, plan)["decision"], expected)
        for overrides in (dict(formal=False), dict(min_samples=51)):
            with self.subTest(rules=overrides):
                functional = freeze_plan(dict(rules, **overrides), dict(conclusion=dict(expected_effect=effects())))
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
                exploration = dict(runs={"explore": dict(conditions=dict(map="map", race="Zerg", difficulty="Easy"),
                                                         coverage=dict(sampled_seconds=1))})
                smoke = dict(runs={"smoke": dict(outcome=dict(execution_status=status, termination="worker_error"),
                                                 coverage=dict(integrity=integrity))})
                diagnosis = dict(conclusion=dict(status="repair_ready"))
                with patch("rsi.run.preflight"), patch("rsi.run.load_settings", return_value=config["model"]), \
                        patch("rsi.run.freeze_context"), patch("rsi.run.diagnosis_job"), patch("rsi.run.editor_job"), \
                        patch("rsi.run.validate", return_value=dict(passed=True, checks=[])), \
                        patch.object(run.versions, "current", return_value=self.parent), \
                        patch.object(run.versions, "candidate", return_value=self.root / "candidate"), \
                        patch.object(run.versions, "diff", return_value=["bot/components/tasks/economy.py"]), \
                        patch.object(run.versions, "freeze", return_value=self.parent), \
                        patch.object(run.analyzer, "query", return_value={}), \
                        patch.object(run, "batch", side_effect=[(dict(batch_id="explore"), exploration), ({}, smoke)]), \
                        patch.object(run, "model", side_effect=[diagnosis, "edited"]) as model:
                    result = run.execute()
                self.assertEqual(result["status"], expected, result.get("reason"))
                self.assertEqual(model.call_count, 2)
                self.assertFalse(result["validation"]["passed"])
                self.assertTrue((run.directory / "validation_0.json").exists())
                self.assertFalse((run.root / "current_version.json").exists())

    def investigation(self, policy="active"):
        """Construct a real investigation using frozen references and fixture logs."""
        context = self.root / ("context_" + policy)
        freeze_context(ROOT, context, [])
        options = dict(self.config["diagnosis"], query_policy=policy)
        job = dict(project=str(ROOT), context=str(context), parent=self.parent,
                   batches=[self.batch], diagnosis=options, analysis=self.config["analysis"])
        return Investigation(job, self.root), job

    def test_investigation_requires_updates_and_real_refs(self):
        """Active queries require prior review and reject invented evidence citations."""
        investigation, _ = self.investigation()
        investigation.update(issue=dict(symptom="fixture", runs=list(self.batch["runs"]),
            window=[0, 20], why_selected="repeated"), hypotheses=[
                dict(id=str(i), explanation="fixture", predictions=dict(support="a", refute="b")) for i in range(2)])
        query = dict(channel="analyzer", kind="metrics", params=dict(batch_id=self.batch["batch_id"], names=["workers"]),
                     purpose="inspect worker count", distinguishes="different worker curves")
        result = investigation.query(**query)
        self.assertTrue(result["source_refs"])
        with self.assertRaises(ValueError):
            investigation.query(**query)
        with self.assertRaises(ValueError):
            investigation.apply_updates([dict(id="0", support_refs=["invented"] )])
        final = investigation.query(**query, updates=[dict(id="0", reason="Observed worker count", support_refs=result["source_refs"][:1], unknowns=["cause unknown"])])
        conclusion = dict(status="repair_ready", change="Fixture intervention", evidence_refs=final["source_refs"][:1],
            code_targets=["bot/components/tasks/economy.py"], expected_effect=effects(),
            hypothesis_id="0", preserve="Keep other economy behavior", risks="Fixture only", uncertainty="Cause still needs intervention evidence")
        investigation.conclude(conclusion, updates=[dict(id="0", reason="Repeated observation", support_refs=final["source_refs"][:1], status="supported")])
        self.assertEqual(investigation.state["conclusion"]["status"], "repair_ready")
        with self.assertRaises(ValueError):
            investigation.query(**query)

    def test_fixed_pack_allows_issue_selection_but_no_adaptive_query(self):
        """The fixed-policy ablation supports a conclusion without permitting adaptive retrieval."""
        investigation, _ = self.investigation("fixed")
        investigation.fixed_pack()
        investigation.update(issue=dict(symptom="fixture", runs=list(self.batch["runs"]),
            window=[0, 20], why_selected="repeated"), hypotheses=[
                dict(id=str(i), explanation="fixture", predictions=dict(support="a", refute="b")) for i in range(2)])
        with self.assertRaises(ValueError):
            investigation.query("analyzer", "overview", dict(batch_id=self.batch["batch_id"]), "test", "test")
        result = investigation.conclude(dict(status="unresolved", reason="No discriminating evidence"))
        self.assertEqual(result["conclusion"]["status"], "unresolved")

    def test_query_correction_and_effect_preflight(self):
        """Invalid queries do not need evidence updates; conclusions require usable checks."""
        investigation, _ = self.investigation()
        investigation.update(issue=dict(symptom="fixture", runs=list(self.batch["runs"]), window=[0, 150],
            why_selected="fixture"), hypotheses=[dict(id=str(i), explanation="fixture",
            predictions=dict(support="a", refute="b")) for i in range(2)])
        query = dict(channel="analyzer", kind="metrics", purpose="inspect", distinguishes="worker counts")
        invalid = investigation.query(**query, params=dict(batch_id=self.batch["batch_id"], typo="workers"))
        self.assertEqual(invalid["status"], "invalid_query")
        result = investigation.query(**query, params=dict(batch_id=self.batch["batch_id"], names=["workers"]))
        self.assertEqual(result["status"], "ok")
        first, second = result["source_refs"][:2]
        investigation.apply_updates([dict(id="0", reason="First observation", support_refs=[first])])
        investigation.apply_updates([dict(id="0", reason="Second observation", support_refs=[second])])
        self.assertEqual(investigation.state["hypotheses"][0]["support_refs"], [first, second])
        with self.assertRaisesRegex(ValueError, "reason"):
            investigation.apply_updates([dict(id="0", status="supported")])
        conclusion = dict(status="repair_ready", hypothesis_id="0", change="fixture", preserve="economy",
            risks="fixture", uncertainty="fixture", code_targets=["bot/components/tasks/economy.py"],
            evidence_refs=[first], expected_effect=effects())
        for name, statistic in (("units_produced.BATTLECRUISER", "mean"), ("unit_count.BATTLECRUISER", "first_time")):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_effect(dict(effects()[0], name=name, statistic=statistic))
        missing = copy.deepcopy(conclusion)
        missing["expected_effect"][0]["name"] = "unit_count.BATTLECRUISER"
        with self.assertRaisesRegex(ValueError, "no usable observations"):
            investigation.conclude(missing)
        self.assertIsNone(investigation.state["conclusion"])
        investigation.conclude(conclusion)

    def test_editor_and_memory_keep_only_relevant_context(self):
        """The Editor receives selected facts; experience never embeds full investigations."""
        issue = dict(symptom="worker issue", runs=["run"], window=[0, 150], why_selected="fixture")
        conclusion = dict(status="repair_ready", hypothesis_id="0", change="Adjust worker selection",
            preserve="Keep other economy logic", code_targets=["bot/components/tasks/economy.py"],
            evidence_refs=["run:L1"], expected_effect=effects(), risks="fixture", uncertainty="fixture")
        diagnosis = dict(issue=issue, conclusion=conclusion,
            hypotheses=[dict(id="0", explanation="fixture", support_refs=["run:L1"], updates=[dict(reason="fixture")])],
            queries=[dict(channel="analyzer", kind="metrics", result=dict(source_refs=["run:L1", "run:L2"], items=[
                dict(name="workers", value=1, source_refs=["run:L1"]),
                dict(name="unrelated", source_refs=["run:L2"], payload="x" * 50000)]))])
        feedback = dict(passed=False, checks=[dict(name="syntax", passed=True),
                                              dict(name="import", passed=False, output="x" * 10000)])
        job = editor_job(self.root / "candidate", diagnosis, feedback)
        payload = json.loads(job["prompt"].split("\n")[-1])
        self.assertEqual(payload["issue"], issue)
        self.assertEqual(len(payload["evidence"]), 1)
        self.assertNotIn("updates", payload["hypothesis"])
        self.assertEqual(len(payload["validator_feedback"]["checks"]), 1)
        self.assertLess(len(payload["validator_feedback"]["checks"][0]["output"]), 10000)
        report = dict(iteration_id="fixture", status="inconclusive", conditions=dict(race="Zerg"), diagnosis=diagnosis,
                      validation=feedback, changed_files=conclusion["code_targets"],
                      artifacts=dict(diagnosis="diagnosis.json", patch="patch.diff"))
        record(self.root, report)
        memory = search(self.root, dict(race="Zerg"))[0]
        self.assertNotIn("queries", json.dumps(memory))
        self.assertEqual(memory["proposal"]["change"], conclusion["change"])
        self.assertEqual(memory["artifacts"]["patch"], "patch.diff")
        self.assertEqual(memory["status"], "inconclusive")

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
                self.assertFalse(run.analyzer.records)
                investigation = Investigation(dict(project=str(project), parent=job["parent"], context=job["context"],
                    batches=job["batches"], diagnosis=config["diagnosis"], analysis=config["analysis"]), Path(job["context"]))
                self.assertTrue(investigation.analyzer.query("overview", job["batches"][0]["batch_id"])["items"])
                value = dict(issue={}, hypotheses=[], queries=[], conclusion=dict(status="repair_ready",
                    code_targets=["bot/components/tasks/economy.py"], evidence_refs=[],
                    change="fixture", expected_effect=effects(), risks="fixture", uncertainty="fixture"))
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
        plan = freeze_plan(config["evaluation"], dict(conclusion=dict(expected_effect=effects())))
        self.assertEqual(evaluate(p, p, plan)["decision"], "inconclusive")

    def test_sdk_tools_with_scripted_transport(self):
        """Use the installed SDK with TestLLM; no network/model endpoint is called."""
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
        from openhands.sdk.testing import TestLLM
        from openhands.sdk.llm import Message, MessageToolCall, TextContent
        investigation, job = self.investigation()
        definitions = {tool.name: tool for tool in investigation_tools(investigation)}
        schema = definitions["investigate"].action_type.to_mcp_schema()
        self.assertIn("kind", schema["properties"])
        self.assertIn("kind", schema["required"])
        self.assertNotIn("params", definitions["select_issue"].action_type.to_mcp_schema()["properties"])
        query_params = dict(batch_id=self.batch["batch_id"], names=["workers"], time=[0, 2])
        evidence = investigation.analyzer.query("metrics", **query_params)["source_refs"][:1]
        job.update(role="diagnosis", model=self.config["model"], prompt="Offline fixture: use the prescribed tool sequence.")
        calls = [
            ("select_issue", dict(issue=dict(symptom="fixture", runs=list(self.batch["runs"]), window=[0, 20],
                why_selected="fixture"), hypotheses=[dict(id=str(i), explanation="fixture",
                predictions=dict(support="a", refute="b")) for i in range(2)])),
            ("investigate", dict(channel="analyzer", kind="metrics", params=dict(batch_id=self.batch["batch_id"],
                typo="workers"), purpose="fixture", distinguishes="fixture")),
            ("investigate", dict(channel="analyzer", kind="metrics", params=query_params,
                purpose="fixture", distinguishes="fixture")),
            ("conclude", dict(conclusion=dict(status="repair_ready", hypothesis_id="0", change="Fixture intervention",
                preserve="Keep economy behavior", code_targets=["bot/components/tasks/economy.py"],
                evidence_refs=evidence, expected_effect=effects(), risks="fixture", uncertainty="fixture"),
                updates=[dict(id="0", reason="Fixture observations support investigation", support_refs=evidence,
                              status="supported")])),
        ]
        messages = [Message(role="assistant", content=[TextContent(text="Offline fixture")],
            tool_calls=[MessageToolCall(id=str(i), name=name, arguments=json.dumps(params), origin="completion")])
            for i, (name, params) in enumerate(calls)]
        messages.append(Message(role="assistant", content=[TextContent(text="Done.")],
            tool_calls=[MessageToolCall(id="finish", name="finish", arguments=json.dumps(dict(message="Done.")), origin="completion")]))
        directory = self.root / "sdk"
        write_json(directory / "job.json", job)
        # Global SDK vision profiles are unrelated to this text-only fixture and live outside the sandbox.
        with patch("openhands.sdk.agent.base.has_vision_profile_available", return_value=False):
            result = execute(directory / "job.json", llm_factory=lambda: TestLLM.from_messages(messages))
        self.assertEqual(result["status"], "ok", result.get("reason"))
        self.assertEqual(result["value"]["conclusion"]["status"], "repair_ready")
        self.assertEqual(len(result["value"]["queries"]), 2)
        self.assertEqual(result["value"]["queries"][0]["result"]["status"], "invalid_query")

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
        path = candidate / "bot/components/tasks/economy.py"
        quote = (lambda value: "'" + str(value).replace("'", "''") + "'") if sys.platform == "win32" else shlex.quote
        code = ("from pathlib import Path; import bot, sys; "
                "assert Path(bot.__file__).resolve().parent == Path.cwd() / 'bot'; "
                f"assert Path(sys.executable).resolve() == Path({sys.executable!r}).resolve(); "
                "print('CANDIDATE_IMPORT_OK')")
        test_command = ("python -B -m unittest discover -s " + quote(str(ROOT / "scripts/test"))
                        + " -p test_bot.py -v")
        calls = [
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
        job = editor_job(candidate, dict(conclusion=dict(evidence_refs=[]), queries=[]))
        job["model"] = self.config["model"]
        directory = self.root / "sdk_editor"
        write_json(directory / "job.json", job)
        observations = []
        execute_terminal = TerminalExecutor.__call__

        def observe_terminal(executor, action, conversation=None):
            """Capture results while executing the official terminal unchanged."""
            observation = execute_terminal(executor, action, conversation)
            observations.append(observation)
            return observation

        with patch("openhands.sdk.agent.base.has_vision_profile_available", return_value=False), \
                patch.object(TerminalExecutor, "__call__", new=observe_terminal):
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
        self.assertTrue(validate(self.parent, candidate)["passed"])


if __name__ == "__main__":
    unittest.main()
