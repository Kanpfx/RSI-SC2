"""Check runtime storage, configuration and shared worker lifecycle."""
from pathlib import Path
import json
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from runtime.workspace.storage import read_json, write_json
from runtime.workspace.references import freeze_context
from rsi.config import validate_config
from runtime.workspace.candidates import allocate, remove_workspace
from runtime.models.sessions import ExperimentRuntime


ROOT = Path(__file__).resolve().parents[1]


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_match_parallelism_is_independent_of_trial_count(self):
        config = read_json(ROOT / "rsi/config.json")
        validate_config(dict(config, trials=5, match_parallelism=1))
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "match_parallelism"):
                validate_config(dict(config, match_parallelism=value))

    def test_api_snapshots_ignore_pending_knowledge_and_detect_corruption(self):
        api = self.root / "knowledge/python-sc2/sc2/bot_ai.py"
        api.parent.mkdir(parents=True)
        api.write_text("# API version 1\n", encoding="utf-8")
        (api.parent.parent / "pyproject.toml").write_text('[project]\nversion = "7.1.3"\n', encoding="utf-8")
        write_json(self.root / "knowledge/example.json", dict(value=1))
        with patch("runtime.workspace.references.PROJECT", self.root):
            first = freeze_context(self.root, self.root / "refs")
            self.assertFalse((first / "knowledge").exists())
            self.assertTrue((first / "api/bot_ai.py").is_file())
            write_json(self.root / "knowledge/example.json", dict(value=2))
            self.assertEqual(freeze_context(self.root, self.root / "refs"), first)
            api.write_text("# API version 2\n", encoding="utf-8")
            second = freeze_context(self.root, self.root / "refs")
            self.assertNotEqual(first, second)
            (second / "api/bot_ai.py").write_text("corrupt", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "identity"):
                freeze_context(self.root, self.root / "refs")

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

    def test_all_sessions_reuse_one_worker_and_close_it_once(self):
        process = Mock()
        process.poll.return_value = None
        with patch("runtime.models.sessions.subprocess.Popen", return_value=process) as launch:
            with ExperimentRuntime(self.root, self.root / "runtime") as runtime:
                self.assertIs(runtime._worker(), process)
                self.assertIs(runtime._worker(), process)
            runtime.close()
        launch.assert_called_once()
        self.assertIn("runtime.models.sessions", launch.call_args.args[0])
        process.stdin.close.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI is not installed")
    def test_compose_has_one_container_and_only_outputs_are_writable_on_host(self):
        command = ["docker", "compose", "-f", str(ROOT / "compose.yaml"), "config",
                   "--format", "json", "--no-env-resolution", "--no-interpolate"]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=15, check=True)
        services = json.loads(result.stdout)["services"]
        self.assertEqual(set(services), {"experiment"})
        service = services["experiment"]
        self.assertEqual(service["image"], "aiarena/arenaclient-sc2:v0.8.0")
        self.assertEqual(service["pull_policy"], "never")
        self.assertNotIn("build", service)
        self.assertEqual(service["entrypoint"], ["/opt/rsi/python/bin/python3", "-u", "-B", "-m", "rsi"])
        self.assertTrue(service["read_only"])
        self.assertTrue(service["init"])
        self.assertFalse(service.get("privileged", False))
        self.assertFalse(service.get("ports"))
        writable = [mount["target"] for mount in service["volumes"] if not mount.get("read_only", False)]
        self.assertEqual(writable, ["/app/outputs"])
        self.assertEqual({mount["target"] for mount in service["volumes"]},
                         {"/opt/rsi", "/app/bot", "/app/rsi", "/app/runtime", "/app/knowledge",
                          "/app/tests", "/app/pyproject.toml", "/app/outputs"})
        environment = next(mount for mount in service["volumes"] if mount["target"] == "/opt/rsi")
        self.assertEqual(environment["type"], "volume")
        self.assertTrue(environment["read_only"])
