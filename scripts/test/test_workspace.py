"""Check runtime storage and Docker boundaries without games, paid models or Docker."""
import copy
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from runner.artifacts import read_json, write_json
from runner.versions import inventory
from rsi.references import freeze_context
from rsi.tools.config import validate_config
from rsi.tools.sandbox import docker_command, editor_container, preflight_editor, prepare_editor_job
from rsi.workspace import allocate, preflight_storage, remove_workspace


ROOT = Path(__file__).resolve().parents[2]


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_readable_paths_keep_existing_runs_on_same_second(self):
        """Timestamp collisions add a counter and numbered children remain readable."""
        with patch('rsi.workspace.datetime') as clock:
            clock.now.return_value.strftime.return_value = '20261027_160000'
            first = allocate(self.root, 'experiments')
            first.mkdir()
            second = allocate(self.root, 'experiments')
        self.assertEqual(first.name, '20261027_160000')
        self.assertEqual(second.name, '20261027_160000_02')
        self.assertEqual(allocate(first, 'iterations').name, 'iteration_01')

    def test_reference_reuse_changes_and_corruption(self):
        """Unchanged references are reused; changed or tampered bytes are distinguished."""
        write_json(self.root / "knowledge/example.json", dict(value=1))
        with patch("rsi.references.importlib.util.find_spec", return_value=None):
            first = freeze_context(self.root, self.root / "refs", shared=True)
            stamp = (first / "knowledge/example.json").stat().st_mtime_ns
            self.assertEqual(freeze_context(self.root, self.root / "refs", shared=True), first)
            self.assertEqual((first / "knowledge/example.json").stat().st_mtime_ns, stamp)
            write_json(self.root / "knowledge/example.json", dict(value=2))
            second = freeze_context(self.root, self.root / "refs", shared=True)
            self.assertNotEqual(first, second)
            self.assertEqual(read_json(first / "knowledge/example.json"), dict(value=1))
            write_json(second / "knowledge/example.json", dict(value=3))
            with self.assertRaisesRegex(ValueError, "identity") as error:
                freeze_context(self.root, self.root / "refs", shared=True)
            self.assertIn("changed=['knowledge/example.json']", str(error.exception))
            self.assertIn("missing=[]; extra=[]", str(error.exception))
            self.assertIn("python=", str(error.exception))
            self.assertIn("expected=" + second.name, str(error.exception))

    def test_sdk_event_storage_at_real_project_depth(self):
        """Use the SDK file writer under this checkout's full Windows path."""
        checks = ROOT / "runtime/checks"
        checks.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=checks) as temporary:
            root = Path(temporary) / "runtime/rsi"
            preflight_storage(root)
            self.assertEqual(list((root / "sessions").iterdir()), [])
            self.assertEqual(list((root / "work").iterdir()), [])

    @unittest.skipUnless(sys.platform == "win32", "Windows ACL regression")
    def test_shared_reference_inherits_parent_permissions(self):
        """Publishing a snapshot must not preserve tempfile's private Windows ACL."""
        write_json(self.root / "knowledge/example.json", dict(value=1))
        with patch("rsi.references.importlib.util.find_spec", return_value=None):
            snapshot = freeze_context(self.root, self.root / "refs", shared=True)
        literal = str(snapshot).replace("'", "''")
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                 f"(Get-Acl -LiteralPath '{literal}').AreAccessRulesProtected"],
                                capture_output=True, text=True, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_inventory_propagates_directory_access_errors(self):
        """An unreadable source must never be hashed as an empty directory."""
        with patch.object(Path, "iterdir", side_effect=PermissionError("fixture access denied")):
            with self.assertRaisesRegex(PermissionError, "fixture access denied"):
                inventory(self.root)

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
        for name in ("rsi", "runner", "scripts/test", "runtime/work/candidate/code", "refs", "runtime/sessions/session"):
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

    def test_container_is_removed_after_interruption(self):
        """Interrupting a Docker client must still remove its named container."""
        project, job, directory = self.docker_fixture()
        with patch("rsi.tools.sandbox._docker") as docker:
            docker.return_value.returncode = 0
            with self.assertRaises(KeyboardInterrupt):
                with editor_container(project, job, directory) as command:
                    name = command[command.index("--name") + 1]
                    raise KeyboardInterrupt
            docker.assert_called_once_with(["rm", "--force", name])

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
