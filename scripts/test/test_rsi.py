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

from rsi.analyzer import Analyzer, load_batch_analysis, save_batch_analysis, run_view
from rsi.diagnosis.investigate import Investigation
from rsi.diagnosis.session import investigation_tools, make_job as diagnosis_job
from rsi.evaluator import METRICS, evaluate
from rsi.experiments import fixed_pack
from rsi.editor import make_job as editor_job
from rsi.memory import record, search
from rsi.run import Run, run_experiment
from rsi.tools.llm import budgeted_llm_type, call_session, execute
from rsi.tools.config import validate_config
from runner.matches import run_batch, run_match
from rsi.retriever import Retriever
from rsi.references import References, freeze_context
from rsi.tools.validate import validate
from runner.artifacts import path_reference, project_path, read_json, write_json
from runner.versions import Versions, inventory

ROOT = Path(__file__).resolve().parents[2]


def fixture_batch(project, version, conditions, directory, purpose, parallel=3, *, wall_budget=None):
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
                supply_used=15, supply_cap=15,
                score=dict(killed_value_units=1000 * frame / 3360, killed_value_structures=200 * frame / 3360,
                    lost_minerals_army=100 * frame / 3360, lost_vespene_army=50 * frame / 3360,
                    used_minerals_army=400, used_vespene_army=300,
                    total_used_minerals_army=800 * frame / 3360, total_used_vespene_army=600 * frame / 3360,
                    collected_minerals=3000 * frame / 3360, collected_vespene=1000 * frame / 3360),
                own=[dict(tag=1, type="SCV", build_progress=1,
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


def proposal():
    """Return the four-field editing handoff used by diagnosis and evaluation fixtures."""
    return dict(change="Fixture intervention", preserve="Keep economy behavior",
                verification="Inspect worker growth and check that supply and technology remain healthy.",
                code_targets=["bot/components/tasks/economy.py"])


def task(task_id="T1", hypothesis_id="0", **changes):
    """Return one independent intervention linked to an existing hypothesis."""
    return dict(id=task_id, hypothesis_ids=[hypothesis_id], **dict(proposal(), **changes))


def plan():
    """Return the diagnosis task plan."""
    return dict(tasks=[task()])


def sdk_messages(calls):
    from openhands.sdk.llm import Message, MessageToolCall, TextContent
    return [Message(role="assistant", content=[TextContent(text="Offline fixture")], tool_calls=[
        MessageToolCall(id=str(index), name=name, arguments=json.dumps(arguments), origin="completion")])
        for index, (name, arguments) in enumerate(calls)]


class PipelineTests(unittest.TestCase):
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
        report = analyzer.analyze_batch(self.batch)
        retriever = Retriever(self.parent["source_root"], ROOT, analyses=[report])
        run = next(iter(report["runs"].values()))
        self.assertEqual(run["coverage"]["integrity"], "complete")
        self.assertIn("mineral_income", run["coverage"]["missing_metrics"])
        self.assertEqual(run["actions"]["submitted_ratio"], 1)
        self.assertTrue(any(e["kind"] == "supply_block" for e in run["events"]))
        result = retriever.query_logs("trace", batch_id=self.batch["batch_id"], call_id="c1", frame=[0, 22])
        self.assertTrue(any(r.get("data", {}).get("status") == "observed" for r in result["items"]))
        self.assertTrue(all("run_id" in i and "batch_id" in i for i in result["items"]))
        with self.assertRaises(ValueError):
            analyzer.analyze_batch(dict(self.batch, purpose="holdout"))
        retriever.max_chars = 10
        result = retriever.query_logs("overview", batch_id=self.batch["batch_id"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["items"], [])

    def test_match_result_merge_preserves_worker_output(self):
        conditions = dict(self.config["matches"], seed=1)
        for with_id in (True, False):
            directory = self.root / f"match_{with_id}"
            result = dict(execution_status="normal", outcome="win", version=self.parent["version"])
            if with_id:
                result["run_id"] = directory.name
            with self.subTest(with_id=with_id), patch("runner.matches.subprocess.Popen") as process:
                process.return_value.wait.side_effect = lambda **kwargs: write_json(directory / "result.json", result)
                merged = run_match(ROOT, self.parent, conditions, directory, "parent", "batch")
            self.assertEqual(merged, read_json(directory / "result.json"))
            self.assertNotIn("run_id", merged)
            self.assertEqual(merged["outcome"], "win")
            self.assertNotIn("version", merged)

    def test_batch_staggers_three_submissions(self):
        events = []
        conditions = [dict(self.config["matches"], seed=seed) for seed in (1, 2, 3)]
        with patch("runner.matches.ThreadPoolExecutor") as executor, \
                patch("runner.matches.time.sleep", side_effect=lambda seconds: events.append(("sleep", seconds))):
            pool = executor.return_value.__enter__.return_value
            def submit(function, project, version, condition, *args, **kwargs):
                events.append(("submit", condition["seed"]))
                from concurrent.futures import Future
                future = Future()
                future.set_result(dict(seed=condition["seed"]))
                return future
            pool.submit.side_effect = submit
            batch = run_batch(ROOT, self.parent, conditions, self.root / "staggered", "parent")
        self.assertEqual(events, [("submit", 1), ("sleep", 3), ("submit", 2), ("sleep", 3), ("submit", 3)])
        self.assertEqual(len(batch["results"]), 3)
        executor.assert_called_once_with(max_workers=3)

    def test_match_cancellation_stops_worker_and_preserves_completed_result(self):
        import subprocess
        from threading import Event
        conditions = dict(self.config["matches"], seed=1)
        for completed in (False, True):
            with self.subTest(completed=completed):
                cancel = Event()
                directory = self.root / f"cancel_match_{completed}"
                saved = dict(execution_status="normal", outcome="win", termination="game_result")

                def wait(**kwargs):
                    if completed:
                        write_json(directory / "result.json", saved)
                    cancel.set()
                    raise subprocess.TimeoutExpired("offline worker", kwargs["timeout"])

                with patch("runner.matches.subprocess.Popen") as process, \
                        patch("runner.matches.stop_process") as stop:
                    process.return_value.wait.side_effect = wait
                    result = run_match(ROOT, self.parent, conditions, directory, "parent", "batch", cancel=cancel)
                stop.assert_called_once_with(process.return_value)
                self.assertEqual(result["termination"], "game_result" if completed else "cancelled")
                self.assertEqual(result["outcome"], "win" if completed else None)

    def test_batch_interrupt_cancels_active_and_queued_matches(self):
        from threading import Event
        first_done, second_started = Event(), Event()
        started = []
        conditions = [dict(self.config["matches"], seed=seed) for seed in range(1, 5)]
        directory = self.root / "interrupted_batch"

        def match(project, version, condition, path, purpose, batch_id, *, cancel):
            if cancel.is_set():
                return dict(execution_status="unknown", outcome=None, termination="cancelled",
                            run_id=path.name, directory=path_reference(path))
            started.append(condition["seed"])
            if condition["seed"] == 1:
                result = dict(execution_status="normal", outcome="win", termination="game_result")
                write_json(path / "result.json", result)
                first_done.set()
            else:
                second_started.set()
                self.assertTrue(cancel.wait(5), "Batch failed to signal cancellation before pool shutdown")
                result = dict(execution_status="unknown", outcome=None, termination="cancelled")
                write_json(path / "result.json", result)
            return dict(result, run_id=path.name, directory=path_reference(path))

        delays = []

        def delay(seconds):
            delays.append(seconds)
            if len(delays) == 1:
                self.assertTrue(first_done.wait(5))
            elif len(delays) == 2:
                self.assertTrue(second_started.wait(5))
            else:
                raise KeyboardInterrupt

        with patch("runner.matches.run_match", side_effect=match), \
                patch("runner.matches.time.sleep", side_effect=delay):
            with self.assertRaises(KeyboardInterrupt):
                run_batch(ROOT, self.parent, conditions, directory, "parent", parallel=1)
        batch = read_json(directory / "batch.json")
        self.assertEqual(started, [1, 2])
        self.assertEqual(batch["status"], "interrupted")
        self.assertEqual([r["termination"] for r in batch["results"]],
                         ["game_result", "cancelled", "cancelled", "cancelled"])
        self.assertEqual(batch["results"][0]["outcome"], "win")

    def test_analyzer_summaries_and_large_evidence_are_retrievable(self):
        batch = Analyzer(ROOT).analyze_batch(self.batch)
        retriever = Retriever(self.parent["source_root"], ROOT, analyses=[batch], max_items=60, max_chars=24000)
        result = retriever.query_logs("metrics", batch_id=self.batch["batch_id"], names=["workers", "minerals"])
        self.assertEqual([i["name"] for i in result["items"]], ["workers", "minerals"])
        self.assertNotIn("source_refs", result)
        self.assertNotIn("allowed_statistics", result["items"][0])
        self.assertNotIn("reason", result["items"][0])
        self.assertNotIn("truncated", result)
        self.assertTrue(all("run_id" in i and "batch_id" in i for i in result["items"]))
        raw = retriever.query_logs("metrics", batch_id=self.batch["batch_id"], names=["workers"], detail="raw")
        self.assertNotIn("truncated", raw)
        self.assertTrue(any("values" in item and "frame" in item for item in raw["items"]))
        run = next(iter(batch["runs"].values()))
        run["events"] = [dict(kind="production_idle", frame=0, end_frame=3360, data=dict(facility="STARPORT"),
                              source_refs=[f"run:L{i}" for i in range(3000)])]
        event = retriever.query_logs("events", batch_id=self.batch["batch_id"])["items"][0]
        self.assertEqual(event["source_ref_count"], 3000)
        self.assertTrue(event["source_refs"])
        self.assertLess(len(event["source_refs"]), 3000)
        self.assertEqual(len(run["events"][0]["source_refs"]), 3000)
        run["events"][0]["data"]["large"] = "x" * 30000
        oversized = retriever.query_logs("events", batch_id=self.batch["batch_id"])
        self.assertTrue(oversized["items"][0]["detail_omitted"])
        self.assertTrue(oversized["truncated"])
        self.assertNotIn("next_offset", oversized)
        self.assertEqual(oversized["items"][0]["kind"], "production_idle")
        trace = retriever.query_logs("trace", batch_id=self.batch["batch_id"], call_id="c1")
        self.assertFalse(any(r["kind"] == "state" for r in trace["items"]))
        self.assertTrue(any(r["kind"] == "state" for r in retriever.query_logs(
            "state", batch_id=self.batch["batch_id"])["items"]))

    def test_raw_pagination_preserves_results_and_stops_at_page_limit(self):
        batch = Analyzer(ROOT).analyze_batch(self.batch)
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
                yield dict(kind="unit_created", frame=i, data={}, source_refs=[f"run:L{i}"])
            raise AssertionError("Read past the requested page")

        retriever.max_chars = 24000
        run["events"] = events()
        page = retriever.query_logs("events", batch_id=self.batch["batch_id"])
        self.assertEqual(len(page["items"]), 3)
        self.assertNotIn("truncated", page)
        self.assertEqual(page["next_offset"], 3)

    def test_compact_queries_keep_coverage_and_each_comparison_run(self):
        batch = Analyzer(ROOT).analyze_batch(self.batch)
        report = next(iter(batch["runs"].values()))
        report["coverage"].update(integrity="partial", problems=["corrupt_line:100"], unattributed_deaths=2)
        reference = copy.deepcopy(batch)
        reference["batch_id"] = "reference"
        reference["runs"] = {"reference_0": copy.deepcopy(report), "reference_1": copy.deepcopy(report)}
        retriever = Retriever(self.parent["source_root"], ROOT, analyses=[batch, reference], max_items=1)
        with patch("rsi.retriever.read_records", side_effect=AssertionError("Unexpected raw-log read")):
            collected, offset = [], 0
            while True:
                page = retriever.query_logs("compare", batch_id=batch["batch_id"], reference_batch="reference",
                                            names=["workers"], offset=offset)
                collected.extend(page["items"])
                if "next_offset" not in page:
                    break
                offset = page["next_offset"]
            missing = retriever.query_logs("metrics", batch_id=batch["batch_id"], names=["mineral_income"])
            overview = retriever.query_logs("overview", batch_id=batch["batch_id"])
            raw = retriever.query_logs("overview", batch_id=batch["batch_id"], detail="raw")
        self.assertEqual([(r["batch_id"], r["run_id"]) for r in collected],
                         [("parent", "parent_0"), ("reference", "reference_0"), ("reference", "reference_1")])
        self.assertTrue(all("reference" not in row for row in collected))
        coverage = missing["quality"]["parent"]["parent_0"]
        self.assertEqual(coverage["problems"], ["corrupt_line:100"])
        self.assertEqual(coverage["unattributed_deaths"], 2)
        self.assertEqual(coverage["last_frame"], 3360)
        self.assertNotIn("missing_metrics", coverage)
        self.assertEqual(missing["items"][0]["reason"], report["coverage"]["missing_metrics"]["mineral_income"])
        self.assertNotIn("actions", overview["items"][0])
        self.assertEqual(raw["items"][0]["actions"], report["actions"])
        self.assertEqual(raw["items"][0]["modules"], report["modules"])

    def test_raw_log_cache_separates_batches_with_the_same_run_name(self):
        """run_01 in two batches must retain each batch's original trace."""
        retriever = Retriever(self.parent['source_root'], ROOT)
        first = dict(run_id='run_01', log_path=str(self.root / 'batch_01/run_01/trace.jsonl'))
        second = dict(run_id='run_01', log_path=str(self.root / 'batch_02/run_01/trace.jsonl'))
        with patch('rsi.retriever.read_records', side_effect=[([dict(value=1)], []), ([dict(value=2)], [])]) as read:
            self.assertEqual(retriever._records(first), [dict(value=1)])
            self.assertEqual(retriever._records(second), [dict(value=2)])
            self.assertEqual(retriever._records(first), [dict(value=1)])
        self.assertEqual(read.call_count, 2)

    def test_compact_trace_preserves_metrics_actions_and_module_totals(self):
        """Separating native data and summarizing quiet calls must preserve evaluation inputs."""
        from itertools import groupby
        from bot.telemetry.logger import Logger
        from rsi.analyzer import read_records

        original = Path(next(iter(self.batch['runs'].values())))
        compact = self.root / 'compact/run_01'
        compact.mkdir(parents=True)
        for name in ('manifest.json', 'result.json'):
            (compact / name).write_bytes((original / name).read_bytes())
        rows = [json.loads(line) for line in (original / 'trace.jsonl').read_text().splitlines()]
        logger = Logger(compact)
        for loop, group in groupby(rows, key=lambda row: row['frame']):
            group = list(group)
            calls = [{key: value for key, value in row.items() if key != 'event'}
                     for row in group if row['event'] == 'module_call']
            actions = [{key: value for key, value in row.items() if key != 'event'}
                       for row in group if row['event'] == 'action']
            logger._execution(loop, calls, actions)
            for row in group:
                if row['event'] not in {'module_call', 'action'}:
                    logger.emit(**row)
        logger.close()

        def without_refs(value):
            if isinstance(value, list):
                return [without_refs(item) for item in value]
            if isinstance(value, dict):
                return {key: without_refs(item) for key, item in value.items()
                        if key not in {'source_ref', 'source_refs'}}
            return value

        analyzer = Analyzer(ROOT, dict(sample_gap_frames=16))
        before = run_view(original, 'run_01', analyzer.analyze_run(original, 'run_01'))
        after = run_view(compact, 'run_01', analyzer.analyze_run(compact, 'run_01'))
        for key in ('metrics', 'events', 'actions', 'modules'):
            self.assertEqual(without_refs(before[key]), without_refs(after[key]), key)
        records, problems = read_records(compact, 'run_01')
        self.assertFalse(problems)
        self.assertTrue(all(row['source_ref'].startswith('run_01:state:L') for row in records if row['event'] == 'state'))
        report = dict(batch_id='compact', purpose='parent', runs={'run_01': after})
        retriever = Retriever(self.parent['source_root'], ROOT, analyses=[report])
        trace = retriever.query_logs('trace', batch_id='compact', module='Economy')
        self.assertTrue(any(row['kind'] == 'module_call' for row in trace['items']))
        (compact / 'state.jsonl').unlink()
        self.assertIn('missing_state', read_records(compact, 'run_01')[1])


    def test_reference_reads_use_sdk_and_json_keys(self):
        from openhands.tools.file_editor.editor import FileEditor
        context = self.root / "references"
        write_json(context / "knowledge/units.json", dict(SCV=dict(cost=dict(minerals=50))))
        path = context / "api/example.py"
        path.parent.mkdir()
        source = "".join(f"# line {i}\n" for i in range(1, 251))
        path.write_text(source, encoding="utf-8")
        retriever = Retriever(self.parent["source_root"], context)
        with patch.object(FileEditor, "read_file", autospec=True, side_effect=FileEditor.read_file) as read:
            result = retriever.read_reference("api/example.py", lines=[2, 4])
        read.assert_called_once()
        item = result
        self.assertEqual((item["start_line"], item["end_line"]), (2, 4))
        self.assertEqual(item["source"], "# line 2\n# line 3\n# line 4")
        self.assertEqual(result["next_line"], 5)
        capped = retriever.read_reference("api/example.py", lines=[10, 250])
        self.assertEqual(capped["end_line"], 129)
        tail = retriever.read_reference("api/example.py", lines=[240, 300])
        self.assertEqual(tail["end_line"], 250)
        self.assertNotIn("next_line", tail)
        self.assertEqual(path.read_text(encoding="utf-8"), source)
        self.assertEqual(retriever.read_reference("knowledge/units.json")["keys"], ["SCV"])
        entry = retriever.read_reference("knowledge/units.json", key="SCV.cost")
        self.assertEqual(entry["value"], dict(minerals=50))
        self.assertEqual(set(entry), {"path", "key", "value"})
        for arguments in (dict(path="api/../knowledge/units.json"), dict(path="entry.py"),
                          dict(path="api/example.py", key="SCV"), dict(path="api/example.py", lines=[0, 10]),
                          dict(path="knowledge/units.json", lines=[1, 10])):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                retriever.read_reference(**arguments)
        write_json(context / "knowledge/units.json", dict(SCV=dict(cost=dict(minerals=50), description="x" * 5000)))
        retriever.max_chars = 200
        self.assertTrue(retriever.read_reference("knowledge/units.json", key="SCV")["truncated"])
        self.assertEqual(retriever.read_reference("knowledge/units.json", key="SCV.cost")["value"], dict(minerals=50))
        retriever.max_chars = 10
        limited = retriever.read_reference("api/example.py")
        self.assertNotIn("source", limited)
        self.assertTrue(limited["truncated"])

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

    def test_project_references_resolve_outside_project_cwd(self):
        with TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            directory = Path(temporary)
            versions = Versions(ROOT, directory / "rsi")
            parent = versions.freeze(ROOT)
            self.assertFalse(Path(parent["source_root"]).is_absolute())
            self.assertNotIn("\\", parent["source_root"])
            batch = fixture_batch(ROOT, parent, [dict(self.config["matches"], seed=1)],
                                  directory / "parent", "parent")
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
                report = Analyzer(ROOT).analyze_batch(batch)
                log_path = next(iter(report["runs"].values()))["log_path"]
                self.assertFalse(Path(log_path).is_absolute())
                self.assertTrue(project_path(log_path).is_file())

    def test_iteration_summary_keeps_details_in_stage_artifacts(self):
        run = Run(ROOT, self.config)
        run.directory = self.root / "summary"
        diagnosis = dict(conclusion=plan(), queries=[dict(payload="x" * 10000)])
        validation = dict(passed=False, checks=[dict(name="import", passed=False, output="x" * 10000)])
        evaluation = dict(decision="inconclusive", reason="fixture", supplementary=[dict(payload="x" * 10000)])
        diagnosis_path = run.directory / "sessions/diagnosis/investigation.json"
        write_json(diagnosis_path, diagnosis)
        run.report["artifacts"]["diagnosis"] = path_reference(diagnosis_path)
        for name, value in (("validation_0", validation), ("evaluation", evaluation)):
            run.save(name, value)
        run.report.update(diagnosis=diagnosis, validation=validation, evaluation=evaluation)
        run.checkpoint("finished")
        summary = read_json(run.directory / "iteration.json")
        self.assertNotIn("config", summary)
        self.assertFalse((run.directory / "final.json").exists())
        self.assertFalse((run.directory / "diagnosis.json").exists())
        self.assertEqual(summary["diagnosis"], dict(status="repair_ready", tasks=1))
        self.assertEqual(summary["validation"], dict(passed=False, failed_checks=["import"]))
        self.assertEqual(summary["evaluation"], dict(decision="inconclusive", reason="fixture"))
        for name, value in (("diagnosis", diagnosis), ("validation_0", validation), ("evaluation", evaluation)):
            self.assertEqual(read_json(project_path(summary["artifacts"][name])), value)
        self.assertIs(run.report["diagnosis"], diagnosis)
        self.assertIs(run.report["validation"], validation)
        self.assertIs(run.report["evaluation"], evaluation)

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

    def test_native_score_metrics_and_missing_legacy_data(self):
        analyzer = Analyzer(ROOT)
        report = next(iter(analyzer.analyze_batch(self.batch)["runs"].values()))
        expected = dict(combat_value=1200, trade_efficiency=1200 / 1350, army_value=700,
                        army_investment=1400, resource_collection_rate=1600)
        for name, value in expected.items():
            self.assertAlmostEqual(report["metrics"][name][-1], value)
        self.assertNotIn("army_value", report["coverage"]["missing_metrics"])
        self.assertEqual(report["metrics"]["score.killed_value_units"][-1], 1000)
        path = Path(next(iter(self.batch["runs"].values()))) / "trace.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for row in rows:
            row.pop("score", None)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        legacy = next(iter(analyzer.analyze_batch(self.batch)["runs"].values()))
        self.assertTrue(all(value is None for value in legacy["metrics"]["combat_value"]))
        self.assertIn("army_value", legacy["coverage"]["missing_metrics"])

    def investigation(self, policy="active"):
        """Construct an investigation from frozen references and saved static analysis."""
        context = self.root / ("context_" + policy)
        freeze_context(ROOT, context)
        memory_path = self.root / "memory.json"
        write_json(memory_path, [])
        options = dict(self.config["diagnosis"], query_policy=policy)
        analysis_path = context / "analysis.json"
        save_batch_analysis(analysis_path, Analyzer(context).analyze_batch(self.batch))
        job = dict(project=str(ROOT), context=str(context), parent=self.parent,
                   batches=[self.batch], diagnosis=options, analyses=[str(analysis_path)], memory=str(memory_path))
        return Investigation(job, self.root), job

    def test_diagnosis_reads_static_analysis_without_recomputing(self):
        _, job = self.investigation()
        analysis = load_batch_analysis(job["analyses"][0])
        stored = read_json(job["analyses"][0])
        self.assertTrue(all(set(reference) == {"source", "analysis"} for reference in stored["runs"].values()))
        expected = copy.deepcopy(analysis)
        with patch.object(Analyzer, "analyze_run", side_effect=AssertionError("Repeated analysis")):
            investigation = Investigation(job, self.root)
            retriever = investigation.retriever
            with patch("rsi.retriever.read_records", side_effect=AssertionError("Unexpected raw-log read")):
                for kind, params in (("metrics", dict(names=["workers"])), ("events", {})):
                    self.assertTrue(retriever.query_logs(kind, batch_id=self.batch["batch_id"], **params)["items"])
            self.assertFalse(retriever.records)
            trace = retriever.query_logs("trace", batch_id=self.batch["batch_id"], call_id="c1")
            self.assertTrue(any(item.get("data", {}).get("status") == "observed" for item in trace["items"]))
            self.assertEqual(len(retriever.records), len(self.batch["runs"]))
        self.assertEqual(read_json(job["analyses"][0]), stored)
        self.assertEqual(retriever.batches[analysis["batch_id"]], expected)
        legacy_path = self.root / "legacy_analysis.json"
        write_json(legacy_path, expected)
        self.assertEqual(load_batch_analysis(legacy_path), expected)
        with self.assertRaisesRegex(ValueError, "Holdout"):
            Retriever(self.parent["source_root"], ROOT, analyses=[dict(analysis, purpose="holdout")])

    def test_initial_batch_reuse_is_read_only_and_validated(self):
        Analyzer(ROOT).analyze_batch(self.batch)
        history = {p: p.read_bytes() for p in (self.root / "parent").rglob("*") if p.is_file()}
        project = self.root / "reuse_project"
        shutil.copytree(ROOT / "bot", project / "bot", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "knowledge", project / "knowledge")
        config = copy.deepcopy(self.config)
        config["parallel_matches"] = 1
        config["parent_batch"] = str(self.root / "parent/batch.json")
        diagnosis = dict(conclusion=None, reason="fixture", queries=[])
        with patch("rsi.run.preflight"), patch("rsi.run.load_settings", return_value=config["model"]), \
                patch("rsi.run.run_batch", side_effect=AssertionError("Unexpected match")), \
                patch.object(Run, "_diagnose", return_value=diagnosis):
            report = run_experiment(project, config)
        self.assertEqual(report["status"], "completed", report.get("reason"))
        iteration = read_json(project_path(report["iterations"][0]["report"]))
        analysis = read_json(project_path(iteration["artifacts"][self.batch["batch_id"] + "_analysis"]))
        self.assertGreater(analysis["summary"]["event_counts"].get("resource_float", 0), 0)
        self.assertEqual(report["costs"]["matches"], 0)
        self.assertEqual({p: p.read_bytes() for p in (self.root / "parent").rglob("*") if p.is_file()}, history)
        config["parallel_matches"] = 2
        with patch("rsi.run.run_batch", side_effect=AssertionError("Unexpected match")):
            invalid = run_experiment(project, config)
        self.assertEqual(invalid["status"], "error")
        self.assertIn("conditions differ", invalid["reason"])
        self.assertFalse(invalid["iterations"])

    def test_diagnosis_guide_uses_frozen_parent(self):
        project = self.root / "different_workspace"
        (project / "bot").mkdir(parents=True)
        (project / "bot/README.md").write_text("UNRELATED_WORKSPACE_GUIDE", encoding="utf-8")
        context = self.root / "guide_context"
        (context / "knowledge").mkdir(parents=True)
        (context / "knowledge/README.md").write_text("FROZEN_KNOWLEDGE_GUIDE", encoding="utf-8")
        job = diagnosis_job(project, self.parent, [self.batch], {}, context, self.config, analysis_paths=[], memory_path=self.root / "memory.json")
        guide = (project_path(self.parent["source_root"]) / "bot/README.md").read_text(encoding="utf-8")
        self.assertIn(guide, job["prompt"])
        self.assertIn("FROZEN_KNOWLEDGE_GUIDE", job["prompt"])
        self.assertNotIn("UNRELATED_WORKSPACE_GUIDE", job["prompt"])

    def test_diagnosis_overview_keeps_core_data_and_missing_metrics(self):
        analysis = Analyzer(ROOT).analyze_batch(self.batch)
        second = copy.deepcopy(analysis)
        second["batch_id"] = "second"
        report = next(iter(second["runs"].values()))
        report["conditions"].update(map="OtherMap", difficulty="Hard")
        del report["metrics"]["vespene"]
        report["metrics"].pop("army_supply", None)
        report["events"].extend(copy.deepcopy(report["events"]))
        run = Run(ROOT, self.config)
        run.directory = self.root / "overview_iteration"
        with patch("rsi.retriever.read_records", side_effect=AssertionError("Unexpected raw-log read")):
            task = run._overview([analysis, second])
        self.assertEqual(read_json(run.directory / "overview.json"), task)
        self.assertEqual(set(task), {"runs", "events", "paths", "limits"})
        self.assertEqual([r["batch_id"] for r in task["runs"]], ["parent", "second"])
        self.assertEqual(task["runs"][1]["map"], "OtherMap")
        self.assertEqual(task["runs"][1]["difficulty"], "Hard")
        self.assertEqual(task["runs"][0]["metrics"]["workers_last"], 1)
        self.assertAlmostEqual(task["runs"][0]["metrics"]["minerals_mean"], 600)
        self.assertAlmostEqual(task["runs"][0]["metrics"]["supply_block_ratio"], 1)
        self.assertIsNone(task["runs"][1]["metrics"]["vespene_mean"])
        self.assertIsNone(task["runs"][1]["metrics"]["army_supply_last"])
        self.assertEqual(len(task["events"]), len({(e["batch_id"], e["run_id"], e["kind"]) for e in task["events"]}))
        self.assertNotIn("max_chars", task["limits"])

    def test_investigation_accepts_natural_evidence_and_autonomous_queries(self):
        investigation, _ = self.investigation()
        issue = dict(problem="Worker growth stalls", evidence=[
            "Explore run: worker counts stay flat while minerals accumulate around 60-150 seconds",
            "Economy task source and SCV reference entry are relevant"])
        hypotheses = [dict(id=str(i), explanation="fixture", predictions=dict(support="a", refute="b"),
                           code_targets=["bot/components/tasks/economy.py"]) for i in range(2)]
        accepted = investigation.update(issue=issue, hypotheses=hypotheses)
        self.assertEqual(accepted, dict(issue=issue, hypotheses=hypotheses))
        self.assertTrue(investigation.query("query_logs", kind="metrics", purpose="Distinguish the explanations",
                                            params=dict(names=["workers"]))["items"])
        self.assertTrue(investigation.query("read_reference", path="source/components/tasks/economy.py")["source"])
        result = investigation.conclude(plan())
        self.assertEqual(result["conclusion"], plan())
        self.assertEqual(set(result["conclusion"]), {"tasks"})
        self.assertEqual(investigation.state["issue"], issue)
        self.assertNotIn("updated", investigation.state["queries"][0])
        self.assertNotIn("support_refs", investigation.state["hypotheses"][0])
        with self.assertRaises(ValueError):
            investigation.query("query_logs", kind="overview", purpose="Already concluded")

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

    def test_conclusion_checks_required_fields_and_code_locations(self):
        investigation, _ = self.investigation()
        investigation.update(issue=dict(problem="fixture", evidence=["Overview and source observations"]),
            hypotheses=[dict(id=str(i), explanation="fixture", predictions=dict(support="a", refute="b"))
                        for i in range(2)])
        for change in (dict(verification=""), dict(code_targets=[]),
                       dict(code_targets=["bot/core/actions.py"]),
                       dict(code_targets=["bot/components/tasks/missing.py"]),
                       dict(code_targets=["bot.components.tasks.economy::Missing.execute"])):
            with self.subTest(change=change), self.assertRaises(ValueError):
                investigation.conclude(dict(tasks=[task(**change)]))
            self.assertIsNone(investigation.state["conclusion"])
        conclusion = dict(tasks=[task(code_targets=["bot.components.tasks.economy::EconomyTask.execute"])])
        self.assertEqual(investigation.conclude(conclusion)["conclusion"], conclusion)

    def test_query_budget_and_explicit_batch_selection(self):
        investigation, _ = self.investigation()
        investigation.update(issue=dict(problem="fixture", evidence=["Multiple exploration sources"]),
            hypotheses=[dict(id=str(i), explanation="fixture", predictions=dict(support="a", refute="b"))
                        for i in range(2)])
        investigation.options["max_queries"] = 2
        investigation.job["batches"].append(dict(self.batch, batch_id="another_batch"))
        invalid = investigation.query("query_logs", kind="metrics", purpose="Choose a batch", params=dict(names=["workers"]))
        self.assertEqual(invalid["error"]["code"], "invalid_query")
        self.assertIn("batch_id", invalid["error"]["message"])
        valid = investigation.query("query_logs", kind="metrics", purpose="Explicit scope",
            params=dict(batch_id=self.batch["batch_id"], names=["workers"], frame=[0, 448]))
        self.assertTrue(valid["items"])
        exhausted = investigation.query("read_reference", path="source/entry.py")
        self.assertEqual(exhausted["error"]["code"], "query_budget_exhausted")
        self.assertEqual(len(investigation.state["queries"]), 2)

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
        run = Run(ROOT, config)
        run.root = self.root / "unresolved_rsi"
        run.versions = Versions(ROOT, run.root)
        run.directory = run.root / "iteration"
        diagnosis = dict(issue=dict(problem="fixture", evidence=["Ambiguous observations"]),
                         hypotheses=[], queries=[], conclusion=None, reason="Competing explanations remain")
        with patch("rsi.run.preflight"), patch("rsi.run.load_settings", return_value=config["model"]), \
                patch("rsi.run.run_batch", side_effect=fixture_batch), \
                patch.object(run, "model", return_value=diagnosis) as model:
            result = run.execute().report
        self.assertEqual(result["status"], "unresolved", result.get("reason"))
        self.assertEqual(result["reason"], diagnosis["reason"])
        self.assertEqual(model.call_count, 1)
        self.assertNotIn("validation", result)
        self.assertFalse((run.directory / "candidate").exists())
        memory = read_json(run.root / "memory" / (result["iteration_id"] + ".json"))
        self.assertIsNone(memory["conclusion"])

    def test_editor_and_memory_keep_compact_context_and_full_query_records(self):
        issue = dict(problem="worker issue", evidence=["Run worker curve and source observations"])
        conclusion = plan()
        hypothesis = dict(id="0", explanation="fixture", predictions=dict(support="a", refute="b"),
                          code_targets=["bot/components/tasks/economy.py"])
        queries = [dict(tool="query_logs", arguments=dict(kind="metrics", params=dict(names=["workers"]), purpose="Inspect workers"),
                        result=dict(source_refs=["run:L1"], items=[dict(name="workers", source_refs=["run:L1"], payload="x"*50000)]))]
        diagnosis = dict(issue=issue, conclusion=conclusion,
                         hypotheses=[dict(hypothesis, revisions=[dict(reason="History stays out of prompt")])], queries=queries)
        feedback = dict(passed=False, checks=[dict(name="syntax", passed=True),
                                              dict(name="import", passed=False, output="x" * 10000)])
        write_json(self.root / "context/knowledge/fixture.json", dict(SCV={}))
        (self.root / "context/api").mkdir()
        write_json(self.root / "sessions/diagnosis/investigation.json", diagnosis)
        job = editor_job(self.root / "candidate", diagnosis, task(), feedback, context=self.root / "context",
                         evidence_path=self.root / "sessions/diagnosis/investigation.json")
        payload = job["task"]
        self.assertEqual(payload["issue"], issue)
        self.assertEqual(payload["hypotheses"], [hypothesis])
        self.assertEqual(payload["task"], task())
        self.assertNotIn("proposal", payload)
        self.assertNotIn("x"*5000, json.dumps(job))
        full_evidence = read_json((self.root / "candidate" / payload["paths"]["evidence"]).resolve())
        self.assertEqual(full_evidence["queries"], queries)
        self.assertFalse((self.root / "editor_evidence.json").exists())
        self.assertEqual(payload["paths"]["references"], "../context")
        self.assertNotIn("references", payload)
        self.assertEqual(project_path(job["context"]), self.root / "context")
        self.assertEqual(len(payload["validator_feedback"]["checks"]), 1)
        self.assertEqual(len(payload["validator_feedback"]["checks"][0]["output"]), 4000)
        report = dict(iteration_id="fixture", parent=self.parent, status="inconclusive", conditions=dict(race="Zerg"), diagnosis=diagnosis,
                        validation=feedback, changed_files=conclusion["tasks"][0]["code_targets"],
                      artifacts=dict(diagnosis="diagnosis.json", patch="patch.diff", validation_0="validation_0.json", config="config.json"))
        record(self.root, report)
        memory = search(self.root, dict(race="Zerg"))[0]
        self.assertNotIn("queries", json.dumps(memory))
        self.assertEqual(memory["issue"], issue)
        self.assertEqual(memory["conclusion"], conclusion)
        self.assertEqual(memory["parent"], self.parent["version"])
        self.assertNotIn("interpretation", memory)
        self.assertNotIn("proposal", memory)
        self.assertNotIn("config", memory["artifacts"])
        self.assertEqual(memory["artifacts"]["validation_0"], "validation_0.json")
        self.assertNotIn("hypotheses", memory)
        self.assertNotIn("checks", memory["validation"])
        self.assertNotIn("metrics", memory["evaluation"])
        self.assertEqual(memory["artifacts"]["patch"], "patch.diff")
        self.assertEqual(memory, read_json(self.root / "memory/fixture.json"))

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
    def scripted_model(project, job, directory, *, runtime=None):
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

    def test_sequential_rounds_reuse_accepted_code_and_batch(self):
        project, config = self.experiment_fixture()
        initial_bytes = (project / "bot/components/tasks/economy.py").read_bytes()
        calls = []

        def matches(project, version, conditions, directory, purpose, parallel, *, wall_budget=None):
            batch = fixture_batch(project, version, conditions, directory, purpose, parallel)
            calls.append((version, purpose, batch["batch_id"]))
            if len(calls) in (2, 4):
                for path in batch["runs"].values():
                    result = read_json(Path(path) / "result.json")
                    result["outcome"] = "win"
                    result["termination"] = "game_result"
                    write_json(Path(path) / "result.json", result)
                    if len(calls) == 4:
                        trace = Path(path) / "trace.jsonl"
                        rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
                        for row in rows:
                            if "score" in row:
                                for name in ("killed_value_units", "used_minerals_army", "total_used_minerals_army"):
                                    row["score"][name] *= 1.2
                        trace.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            return batch

        with patch("rsi.run.preflight") as preflight, patch("rsi.run.load_settings", return_value=config["model"]) as settings, \
                patch("rsi.run.call_session", side_effect=self.scripted_model), \
                patch("rsi.run.run_batch", side_effect=matches), \
                patch("rsi.run.Analyzer", wraps=Analyzer) as analyzer, \
                patch("rsi.run.freeze_context", wraps=freeze_context) as frozen:
            report = run_experiment(project, config, rounds=3)
        self.assertEqual(frozen.call_count, 1)
        self.assertEqual(analyzer.call_count, 1)
        self.assertEqual(settings.call_count, 2)
        self.assertEqual(preflight.call_count, 2)
        self.assertEqual(report["status"], "completed", report.get("reason"))
        self.assertEqual([r["status"] for r in report["iterations"]], ["accept", "reject", "accept"])
        self.assertEqual([purpose for _, purpose, _ in calls], ["parent", "candidate_t01", "candidate_t01", "candidate_t01"])
        self.assertEqual(report["costs"]["matches"], 4)
        rounds = report["iterations"]
        self.assertEqual(rounds[1]["parent"], rounds[0]["candidate"])
        archive = read_json(project_path(report["archive"]))
        self.assertEqual(len(archive["edges"]), 3)
        self.assertIn(rounds[2]["parent"]["version"], archive["nodes"])
        self.assertEqual(report["current_parent"], rounds[2]["candidate"])
        for index in (1, 2):
            saved = read_json(project_path(rounds[index]["report"]))
            self.assertTrue(saved["parent_batch"].endswith("batch.json"))
            self.assertEqual(saved["costs"]["matches"], 1)
            self.assertEqual(len(saved["costs"]["sessions"]), 2)
        self.assertIn(calls[3][2], report["current_batch"])
        self.assertEqual((project / "bot/components/tasks/economy.py").read_bytes(), initial_bytes)
        roots = list((project / "runtime/rsi/experiments").iterdir())
        self.assertEqual(len(roots), 1)
        self.assertEqual(len(list((roots[0] / "memory").glob("*.json"))), 3)
        experiment = read_json(roots[0] / "experiment.json")
        self.assertEqual(experiment["config"], config)
        self.assertNotIn("costs", experiment["iterations"][0])
        self.assertEqual(experiment["iterations"][0]["parent"], report["initial_parent"]["version"])
        for index, item in enumerate(rounds):
            directory = project_path(item["report"]).parent
            iteration = read_json(directory / "iteration.json")
            self.assertNotIn("config", iteration)
            self.assertFalse((directory / "context").exists())
            self.assertFalse((directory / "editor_evidence.json").exists())
            self.assertFalse((directory / "diagnosis.json").exists())
            self.assertFalse((directory / "final.json").exists())
            self.assertFalse(list(directory.glob("*_started.json")))
            self.assertFalse(list(directory.glob("*_response.json")))
            self.assertFalse(list(directory.glob("*_analysis.json")))
            self.assertEqual(len(read_json(directory / "memory.json")), index)
            job = read_json(project_path(iteration["artifacts"]["diagnosis_job"]))
            self.assertEqual(project_path(job["context"]).parent, project / "runtime/rsi/refs")
            self.assertEqual(project_path(job["context"]), project_path(report["context"]["source_root"]))
            self.assertEqual(project_path(job["memory"]), directory / "memory.json")
            memory = Retriever(job["parent"]["source_root"], job["context"], memory_path=job["memory"]).query_memory()
            self.assertEqual(len(memory["items"]), index)
            editor = read_json(project_path(iteration["artifacts"]["editor_t01_0_job"]))
            evidence = project_path(editor["workspace"]) / editor["task"]["paths"]["evidence"]
            self.assertEqual(read_json(evidence.resolve()), read_json(project_path(iteration["artifacts"]["diagnosis"])))
        self.assertFalse((project / "runtime/rsi/current_version.json").exists())

    def test_candidate_failure_stops_infrastructure_errors_without_repair_matches(self):
        project, config = self.experiment_fixture()
        for status, expected in (("unknown", "error"), ("bot_error", "reject")):
            calls = []

            def matches(project, version, conditions, directory, purpose, parallel, *, wall_budget=None):
                batch = fixture_batch(project, version, conditions, directory, purpose, parallel)
                calls.append(purpose)
                if purpose.startswith("candidate"):
                    path = Path(next(iter(batch["runs"].values()))) / "result.json"
                    write_json(path, dict(execution_status=status, termination="wall_timeout", outcome=None))
                return batch

            with self.subTest(status=status), patch("rsi.run.preflight"), \
                    patch("rsi.run.load_settings", return_value=config["model"]), \
                    patch("rsi.run.call_session", side_effect=self.scripted_model) as model, \
                    patch("rsi.run.run_batch", side_effect=matches):
                report = run_experiment(project, config, rounds=3 if status == "unknown" else 1)
            self.assertEqual(report["iterations"][0]["status"], expected, report.get("reason"))
            self.assertEqual(len(report["iterations"]), 1)
            self.assertEqual(calls, ["parent", "candidate_t01"])
            self.assertEqual(model.call_count, 2)
            self.assertEqual(report["current_parent"], report["initial_parent"])
        with patch("rsi.run.preflight"), patch("rsi.run.load_settings", return_value=config["model"]), \
                patch("rsi.run.run_batch", side_effect=KeyboardInterrupt):
            report = run_experiment(project, config, rounds=3)
        self.assertEqual(report["status"], "interrupted")
        self.assertEqual(len(report["iterations"]), 1)

    def test_static_failure_and_model_failure_do_not_run_candidates(self):
        project, config = self.experiment_fixture()
        config["max_repairs"] = 1
        with patch("rsi.run.preflight"), patch("rsi.run.load_settings", return_value=config["model"]), \
                patch("rsi.run.call_session", side_effect=self.scripted_model) as model, \
                patch("rsi.run.run_batch", side_effect=fixture_batch) as matches, \
                patch("rsi.run.validate", return_value=dict(passed=False, checks=[dict(name="import", passed=False)])):
            report = run_experiment(project, config)
        self.assertEqual(report["iterations"][0]["status"], "reject")
        self.assertEqual(matches.call_count, 1)
        self.assertEqual(model.call_count, 3)
        with patch("rsi.run.preflight"), patch("rsi.run.load_settings", return_value=config["model"]), \
                patch("rsi.run.call_session", return_value=dict(status="error", reason="Connection failed after retry", usage={})) as model, \
                patch("rsi.run.run_batch", side_effect=fixture_batch) as matches:
            failed = run_experiment(project, config, rounds=3)
        self.assertEqual(failed["status"], "error")
        self.assertEqual(len(failed["iterations"]), 1)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(matches.call_count, 1)
        self.assertIn("Connection failed", failed["reason"])

    def test_parent_selection_and_configuration_boundaries(self):
        project, config = self.experiment_fixture()
        source = self.root / "custom_parent"
        shutil.copytree(project / "bot", source / "bot")
        path = source / "bot/components/tasks/economy.py"
        path.write_text(path.read_text(encoding="utf-8") + "\n# Explicit parent.\n", encoding="utf-8")
        selected = Versions(project, project / "runtime/rsi").freeze(source)
        with patch("rsi.run.preflight"), patch("rsi.run.load_settings", return_value=config["model"]), \
                patch("rsi.run.run_batch", side_effect=fixture_batch), \
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
        run = Run(ROOT, config, root=self.root / "five_matches")
        candidate = self.versions.candidate(self.parent, self.root / "seed_candidate")
        path = candidate / "bot/components/tasks/economy.py"
        path.write_text(path.read_text(encoding="utf-8") + "\n# Distinct seed fixture.\n", encoding="utf-8")
        child = self.versions.freeze(candidate)
        with patch("rsi.run.preflight"), patch("rsi.run.load_settings", return_value=config["model"]), \
                patch("rsi.run.run_batch", side_effect=fixture_batch) as matches, \
                patch.object(run, "_diagnose", return_value=dict(conclusion=plan())), \
                patch.object(run, "_edit_and_validate", return_value=(child, dict(passed=True, checks=[]))):
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

        def model(project, job, directory, *, runtime=None):
            jobs.append(job)
            result = self.scripted_model(project, job, directory)
            if job["role"] == "diagnosis":
                result["value"]["conclusion"] = dict(tasks=[task("T1"), task("T2")])
            result["usage"] = dict(tokens=job["model"]["max_total_tokens"] // 2,
                                   cost=job["model"]["max_cost"] / 2)
            return result

        with patch.dict(os.environ, LLM_MODEL="openai/environment", LLM_BASE_URL="https://environment.invalid/v1"), \
                patch("rsi.run.preflight") as preflight, patch("rsi.run.run_batch", side_effect=fixture_batch), \
                patch("rsi.run.call_session", side_effect=model), \
                patch("rsi.run.validate", return_value=dict(passed=True, checks=[])):
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
        for name, job in zip(("diagnosis_job", "editor_t01_0_job", "editor_t02_0_job"), jobs):
            self.assertEqual(read_json(project_path(iteration["artifacts"][name]))["model"], job["model"])
        evaluation = read_json(project_path(iteration["artifacts"]["evaluation_t01"]))
        self.assertEqual(evaluation["rules"]["min_metric_improvement"], 0.05)
        self.assertEqual(evaluation["rules"]["min_improved_fraction"], 0.6)

    def test_diagnosis_tool_token_budget(self):
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
        self.assertEqual(set(conclusion_type.model_fields), {"tasks"})
        task_type = conclusion_type.model_fields["tasks"].annotation.__args__[0]
        self.assertEqual(set(task_type.model_fields), {"id", "hypothesis_ids", "change", "preserve", "verification", "code_targets"})
        issue = dict(problem="fixture", evidence=["Run one, worker curve near 60-150 seconds", "SCV knowledge entry"])
        self.assertEqual(issue_type(**issue).evidence, issue["evidence"])
        with self.assertRaises(ValidationError):
            issue_type(**issue, window=[0, 1])
        with self.assertRaises(ValidationError):
            conclusion_type(**plan(), expected_effect=[])
        self.assertEqual(set(tools[2].action_type.model_fields), {"path", "lines", "key"})
        self.assertEqual(set(tools[3].action_type.model_fields), {"text"})
        self.assertNotIn("updates", tools[1].action_type.to_mcp_schema()["properties"])
        self.assertNotIn("updates", tools[-1].action_type.to_mcp_schema()["properties"])


    def test_llm_settings_reach_sdk(self):
        from pydantic import ValidationError
        from openhands.sdk.llm.options.chat_options import select_chat_options
        from rsi.tools.llm import _create_llm, preflight
        settings = dict(self.config["model"], model="openai/gpt-4o", api_key_env="RSI_TEST_API_KEY",
                        reasoning_effort="low", temperature=0.2, top_p=0.8, top_k=20, seed=43)
        with patch.dict(os.environ, RSI_TEST_API_KEY="offline"):
            llm = _create_llm(dict(role="diagnosis", model=settings))
            for field in ("reasoning_effort", "temperature", "top_p", "top_k", "seed", "max_output_tokens"):
                self.assertEqual(getattr(llm, field), settings[field])
            self.assertEqual(llm.timeout, settings["request_timeout"])
            self.assertEqual(llm.num_retries, 2)
            reasoning_llm = _create_llm(dict(role="diagnosis", model=dict(
                settings, model="openrouter/anthropic/claude-haiku-5.5")))
            options = select_chat_options(reasoning_llm, {}, has_tools=True)
            self.assertEqual(options["reasoning_effort"], "low")
            self.assertEqual(options["max_completion_tokens"], settings["max_output_tokens"])
            self.assertNotIn("temperature", options)
            with self.assertRaises(ValidationError):
                preflight(dict(settings, top_p=2))

    def test_sdk_request_retries_once(self):
        from litellm import APIConnectionError, ModelResponse
        from openhands.sdk.llm import Message, TextContent
        from openhands.sdk.llm.exceptions import LLMServiceUnavailableError
        from rsi.tools.llm import _create_llm
        settings = dict(self.config["model"], model="openai/gpt-4o", api_key_env="RSI_TEST_API_KEY")
        messages = [Message(role="user", content=[TextContent(text="Offline retry fixture")])]
        reply = ModelResponse(choices=[dict(message=dict(role="assistant", content="Recovered"), finish_reason="stop")],
                              usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2))
        error = APIConnectionError(message="Offline connection failure", llm_provider="openai", model="gpt-4o")
        with patch.dict(os.environ, RSI_TEST_API_KEY="offline"):
            llm = _create_llm(dict(role="diagnosis", model=settings))
        llm.retry_min_wait = llm.retry_max_wait = 0
        with patch.object(type(llm), "_transport_call", side_effect=[error, reply]) as transport:
            result = llm.completion(messages)
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(result.message.content[0].text, "Recovered")
        with patch.object(type(llm), "_transport_call", side_effect=error) as transport:
            with self.assertRaises(LLMServiceUnavailableError):
                llm.completion(messages)
        self.assertEqual(transport.call_count, 2)

    def test_sdk_session_limits_and_completion_status(self):
        from openhands.sdk.testing import TestLLM

        class ChargedLLM(TestLLM):
            def completion(self, *args, **kwargs):
                """Report an offline charge so the SDK's actual cost limit is exercised."""
                response = super().completion(*args, **kwargs)
                self.metrics.add_cost(0.1)
                return response

        for case, tool, arguments, error in (
                ("finished", "finish", dict(message="Done."), None),
                ("iterations", "think", dict(thought="Continue checking"), "MaxIterationsReached"),
                ("budget", "think", dict(thought="Continue checking"), "MaxBudgetReached")):
            with self.subTest(case=case):
                message = sdk_messages([(tool, arguments)])[0]
                settings = dict(self.config["model"], max_iterations=1 if case == "iterations" else 5, max_cost=0.05)
                directory = self.root / ("sdk_limit_" + case)
                write_json(directory / "job.json", dict(role="evaluation_plan", model=settings, prompt="Offline fixture"))
                llm_type = ChargedLLM if case == "budget" else TestLLM
                llm = llm_type.from_messages([message], usage_id="limit_" + case, api_key="offline")
                with patch("openhands.sdk.agent.base.has_vision_profile_available", return_value=False), redirect_stdout(StringIO()):
                    result = execute(directory / "job.json", llm_factory=lambda: llm)
                self.assertEqual(result["status"], "error" if error else "ok", result.get("reason"))
                self.assertEqual(result["execution_status"], "error" if error else "finished")
                if error:
                    self.assertIn(error, result["reason"])
                    self.assertNotIn("value", result)
                else:
                    self.assertEqual(result["value"], "Done.")
                if case == "budget":
                    self.assertAlmostEqual(result["usage"]["cost"], 0.1)
                saved = read_json(directory / "llm.json")
                self.assertEqual(saved["reasoning_effort"], llm.reasoning_effort)
                self.assertNotIn("api_key", saved)
                self.assertNotIn("offline", (directory / "llm.json").read_text(encoding="utf-8"))
                self.assertEqual(read_json(directory / "response.json"), result)

    def test_model_worker_console_relay_keeps_stderr_in_file(self):
        import subprocess
        import sys
        popen = subprocess.Popen
        directory = self.root / "relay"
        code = ("import json,sys; from pathlib import Path; "
                "job = Path(json.loads(sys.stdin.readline())); "
                "(job.parent / 'stdout.txt').write_text('[diagnosis] evidence progress\\n', encoding='utf-8'); "
                "(job.parent / 'stderr.txt').write_text('SDK internal noise', encoding='utf-8'); "
                "response = Path(sys.argv[1]); investigation = response.parent / 'investigation.json'; "
                "investigation.write_text(json.dumps(dict(queries=[], conclusion=None)), encoding='utf-8'); "
                "response.write_text(json.dumps(dict(status='ok', result_path=str(investigation))), encoding='utf-8'); "
                "(job.parent / 'done').touch()")

        def worker(args, **kwargs):
            """Use a real local worker without calling any model endpoint."""
            return popen([sys.executable, "-B", "-c", code, str(directory / "response.json")], **kwargs)

        output = StringIO()
        with patch("rsi.tools.llm.subprocess.Popen", side_effect=worker), redirect_stdout(output):
            result = call_session(ROOT, dict(model=dict(wall_timeout=10)), directory)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["value"], read_json(directory / "investigation.json"))
        self.assertNotIn("value", read_json(directory / "response.json"))
        self.assertIn("[diagnosis] evidence progress", output.getvalue())
        self.assertNotIn("SDK internal noise", output.getvalue())
        self.assertIn("SDK internal noise", (directory / "stderr.txt").read_text(encoding="utf-8"))

    def test_model_worker_failure_records_remain_in_the_session(self):
        for case in ("timeout", "interrupt", "missing"):
            with self.subTest(case=case):
                directory = self.root / ("worker_" + case)
                with patch("rsi.tools.llm.subprocess.Popen") as popen, patch("rsi.tools.llm.stop_process") as stop:
                    process = popen.return_value
                    process.poll.return_value = 0 if case == "missing" else None
                    process.wait.side_effect = [KeyboardInterrupt, None] if case == "interrupt" else None
                    timeout = 0 if case == "timeout" else 10
                    if case == "interrupt":
                        with self.assertRaises(KeyboardInterrupt):
                            call_session(ROOT, dict(model=dict(wall_timeout=timeout)), directory)
                    else:
                        result = call_session(ROOT, dict(model=dict(wall_timeout=timeout)), directory)
                        self.assertEqual(result, read_json(directory / "response.json"))
                    saved = read_json(directory / "response.json")
                    self.assertEqual(saved["status"], "interrupted" if case == "interrupt" else "error")
                    self.assertNotIn("value", saved)
                    self.assertEqual(stop.call_count, 0 if case == "missing" else 1)

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
        output = StringIO()
        with patch("openhands.sdk.agent.base.has_vision_profile_available", return_value=False), redirect_stdout(output):
            result = execute(directory / "job.json", llm_factory=lambda: TestLLM.from_messages(messages))
        self.assertEqual(result["status"], "ok", result.get("reason"))
        self.assertEqual(result["value"]["conclusion"], plan())
        saved = read_json(directory / "response.json")
        self.assertNotIn("value", saved)
        self.assertEqual(read_json(project_path(saved["result_path"])), result["value"])
        self.assertEqual(len(result["value"]["queries"]), 5)
        self.assertEqual(result["value"]["queries"][0]["result"]["error"]["code"], "invalid_query")
        self.assertIn("Issue: fixture", output.getvalue())
        self.assertIn("Hypothesis 0: fixture", output.getvalue())
        self.assertIn("Query metrics", output.getvalue())
        self.assertIn("Read source/components/tasks/economy.py", output.getvalue())
        self.assertIn("Query memory: workers", output.getvalue())
        self.assertEqual(result["value"]["queries"][3]["result"]["key"], "WORKER_SATURATION")
        self.assertIn("Tool rejected:", output.getvalue())
        self.assertIn("Task T1: Fixture intervention", output.getvalue())
        self.assertIn("Tool rejected (select_issue):", output.getvalue())
        for failure, target in (("render", "rsi.tools.console._tool_progress"), ("write", "rsi.tools.console.print")):
            with self.subTest(failure=failure):
                failed_directory = self.root / ("sdk_display_" + failure)
                write_json(failed_directory / "job.json", job)
                with patch("openhands.sdk.agent.base.has_vision_profile_available", return_value=False), \
                        patch(target, side_effect=RuntimeError("Display failure"), create=True):
                    recovered = execute(failed_directory / "job.json", llm_factory=lambda: TestLLM.from_messages(messages))
                self.assertEqual(recovered["status"], "ok", recovered.get("reason"))
                self.assertEqual(recovered["value"]["conclusion"], plan())
                self.assertTrue((failed_directory / "usage.json").is_file())
                self.assertTrue((failed_directory / "investigation.json").is_file())

    def test_sdk_fixed_pack_is_retained_in_diagnosis(self):
        from openhands.sdk.testing import TestLLM
        from openhands.sdk.conversation import LocalConversation
        _, job = self.investigation("fixed")
        job["diagnosis"]["max_queries"] = 2
        job.update(role="diagnosis", model=self.config["model"], prompt="Offline fixed evidence fixture.",
                   task=dict(fixture=True))
        message = sdk_messages([("finish", dict(message="Done."))])[0]
        directory = self.root / "sdk_fixed"
        write_json(directory / "job.json", job)
        sent = []
        send_message = LocalConversation.send_message

        def capture(conversation, message, *args, **kwargs):
            """Inspect the actual initial message sent through the SDK."""
            sent.append(message)
            return send_message(conversation, message, *args, **kwargs)

        with patch("openhands.sdk.agent.base.has_vision_profile_available", return_value=False), redirect_stdout(StringIO()), \
                patch.object(LocalConversation, "send_message", new=capture):
            result = execute(directory / "job.json", llm_factory=lambda: TestLLM.from_messages([message]))
        self.assertEqual(result["status"], "ok", result.get("reason"))
        payload, pack = sent[0].split("Task data (JSON):\n", 1)[1].split("\n\nPredeclared evidence pack (JSON query records):\n", 1)
        self.assertEqual(json.loads(payload), read_json(directory / "job.json")["task"])
        queries = result["value"]["queries"]
        self.assertEqual(json.loads(pack), json.loads(json.dumps(queries)))
        self.assertEqual([query["arguments"]["kind"] for query in queries], ["overview", "metrics"])
        self.assertTrue(all(query["result"]["items"] for query in queries))
        self.assertEqual(read_json(directory / "investigation.json")["queries"], json.loads(json.dumps(queries)))

    def test_sdk_unresolved_preserves_reason_and_handles_missing_conclusion(self):
        from openhands.sdk.testing import TestLLM
        _, job = self.investigation()
        job.update(role="diagnosis", model=self.config["model"], prompt="Offline unresolved fixture.")
        for explicit in (True, False):
            with self.subTest(explicit=explicit):
                calls = [("conclude", dict(reason="Evidence cannot distinguish the causes"))] if explicit else []
                calls.append(("finish", dict(message="Done.")))
                messages = sdk_messages(calls)
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
        import sys
        from openhands.sdk.testing import TestLLM
        from openhands.tools.file_editor import FileEditorTool
        from openhands.tools.terminal import TerminalExecutor, TerminalTool

        candidate = self.versions.candidate(self.parent, self.root / "editor_candidate")
        context = self.root / "editor_context"
        freeze_context(ROOT, context)
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
        messages = sdk_messages(calls + [("finish", dict(message="Done."))])
        job = editor_job(candidate, dict(conclusion=plan(), queries=[]), task(), context=context,
                         evidence_path=self.root / "investigation.json")
        payload = job["task"]
        self.assertEqual(payload["task"], task())
        self.assertEqual((candidate / payload["paths"]["references"]).resolve(), context.resolve())
        self.assertIn((candidate / "bot/README.md").read_text(encoding="utf-8"), job["prompt"])
        self.assertIn((context / "knowledge/README.md").read_text(encoding="utf-8"), job["prompt"])
        job["model"] = self.config["model"]
        directory = self.root / "sdk_editor"
        write_json(directory / "job.json", job)
        observations = []
        reference_results = []
        read_reference = References.read_reference
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
                patch.object(References, "read_reference", new=observe_reference):
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
        self.assertEqual(reference_results[0]["source"],
                         "\n".join(api_source.decode("utf-8").splitlines()[:12]))
        self.assertEqual(reference_results[1]["key"], "WORKER_SATURATION")
        self.assertEqual((context / "api/bot_ai.py").read_bytes(), api_source)
        self.assertTrue(validate(self.parent, candidate)["passed"])


if __name__ == "__main__":
    unittest.main()
