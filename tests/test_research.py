"""Verify meaningful research JSON and keep runtime bookkeeping outside results."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from rsi.agents.discovery import Discovery
from rsi.agents.hypotheses import Hypotheses
from rsi.agents.investigation import Investigation
from rsi.agents.resolution import Resolution
from rsi.contracts import investigation_context
from runtime.workspace.storage import read_json
from tests.fixtures import ROOT, metric, problem, hypotheses, diagnosis, task, evidence, tests


class ResearchTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.job = dict(role="discovery", parent=dict(source_root=str(ROOT)),
                        max_analyses=2, sampling_remaining=2, sampling_styles=["Macro", "Rush"])

    def result(self, **changes):
        return dict(dict(action="diagnosis", diagnosis=diagnosis(), tests=tests()), **changes)

    def investigate(self, value, **previous):
        stage = Investigation(dict(self.job, role="investigation",
            task=copy.deepcopy(dict(dict(hypotheses=hypotheses(), metrics=[metric()]), **previous))), self.root)
        result = stage.summary(json.dumps(value))
        return result, investigation_context(stage.job["task"], result, self.job["parent"])

    def test_discovery_flattens_problem_and_separates_execution_records(self):
        stage = Discovery(self.job, self.root)
        value = dict(action="problem", **problem(), metrics=[metric()])
        with self.assertRaisesRegex(ValueError, "Analyze"):
            stage.summary(json.dumps(value))
        stage.state["analyses"] = [dict(returncode=1)]
        with self.assertRaisesRegex(ValueError, "Analyze"):
            stage.summary(json.dumps(value))
        stage.state["analyses"].append(dict(returncode=0))
        result = stage.summary(json.dumps(value))
        stage.close()
        self.assertIsInstance(result["problem"], str)
        self.assertEqual(read_json(stage.path), result)
        self.assertEqual(len(read_json(stage.execution_path)["analyses"]), 2)
        self.assertNotIn("state", result)
        self.assertNotIn("artifact", result)

    def test_evidence_requires_both_source_and_actual_observation(self):
        stage = Discovery(self.job, self.root)
        stage.state["analyses"] = [dict(returncode=0)]
        for invalid in (["some path"], [dict(source="analysis.json")], [dict(source="", observation="value")]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                stage.summary(json.dumps(dict(action="problem", **dict(problem(), evidence=invalid), metrics=[metric()])))

    def test_final_output_rejects_runtime_fields_and_old_wrappers(self):
        stage = Discovery(self.job, self.root)
        for field in ("state", "artifact", "analyses", "usage"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                stage.summary(json.dumps(dict(action="unresolved", reason="Weak evidence", **{field: {}})))
        with self.assertRaises(ValueError):
            stage.summary("This is not a JSON result")
        with self.assertRaises(ValueError):
            stage.summary(json.dumps(dict(action="problem", problem=problem(), metrics=[metric()])))

    def test_optional_fields_are_omitted_and_empty_placeholders_rejected(self):
        stage = Discovery(self.job, self.root)
        stage.state["analyses"] = [dict(returncode=0)]
        value = dict(action="problem", **dict(problem(), uncertainty=None), metrics=[metric()])
        result = stage.summary(json.dumps(value))
        self.assertNotIn("uncertainty", result)
        for key in ("window", "missing", "comparison"):
            self.assertNotIn(key, result["metrics"][0])
        with self.assertRaises(ValueError):
            stage.summary(json.dumps(dict(value, uncertainty="")))
        with self.assertRaisesRegex(ValueError, "unique"):
            stage.summary(json.dumps(dict(value, metrics=[metric(), metric()])))

    def test_hypotheses_require_evidence_predictions_and_verification(self):
        stage = Hypotheses(dict(self.job, role="hypotheses"), self.root)
        value = dict(action="hypotheses", hypotheses=hypotheses())
        for field in ("evidence", "verification", "predictions"):
            invalid = copy.deepcopy(value)
            del invalid["hypotheses"][0][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                stage.summary(json.dumps(invalid))
        with self.assertRaises(ValueError):
            stage.summary(json.dumps(dict(value, hypotheses=[hypotheses()[0]] * 2)))
        result = stage.summary(json.dumps(value))
        self.assertEqual(result, value)
        self.assertNotIn("code_targets", result["hypotheses"][0])
        with self.assertRaises(ValueError):
            stage.summary(json.dumps(dict(value, reason="Redundant outer explanation")))

    def test_minimal_diagnosis_reuses_upstream_hypotheses_and_metrics(self):
        value = self.result()
        result, context = self.investigate(value)
        self.assertEqual(result, value)
        self.assertNotIn("hypotheses", result)
        self.assertNotIn("metrics", result)
        self.assertEqual(context["hypotheses"], hypotheses())
        self.assertEqual(context["metrics"], [metric()])

    def test_diagnosis_requires_each_hypothesis_support_and_respects_refutation(self):
        value = self.result()
        value["tests"].append(dict(value["tests"][0], outcome="refuted"))
        with self.assertRaisesRegex(ValueError, "supporting tests"):
            self.investigate(value)
        value["tests"].append(dict(value["tests"][0], evidence=evidence("New supporting observation")))
        self.assertEqual(self.investigate(value)[0]["action"], "diagnosis")
        invalid = self.result()
        invalid["tests"][0]["hypothesis_id"] = "unknown"
        with self.assertRaisesRegex(ValueError, "reference"):
            self.investigate(invalid)

    def test_revised_mechanism_cannot_inherit_previous_support(self):
        previous = tests()
        revised = hypotheses()
        revised[0]["explanation"] = "A different causal mechanism"
        value = self.result(hypotheses=revised, tests=[previous[1]])
        with self.assertRaisesRegex(ValueError, "supporting tests"):
            self.investigate(value, tests=previous)
        value["tests"] = previous
        with self.assertRaisesRegex(ValueError, "inherit"):
            self.investigate(value, tests=previous)
        value["tests"] = [dict(previous[0], evidence=evidence("New distinguishing observation")), previous[1]]
        self.assertEqual(self.investigate(value, tests=previous)[0]["hypotheses"], revised)

    def test_unchanged_hypothesis_keeps_support_when_other_mechanism_changes(self):
        revised = hypotheses()
        revised[0]["explanation"] = "A new mechanism"
        value = self.result(hypotheses=revised, diagnosis=dict(diagnosis(), hypothesis_ids=["1"]),
                            tests=[dict(tests()[1], evidence=evidence("Additional check of mechanism 1"))])
        _, context = self.investigate(value, tests=tests())
        self.assertEqual({t["hypothesis_id"] for t in context["tests"]}, {"1"})

    def test_metric_updates_are_explicit_and_do_not_erase_existing_definitions(self):
        additional = dict(metric(), name="new_metric")
        result, context = self.investigate(self.result(metrics=[additional]))
        self.assertEqual([m["name"] for m in context["metrics"]], ["gas", "new_metric"])
        self.assertEqual(result["metrics"], [additional])

    def test_inconclusive_work_is_retained_when_returning_or_abstaining(self):
        for action in ("discovery", "unresolved"):
            value = dict(action=action, reason="More evidence is needed",
                         tests=[dict(tests()[0], outcome="inconclusive")], evidence=evidence("Coverage is insufficient"))
            self.assertEqual(self.investigate(value)[0], value)
        for action in ("resolution", "hypotheses"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.investigate(dict(action=action, reason="Invalid return"))

    def test_resolution_has_top_level_tasks_and_only_supported_targets(self):
        stage = Resolution(dict(self.job, role="resolution", max_tasks=2,
            task=dict(hypotheses=hypotheses(), diagnosis=dict(diagnosis(), hypothesis_ids=["0"]))), self.root)
        with self.assertRaisesRegex(ValueError, "supported"):
            stage.summary(json.dumps(dict(action="resolution", tasks=[task(hypothesis_id="1")])))
        value = dict(action="resolution", tasks=[task()])
        self.assertEqual(stage.summary(json.dumps(value)), value)
        with self.assertRaises(ValueError):
            stage.summary(json.dumps(dict(action="resolution", conclusion=dict(tasks=[task()]))))

    def test_sampling_rejects_invalid_requests_before_starting_work(self):
        stage = Discovery(self.job, self.root)
        for changes in (dict(mode="other"), dict(ai_build="Air"), dict(matches=3), dict(matches=True)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                stage.sample(**dict(dict(matches=1), **changes))
        self.assertFalse((self.root / "sampling").exists())
