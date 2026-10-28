"""Check runtime storage and Docker boundaries without games, paid models or Docker."""
import copy
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from runner.artifacts import read_json, write_json
from rsi.references import freeze_context
from rsi.experiment.config import validate_config
from rsi.tools.sandbox import docker_command, preflight_editor, prepare_editor_job
from rsi.experiment.workspace import allocate, remove_workspace


ROOT = Path(__file__).resolve().parents[1]


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_reference_reuse_changes_and_corruption(self):
        """Unchanged references are reused; changed or tampered bytes are distinguished."""
        write_json(self.root / "knowledge/example.json", dict(value=1))
        with patch("rsi.references.importlib.util.find_spec", return_value=None):
            first = freeze_context(self.root, self.root / "refs")
            stamp = (first / "knowledge/example.json").stat().st_mtime_ns
            self.assertEqual(freeze_context(self.root, self.root / "refs"), first)
            self.assertEqual((first / "knowledge/example.json").stat().st_mtime_ns, stamp)
            write_json(self.root / "knowledge/example.json", dict(value=2))
            second = freeze_context(self.root, self.root / "refs")
            self.assertNotEqual(first, second)
            self.assertEqual(read_json(first / "knowledge/example.json"), dict(value=1))
            write_json(second / "knowledge/example.json", dict(value=3))
            with self.assertRaisesRegex(ValueError, "identity") as error:
                freeze_context(self.root, self.root / "refs")
            self.assertIn("changed=['knowledge/example.json']", str(error.exception))
            self.assertIn("missing=[]; extra=[]", str(error.exception))
            self.assertIn("python=", str(error.exception))
            self.assertIn("expected=" + second.name, str(error.exception))

    def test_cleanup_rejects_other_directories(self):
        """Cleanup must never remove a source, history directory or arbitrary path."""
        owned = allocate(self.root, "work")
        write_json(owned / "workspace.json", {})
        outside = self.root / "versions/version"
        write_json(outside / "keep.json", {})
        for target in (outside, self.root / "work", self.root):
            with self.assertRaises(ValueError):
                remove_workspace(self.root, target)
        self.assertTrue((outside / "keep.json").exists())
        remove_workspace(self.root, owned)
        self.assertFalse(owned.exists())

    def docker_fixture(self):
        """Build explicit mount inputs, including host paths containing spaces."""
        project = self.root / "project with spaces"
        for name in ("rsi", "runner", "tests", "runtime/work/candidate/code", "refs", "runtime/sessions/session"):
            (project / name).mkdir(parents=True)
        evidence = project / "investigation.json"
        write_json(evidence, {})
        job = dict(role="editor", workspace=str(project / "runtime/work/candidate/code"), context=str(project / "refs"),
                   model=dict(api_key_env="RSI_TEST_API_KEY"), execution=dict(mode="docker", image="test:1"),
                   task=dict(task=dict(id="second_starport_bc_throughput"),
                             paths=dict(evidence=str(evidence))))
        return project, job, project / "runtime"

    def test_docker_mounts_and_path_translation(self):
        """Only this experiment's candidates and sessions are writable host mounts."""
        project, job, root = self.docker_fixture()
        original = copy.deepcopy(job)
        command = docker_command(project, job, root, "test-container")
        directory = root / "sessions/session"
        self.assertEqual(prepare_editor_job(job, root, directory), "/sessions/session/container_job.json")
        mounts = [command[i + 1] for i, part in enumerate(command) if part == "--mount"]
        self.assertEqual(len(mounts), 7)
        writable = [m.split("target=")[1] for m in mounts if not m.endswith(",readonly")]
        self.assertEqual(writable, ["/work", "/sessions"])
        self.assertNotIn(f"source={project},", " ".join(mounts))
        self.assertNotIn(".env", " ".join(command))
        self.assertEqual(command[command.index("-e") + 1], "RSI_TEST_API_KEY")
        mapped = read_json(directory / "container_job.json")
        self.assertEqual(mapped["workspace"], "/work/candidate/code")
        self.assertEqual(mapped["context"], "/refs")
        self.assertEqual(mapped["task"]["paths"]["evidence"], "/inputs/session.json")
        self.assertEqual(read_json(root / "inputs/session.json"), {})
        self.assertIn("--worker", command)
        self.assertIn("-i", command)
        self.assertEqual(mapped["task"]["task"]["id"], original["task"]["task"]["id"])
        self.assertEqual(job, original)

    def test_docker_preflight_has_no_local_fallback(self):
        """Selecting Docker fails explicitly before games if it is unavailable."""
        with patch("rsi.tools.sandbox._docker", side_effect=FileNotFoundError):
            preflight_editor(dict(mode="local"))
            with self.assertRaisesRegex(ValueError, "Docker unavailable"):
                preflight_editor(dict(mode="docker", image="test:1"))
        config = read_json(ROOT / "rsi/config.json")
        for execution in (dict(mode="unknown"), dict(mode="docker")):
            with self.assertRaises(ValueError):
                validate_config(dict(config, editor_execution=execution))

    def test_preflight_timeout_removes_its_container(self):
        ok = SimpleNamespace(returncode=0, stderr="")
        with patch("rsi.tools.sandbox._docker", side_effect=[ok, ok, subprocess.TimeoutExpired("docker", 30), ok]) as docker:
            with self.assertRaisesRegex(ValueError, "Docker unavailable"):
                preflight_editor(dict(mode="docker", image="test:1"))
            start = docker.call_args_list[2].args[0]
            name = start[start.index("--name") + 1]
            self.assertEqual(docker.call_args_list[3].args[0], ["rm", "--force", name])
