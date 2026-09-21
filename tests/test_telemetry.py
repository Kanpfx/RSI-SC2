"""System log correlation and loaded-input snapshots, without network calls."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run
from agent.config import LLMConfig
from agent.logger.recorder import Telemetry
from agent.runtime.llm import LLMClient, ModelAgent


class SystemTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_round_requests_retries_and_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("agent.logger.recorder.runtime_manifest", return_value={}):
                trace = Telemetry(directory=root)
            trace.snapshot_context({"general.md": "guidance", "test.md": "tactic"}, {"model": "test"})
            trace.observation(iteration=4, game_loop=80, game_time=3.5, observation="OBS")
            replies = iter([OSError("temporary"), "working", "# actions\nBuildWorkers(to_count=22)"])
            messages = [{"role": "user", "content": "original input"}]
            client = LLMClient(LLMConfig(model="test", base_url="https://example.invalid", api_key="secret"))

            def complete_sync(messages, *, trace, iteration, attempt, request_started):
                reply = next(replies)
                if isinstance(reply, Exception):
                    raise reply
                trace.model_conversation(stage="response", iteration=iteration, attempt=attempt, reply=reply)
                return reply

            with patch.object(client, "_complete_sync", side_effect=complete_sync):
                await ModelAgent(client).run(
                    messages, trace=trace, iteration=4,
                    action_message={"content": "actions input"}, action_system="system",
                    on_working=lambda text: trace.working_memory(text, request_iteration=4),
                )
            system = root / "system"
            model = [json.loads(line) for line in (system / "model.jsonl").read_text().splitlines()]
            events = [json.loads(line) for line in (system / "events.jsonl").read_text().splitlines()]
            requests = [row for row in model if row["stage"] == "request"]
            self.assertEqual([row["request_phase"] for row in requests], ["working", "actions"])
            self.assertNotEqual(requests[0]["request_id"], requests[1]["request_id"])
            self.assertEqual(requests[0]["request_body"]["messages"], messages)
            attempts = [row for row in events if row["event"] == "transport_attempt_started"]
            self.assertEqual([row["attempt"] for row in attempts], [1, 2, 1])
            self.assertEqual(len({row["attempt_id"] for row in attempts}), 3)
            for row in model + events:
                self.assertEqual(row["run_id"], trace.run_id)
                self.assertEqual(row["decision_id"], "d4")
                self.assertEqual(row["observation_game_loop"], 80)
                self.assertEqual(row["observation_game_time"], 3.5)
            metadata = json.loads((system / "metadata.json").read_text())
            self.assertEqual(metadata["log_schema_version"], 3)
            self.assertIn("sha256", metadata["context_snapshot"])
            self.assertFalse((system / "snapshots").exists())
            self.assertNotIn("action_catalog_snapshot", metadata)
            self.assertEqual((root / "context/general.md").read_text(), "guidance")
            self.assertNotIn("secret", (system / "model.jsonl").read_text())
