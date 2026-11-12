"""Verify official SDK capabilities, structured output and managed sampling offline."""
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from rsi.agents.discovery import PROMPT
from runtime.models.sessions import execute, ExperimentRuntime
from runtime.workspace.storage import project_path, read_json, write_json
from runtime.workspace.versions import Versions
from tests.fixtures import ROOT, metric, problem, hypotheses, diagnosis, plan, tests, sdk_messages


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, LITELLM_LOCAL_MODEL_COST_MAP="True", OPENHANDS_SUPPRESS_BANNER="1"))

    def session(self, job, directory, calls):
        from openhands.sdk.testing import TestLLM
        write_json(directory / "job.json", job)
        with patch("openhands.sdk.agent.base.has_vision_profile_available", return_value=False), redirect_stdout(StringIO()):
            return execute(directory / "job.json",
                llm_factory=lambda: TestLLM.from_messages(sdk_messages(calls)))

    def job(self, root, role="discovery"):
        from importlib import import_module
        parent = Versions(root / "outputs").freeze(ROOT)
        candidate = Versions(root / "outputs").candidate(parent, root / "candidate")
        write_json(root / "overview.json", dict(runs=[]))
        module = "rsi.agents." + role
        return dict(role=role, session=module, workspace=str(candidate),
            parent=parent, context=str(ROOT), prompt=import_module(module).PROMPT,
            max_analyses=2, sampling_remaining=1, sampling_styles=["Macro"],
            max_tasks=3, model=read_json(ROOT / "rsi/config.json")["model"],
            task=dict(problem=problem(), hypotheses=hypotheses(), diagnosis=diagnosis(),
                      evidence_index=str(root / "overview.json"), conditions=dict(styles=["Macro"])))

    def command(self, script):
        if sys.platform == "win32":
            return f'& "{sys.executable}" -B "{script}"'
        import shlex
        return f"{shlex.quote(sys.executable)} -B {shlex.quote(str(script))}"

    def test_official_terminal_and_file_reads_produce_validated_final_json(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = self.job(root)
            job["task"]["unused_metadata"] = "PRIVATE_JOB_TASK_FIELD"
            script = root / "session/analysis/inspect.py"
            calls = [
                ("file_editor", dict(command="create", path=str(script), file_text="print(600.0)\n")),
                ("terminal", dict(command=self.command(script))),
                ("file_editor", dict(command="view",
                    path=str(project_path(job["parent"]["source_root"]) / "bot/components/tactics/bc_rush.py"),
                    view_range=[1, 20])),
                ("finish", dict(message=json.dumps(dict(action="problem", **problem(), metrics=[metric()])))),
            ]
            from openhands.sdk.conversation import LocalConversation
            send_message = LocalConversation.send_message
            prompts = []
            def capture(conversation, message, *args, **kwargs):
                prompts.append(message)
                return send_message(conversation, message, *args, **kwargs)
            with patch.object(LocalConversation, "send_message", autospec=True, side_effect=capture):
                result = self.session(job, root / "session", calls)
            self.assertEqual(result["status"], "ok", result.get("reason"))
            self.assertIn("<bot_readme>", prompts[0])
            self.assertNotIn("PRIVATE_JOB_TASK_FIELD", prompts[0])
            self.assertNotIn("Task data (JSON)", prompts[0])
            self.assertNotIn("$defs", prompts[0])
            self.assertEqual(result["value"]["problem"], problem()["problem"])
            analyses = read_json(project_path(result["execution_path"]))["analyses"]
            self.assertEqual(len(analyses), 1)
            self.assertEqual(analyses[0]["returncode"], 0)
            output = read_json(project_path(analyses[0]["artifact"]))
            self.assertIn("600.0", str(output))
            self.assertTrue(project_path(result["result_path"]).is_file())

    def test_sampling_returns_to_same_sdk_conversation_before_final_result(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = self.job(root)
            script = root / "session/analysis/after_sample.py"
            calls = [
                ("file_editor", dict(command="str_replace", path="bot/telemetry/logger.py",
                    old_str="        recorder = self.recorder",
                    new_str="        recorder = self.recorder\n"
                        "        recorder.add_series('gas', fields=[('gas', '<i8')], "
                        "sample=lambda game: (game.vespene,), every_frames=112)")),
                ("run_sampling", dict(matches=1)),
                ("file_editor", dict(command="create", path=str(script), file_text="print('analysis after sampling')\n")),
                ("terminal", dict(command=self.command(script))),
                ("finish", dict(message=json.dumps(dict(action="problem", **problem(), metrics=[metric()])))),
            ]
            from runtime.workspace.checks import validate_sampling
            def sample(stage, **request):
                self.assertTrue(validate_sampling(job["parent"], project_path(job["workspace"]))["passed"])
                self.assertEqual(request["matches"], 1)
                return dict(sampling_remaining=0, evidence_index="new_evidence.json")
            with patch("rsi.agents.common.Stage.sample", autospec=True, side_effect=sample) as sampling:
                result = self.session(job, root / "session", calls)
            self.assertEqual(result["status"], "ok", result.get("reason"))
            sampling.assert_called_once()
            self.assertEqual(len(read_json(project_path(result["execution_path"]))["analyses"]), 1)

    def test_roles_use_final_json_without_submission_tools(self):
        from importlib import import_module
        values = dict(
            hypotheses=dict(action="hypotheses", hypotheses=hypotheses()),
            investigation=dict(action="diagnosis", diagnosis=diagnosis(), tests=tests()),
            resolution=dict(action="resolution", **plan()))
        with TemporaryDirectory() as temporary:
            for role, value in values.items():
                with self.subTest(role=role):
                    root = Path(temporary) / role
                    job = self.job(root, role)
                    names = []
                    module = import_module(job["session"])
                    create_tools = module.create_tools
                    def capture(*args):
                        tools = create_tools(*args)
                        names.extend(t.name for t in tools)
                        if role == "investigation":
                            from openhands.tools.file_editor.definition import FileEditorAction
                            from rsi.agents.common import SamplingInput
                            editor = next(t for t in tools if t.name == "file_editor")
                            sampling = next(t for t in tools if t.name == "run_sampling")
                            file_keys = editor.declared_resources(FileEditorAction(
                                command="view", path="bot/telemetry/logger.py")).keys
                            sample_keys = sampling.declared_resources(SamplingInput(matches=1)).keys
                            self.assertTrue(set(file_keys).intersection(sample_keys))
                        return tools
                    with patch.object(module, "create_tools", side_effect=capture):
                        result = self.session(job, root / "session", [("finish", dict(message=json.dumps(value)))])
                    self.assertEqual(result["status"], "ok", result.get("reason"))
                    self.assertEqual(set(names), {"file_editor", "glob", "grep", "terminal"}
                        | ({"run_sampling"} if role == "investigation" else set()))
                    self.assertEqual(result["value"]["action"], value["action"])

    def test_terminal_budget_counts_commands_but_not_polling_and_survives_revisit(self):
        from openhands.tools.terminal.definition import TerminalAction, TerminalObservation
        from rsi.agents.discovery import Discovery
        from runtime.models.tools import ResearchTerminalExecutor
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = Discovery(dict(role="discovery", max_analyses=1), root)
            delegate = Mock(side_effect=[
                TerminalObservation.from_text(text="running", command="python analysis.py", exit_code=-1),
                TerminalObservation.from_text(text="done", command="", exit_code=0)])
            executor = ResearchTerminalExecutor(delegate, stage)
            executor(TerminalAction(command="python analysis.py"))
            executor(TerminalAction(command=""))
            self.assertEqual(len(stage.state["analyses"]), 1)
            self.assertEqual(stage.state["analyses"][0]["returncode"], 0)
            resumed = Discovery(dict(role="discovery", max_analyses=1, analysis_records=stage.state["analyses"]), root / "resumed")
            rejected = ResearchTerminalExecutor(delegate, resumed)(TerminalAction(command="python another.py"))
            self.assertTrue(rejected.is_error)
            self.assertEqual(delegate.call_count, 2)

    def test_implementation_uses_ordered_input_and_keeps_text_reports(self):
        from rsi.agents.resolution import make_implementation_job
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = self.job(root, "resolution")
            evidence_path = root / "investigation.json"
            write_json(evidence_path, dict(action="diagnosis", tests=tests(), diagnosis=diagnosis()))
            research = dict(problem=problem(), conditions=dict(styles=["Macro"]),
                            hypotheses=hypotheses(), diagnosis=diagnosis(), tests=tests())
            job = make_implementation_job(project_path(base["workspace"]), research, plan()["tasks"][0],
                context=ROOT, evidence_path=evidence_path)
            job["model"] = base["model"]
            calls = [("file_editor", dict(command="view", path="bot/components/tactics/bc_rush.py",
                                         view_range=[1, 10])),
                     ("finish", dict(message="Offline implementation report"))]
            result = self.session(job, root / "session", calls)
            self.assertEqual(result["status"], "ok", result.get("reason"))
            self.assertEqual(result["value"], "Offline implementation report")
            self.assertNotIn("execution_path", result)

    def test_sampling_bridge_preserves_requests_and_updates_remaining_budget(self):
        from rsi.agents.discovery import Discovery
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = Discovery(dict(role="discovery", sampling_remaining=1,
                sampling_styles=["Macro"]), root)
            values = []
            worker = threading.Thread(target=lambda: values.append(stage.sample(1, "repeat", "Macro")), daemon=True)
            worker.start()
            request = root / "sampling/001/request.json"
            deadline = time.monotonic() + 3
            while not request.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(read_json(request), dict(matches=1, mode="repeat", ai_build="Macro"))
            write_json(request.with_name("response.json"), dict(sampling_remaining=0, evidence="evidence.json"))
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(values[0]["evidence"], "evidence.json")
            with self.assertRaisesRegex(ValueError, "budget"):
                stage.sample(1)

    def test_official_terminal_records_failures_and_enforces_command_budget(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = self.job(root)
            script = root / "session/analysis/failing.py"
            calls = [
                ("file_editor", dict(command="create", path=str(script), file_text="raise ValueError('bad analysis')\n")),
                ("terminal", dict(command=self.command(script))),
                ("terminal", dict(command=self.command(script))),
                ("terminal", dict(command=self.command(script))),
                ("finish", dict(message='{"action":"unresolved","reason":"Analysis failed"}')),
            ]
            result = self.session(job, root / "session", calls)
            self.assertEqual(result["status"], "ok", result.get("reason"))
            analyses = read_json(project_path(result["execution_path"]))["analyses"]
            self.assertEqual(len(analyses), 2)
            self.assertTrue(all(item["returncode"] not in (0, None) for item in analyses))
            self.assertIn("bad analysis", str(read_json(project_path(analyses[0]["artifact"]))))

    def test_runtime_services_sampling_and_excludes_wait_from_model_wall_timeout(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = ExperimentRuntime(ROOT, root)
            self.addCleanup(runtime.close)
            directory = root / "session"
            clock = [0.0]
            def submit(text):
                write_json(directory / "sampling/001/request.json", dict(matches=1))
            def poll():
                if (directory / "sampling/001/response.json").exists():
                    write_json(directory / "response.json", dict(status="ok", value="finished", usage={}))
                    (directory / "done").touch()
                return None
            process = SimpleNamespace(stdin=SimpleNamespace(write=submit, flush=lambda: None),
                poll=poll, wait=Mock(side_effect=subprocess.TimeoutExpired("worker", 1)), args=["worker"])
            def sample(request):
                clock[0] += 100
                return dict(sampling_remaining=0, evidence_index="evidence.json")
            with patch.object(runtime, "_worker", return_value=process), \
                    patch("runtime.models.sessions.time.monotonic", side_effect=lambda: clock[0]):
                result = runtime.call_session(dict(model=dict(wall_timeout=10), wall_remaining=1000),
                    directory, on_sampling=sample)
            self.assertEqual(result["status"], "ok", result)
            self.assertTrue((directory / "sampling/001/response.json").is_file())

    def test_editor_restricts_writes_and_allows_official_views_of_references(self):
        from runtime.models.tools import CandidateExecutor
        with TemporaryDirectory() as temporary:
            delegate = Mock()
            executor = CandidateExecutor(delegate, temporary)
            for path in ("../outside.py", "bot/core/actions.py", "bot/components/tasks/old.py"):
                self.assertTrue(executor(SimpleNamespace(path=path, command="create")).is_error)
            delegate.assert_not_called()
            for path, command in (("bot/components/tactics/bc_rush.py", "str_replace"),
                                  (str(ROOT / "knowledge/README.md"), "view")):
                action = SimpleNamespace(path=path, command=command)
                action.model_copy = lambda *, update, action=action: SimpleNamespace(**dict(vars(action), **update))
                executor(action)
            self.assertEqual(delegate.call_count, 2)
