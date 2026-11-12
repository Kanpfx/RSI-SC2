"""Check readable model inputs and the boundary between research and runtime data."""
from importlib import import_module
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest

from rsi.agents.resolution import make_implementation_job
from runtime.workspace.storage import write_json
from tests.fixtures import metric, problem, hypotheses, diagnosis, task, tests


class PromptTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.parent = self.root / "parent"
        self.workspace = self.root / "candidate"
        for directory, text in ((self.parent, "# Parent Bot\nPARENT ARCHITECTURE"),
                                (self.workspace, "# Candidate Bot\nCANDIDATE ARCHITECTURE")):
            (directory / "bot").mkdir(parents=True)
            (directory / "bot/README.md").write_text(text, encoding="utf-8")
        self.index = self.root / "overview.json"
        write_json(self.index, dict(runs=[dict(run_id="DO_NOT_INLINE_RUN", datasets={})] * 100))
        self.conditions = dict(map="AcropolisAIE", opponent_race="T", difficulty="Hard",
                               styles=["Macro", "Rush"], game_time_limit=1800, wall_timeout=600)

    def job(self, role):
        return dict(role=role, parent=dict(source_root=str(self.parent)), context=str(self.root / "refs"),
            workspace=str(self.workspace), max_analyses=16, max_tasks=3, sampling_remaining=4,
            analysis_records=[dict(command="PRIVATE_COMMAND_RECORD", returncode=0)],
            model=dict(max_total_tokens=1500000), task=dict(conditions=self.conditions,
                evidence_index=str(self.index), problem=problem(), metrics=[metric()],
                hypotheses=hypotheses(), tests=tests(), diagnosis=diagnosis()))

    def prepare(self, job):
        module = import_module("rsi.agents." + job["role"])
        stage, prompt = module.prepare_session(job, self.root / "session", module.PROMPT)
        return stage, prompt

    def section(self, prompt, name):
        return re.search(f"<{name}>\\n(.*?)\\n</{name}>", prompt, re.S).group(1)

    def test_research_roles_follow_reading_order_and_keep_full_readme_once(self):
        for role in ("discovery", "hypotheses", "investigation", "resolution"):
            with self.subTest(role=role):
                _, prompt = self.prepare(self.job(role))
                module = import_module("rsi.agents." + role)
                self.assertTrue(prompt.startswith(module.PROMPT.strip()))
                tags = ["brief", "bot_readme", "research_context", "working_rules", "output_requirements"]
                positions = [prompt.index("<" + tag + ">") for tag in tags]
                self.assertEqual(positions, sorted(positions))
                self.assertEqual(prompt.count("PARENT ARCHITECTURE"), 1)
                self.assertNotIn("CANDIDATE ARCHITECTURE", prompt)
                for private in ("$defs", "additionalProperties", "PRIVATE_COMMAND_RECORD", "max_total_tokens",
                                "DO_NOT_INLINE_RUN", "Task data (JSON)"):
                    self.assertNotIn(private, prompt)
                brief = json.loads(self.section(prompt, "brief"))
                self.assertEqual(brief["paths"]["evidence_index"], str(self.index))
                self.assertNotIn("wall_timeout", brief["conditions"])
                self.assertIn("15", self.section(prompt, "working_rules"))

    def test_role_materials_contain_only_the_relevant_research(self):
        expected = dict(discovery={"metrics"}, hypotheses={"problem", "metrics"},
            investigation={"problem", "hypotheses", "metrics", "tests"},
            resolution={"problem", "hypotheses", "diagnosis", "tests"})
        for role, keys in expected.items():
            with self.subTest(role=role):
                _, prompt = self.prepare(self.job(role))
                context = json.loads(self.section(prompt, "research_context"))
                self.assertEqual(set(context), keys)

    def test_first_discovery_omits_empty_research_context(self):
        job = self.job("discovery")
        job["task"] = dict(conditions=self.conditions, evidence_index=str(self.index))
        _, prompt = self.prepare(job)
        self.assertNotIn("<research_context>", prompt)
        self.assertIn("Sampling matches remaining: 4", prompt)

    def test_feedback_and_optional_experience_are_explicit_materials(self):
        job = self.job("discovery")
        feedback = dict(reason="Recheck the comparison", problem=problem())
        job["task"].update(feedback=feedback, experience=str(self.root / "memory.json"))
        _, prompt = self.prepare(job)
        context = json.loads(self.section(prompt, "research_context"))
        self.assertEqual(context["feedback"], feedback)
        self.assertEqual(context["experience"], str(self.root / "memory.json"))

    def test_implementation_uses_same_layout_with_candidate_readme_and_one_task(self):
        research = dict(problem=problem(), conditions=self.conditions, hypotheses=hypotheses(),
                        diagnosis=diagnosis(), tests=tests())
        job = make_implementation_job(self.workspace, research, task(), context=self.root / "refs",
                                      evidence_path=self.root / "investigation.json")
        _, prompt = self.prepare_implementation(job)
        self.assertEqual(prompt.count("CANDIDATE ARCHITECTURE"), 1)
        self.assertNotIn("PARENT ARCHITECTURE", prompt)
        context = json.loads(self.section(prompt, "research_context"))
        self.assertEqual(context["task"]["id"], "T1")
        self.assertEqual([h["id"] for h in context["hypotheses"]], ["0"])
        self.assertEqual([t["hypothesis_id"] for t in context["tests"]], ["0"])
        self.assertIn("problem", context)
        self.assertNotIn("issue", context)
        self.assertNotIn("feedback", context)
        self.assertNotIn("validator_feedback", job["task"])
        self.assertNotIn("Return one JSON object", self.section(prompt, "output_requirements"))
        self.assertNotIn("Remaining new Terminal commands", prompt)

    def prepare_implementation(self, job):
        from rsi.agents.resolution import prepare_session
        return prepare_session(job, self.root / "session", job["prompt"])

    def test_implementation_repair_gets_failed_checks_only(self):
        research = dict(problem=problem(), conditions=self.conditions, hypotheses=hypotheses(),
                        diagnosis=diagnosis(), tests=tests())
        feedback = dict(passed=False, checks=[dict(name="scope", passed=True),
            dict(name="import", passed=False, output="x" * 5000)])
        job = make_implementation_job(self.workspace, research, task(), feedback, self.root / "refs",
                                      evidence_path=self.root / "investigation.json")
        _, prompt = self.prepare_implementation(job)
        context = json.loads(self.section(prompt, "research_context"))
        checks = context["feedback"]["checks"]
        self.assertEqual([check["name"] for check in checks], ["import"])
        self.assertEqual(len(checks[0]["output"]), 4000)
