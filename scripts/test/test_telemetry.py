"""Format round trips and derived-data boundaries without SC2 or model execution."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from bot.telemetry.logger import Logger, SCORE_FIELDS
from rsi.analyzer import Analyzer, load_batch_analysis, run_view, save_batch_analysis
from rsi.logs import read_records
from rsi.retriever import Retriever
from runner.artifacts import read_json, write_json


ROOT = Path(__file__).resolve().parents[2]


def sample(frame, *, unknown=False):
    unit = dict(tag=1, type="SCV", position=[10, 20], health=45, shield=0, build_progress=1,
                orders=[dict(ability="MOVE", target=[20, 30], progress=.25)])
    units = [unit]
    if unknown:
        units.append(dict(unit, tag=2, type="UNKNOWN_FIXTURE"))
    return dict(frame=frame, minerals=600 + frame, vespene=0, supply_used=15, supply_cap=15,
                score=dict.fromkeys(SCORE_FIELDS, 0), own=units, enemies=[dict(unit, tag=9)])


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_native_columns_round_trip_and_callback_separation(self):
        logger = Logger(self.root, chunk_size=3)
        originals = [sample(frame) for frame in (0, 2, 8)]
        for row in originals:
            logger.emit("state", **row)
        logger.emit("unit_created", 2, tag=10, type="MARINE")
        logger.close()
        logger.close()
        chunks = [json.loads(line) for line in (self.root / "state.jsonl").read_text().splitlines()]
        self.assertEqual(chunks[0]["frame_step"], 2)
        self.assertEqual([len(row["minerals"]) for row in chunks[1:]], [2, 1])
        self.assertEqual([row["start_frame"] for row in chunks[1:]], [0, 8])
        self.assertFalse(any("frame" in row for row in chunks[1:]))
        self.assertNotIn("unit_created", (self.root / "state.jsonl").read_text())
        rows, problems = read_records(self.root, "run_01")
        self.assertFalse(problems)
        recovered = [{key: row[key] for key in originals[0]} for row in rows if row["event"] == "state"]
        self.assertEqual(recovered, originals)
        event = json.loads((self.root / "events.jsonl").read_text().splitlines()[1])
        self.assertEqual(event, dict(frame=2, kind="unit_created", data=dict(tag=10, type="MARINE")))

    def test_trace_restores_request_parameters_changes_and_ambiguous_errors(self):
        logger = Logger(self.root)
        call = dict(frame=0, call_id="call1", parent_id="", module_id="Fixture",
                    module_path="bot.components.fixture", path="Root/Fixture", priority=30, status="invoked_true")
        request = dict(frame=0, call_id="call1", source="Root/Fixture", priority=30, request_id="request1",
                       unit_tag=7, ability="MOVE", target=[10, 20], product=None, queue=False, status="proposed", reason="")
        logger._execution(0, [call], [request, dict(request, status="submitted")])
        logger._execution(2, [], [dict(request, frame=2, status="observed", reason="unit_order")])
        logger._execution(4, [], [dict(request, frame=4, target=[20, 30], status="completed"),
            dict(frame=4, status="ambiguous_error", unit_tag=7, ability="MOVE", reason="engine:error", candidates=["request1"])])
        logger.emit("bot_runtime_error", 4, exception="Fixture traceback")
        logger.emit("bot_runtime_error", 4, exception="Fixture traceback")
        logger.close()
        raw = [json.loads(line) for line in (self.root / "trace.jsonl").read_text().splitlines()]
        statuses = [row for row in raw if row["kind"] == "action_status"]
        self.assertNotIn("unit_tag", statuses[0]["data"])
        self.assertNotIn("target", statuses[1]["data"])
        self.assertEqual(statuses[2]["data"]["target"], [20, 30])
        self.assertEqual(sum(row["kind"] == "exception" for row in raw), 1)
        rows, problems = read_records(self.root, "run_01")
        self.assertFalse(problems)
        actions = [row for row in rows if row["event"] == "action"]
        for row in actions[:4]:
            self.assertEqual((row["request_id"], row["call_id"], row["source"], row["priority"]), (1, 1, "Root/Fixture", 30))
            self.assertEqual(row["unit_tag"], 7)
        self.assertEqual(actions[-1]["candidates"], [1])
        self.assertEqual(actions[-1]["status"], "ambiguous_error")

    def test_analyzer_requires_no_metadata_and_stores_only_aligned_derivations(self):
        logger = Logger(self.root)
        for frame in (0, 2, 4, 6):
            logger.emit("state", **sample(frame, unknown=frame == 2))
        logger.close()
        analysis = Analyzer(ROOT, dict(event_frames=4)).analyze_run(self.root)
        self.assertEqual(set(analysis), {"schema_version", "config", "series", "events", "quality"})
        self.assertNotIn("frame", analysis)
        self.assertNotIn("minerals", analysis["series"])
        self.assertFalse(any(name.startswith("score.") for name in analysis["series"]))
        self.assertTrue(all(len(values) == 4 for values in analysis["series"].values()))
        self.assertEqual(analysis["series"]["army_supply"], [0, None, 0, 0])
        event = next(event for event in analysis["events"] if event["kind"] == "supply_block")
        self.assertEqual(event, dict(frame=0, end_frame=6, kind="supply_block", data={}))
        self.assertEqual(analysis["quality"]["state"]["covered_frames"], 6)
        self.assertFalse((self.root / "manifest.json").exists())

    def test_saved_analysis_references_source_without_modifying_reused_evidence(self):
        source, output = self.root / "original/run_01", self.root / "new_batch"
        logger = Logger(source)
        for frame in (0, 2, 4):
            logger.emit("state", **sample(frame))
        logger.close()
        write_json(source / "manifest.json", dict(map="fixture", race="Zerg", difficulty="Easy", seed=1))
        write_json(source / "result.json", dict(schema_version=3, execution_status="normal", outcome="win", last_frame=4, steps=3))
        history = {path.name: path.read_bytes() for path in source.iterdir()}
        batch = dict(batch_id="new_batch", version="fixture", purpose="parent", runs={"run_01": str(source)})
        analysis = Analyzer(ROOT).analyze_batch(batch)
        save_batch_analysis(output / "analysis.json", analysis)
        self.assertEqual({path.name: path.read_bytes() for path in source.iterdir()}, history)
        stored = read_json(output / "run_01/analysis.json")
        self.assertEqual(set(stored), {"schema_version", "config", "series", "events", "quality"})
        restored = load_batch_analysis(output / "analysis.json")
        self.assertEqual(restored, analysis)
        retriever = Retriever(ROOT, ROOT, analyses=[restored])
        response = retriever.query_logs("metrics", batch_id="new_batch", names=["minerals", "workers"], detail="raw", frame=[0, 2])
        points = next(row for row in response["items"] if row["name"] == "minerals" and "values" in row)
        self.assertEqual(points["frame"], [0, 2])
        self.assertEqual(points["values"], [600, 602])

    def test_corrupt_columns_and_unresolved_requests_are_reported_by_source(self):
        logger = Logger(self.root)
        logger.emit("state", **sample(0))
        logger.close()
        path = self.root / "state.jsonl"
        lines = path.read_text().splitlines()
        block = json.loads(lines[1])
        block["minerals"] = []
        path.write_text(lines[0] + "\n" + json.dumps(block) + "\n", encoding="utf-8")
        with (self.root / "trace.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(frame=0, kind="action_status", data=dict(request_id=99, status="observed"))) + "\n")
        analysis = Analyzer(ROOT).analyze_run(self.root)
        self.assertIn("corrupt_line:state:2", analysis["quality"]["state"]["problems"])
        self.assertIn("corrupt_line:trace:2", analysis["quality"]["trace"]["problems"])


if __name__ == "__main__":
    unittest.main()
