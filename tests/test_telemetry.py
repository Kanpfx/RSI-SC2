"""Flat match log correlation and settings, without network calls."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run
from agent.config import LLMConfig
from agent.logger.recorder import Telemetry
from agent.harness.llm import LLMClient, ModelAgent


class SystemTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_serial_requests_and_flat_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("agent.logger.recorder.runtime_manifest", return_value={}):
                trace = Telemetry(directory=root)
            trace.save_settings({"model": "test"})
            trace.observation(iteration=4, game_loop=80, game_time=3.5, observation="OBS")
            replies = iter(["working", "# actions\nBuildWorkers(to_count=22)"])
            messages = [{"role": "user", "content": "original input"}]
            client = LLMClient(LLMConfig(model="test", base_url="https://example.invalid", api_key="secret"))

            def complete_sync(messages, *, phase, timeout):
                reply = next(replies)
                return json.dumps({'choices': [{'message': {'content': reply}, 'finish_reason': 'stop'}]}), 200

            with patch.object(client, "_complete_sync", side_effect=complete_sync):
                working = await client.complete(messages, phase='planner', trace=trace.request_trace(4, 'planner'), iteration=4)
                trace.working_memory(working, request_iteration=4)
                trace.observation(iteration=5, game_loop=80, game_time=3.5, observation='OBS')
                await ModelAgent(client).run(
                    [
                        {"role": "system", "content": "system"},
                        {"role": "user", "content": working + "\nactions input"},
                    ],
                    trace=trace, iteration=5, working=working,
                )
            system = root
            model = [json.loads(line) for line in (system / "model.jsonl").read_text().splitlines()]
            events = [json.loads(line) for line in (system / "events.jsonl").read_text().splitlines()]
            requests = [row for row in model if row["stage"] == "request"]
            self.assertEqual([row["request_phase"] for row in requests], ["planner", "excutor"])
            self.assertNotEqual(requests[0]["request_id"], requests[1]["request_id"])
            self.assertEqual(requests[0]["request_body"]["messages"], messages)
            attempts = [row for row in events if row["event"] == "transport_attempt_started"]
            self.assertEqual([row["attempt"] for row in attempts], [1, 1])
            self.assertEqual(len({row["attempt_id"] for row in attempts}), 2)
            self.assertEqual([row['timeout_seconds'] for row in requests], [10.0, 5.0])
            self.assertTrue(all(row['max_retries'] == 0 for row in requests))
            responses = [row for row in model if row['stage'] == 'response']
            self.assertTrue(all(row['request_elapsed_seconds'] >= 0 for row in responses))
            for row in model + events:
                self.assertEqual(row["run_id"], trace.run_id)
                self.assertEqual(row["decision_id"], 'd5' if row.get('request_phase') == 'excutor' else 'd4')
                self.assertEqual(row["observation_game_loop"], 80)
                self.assertEqual(row["observation_game_time"], 3.5)
            metadata = json.loads((system / "metadata.json").read_text())
            self.assertEqual(metadata["log_schema_version"], 3)
            self.assertNotIn("context_snapshot", metadata)
            self.assertFalse((system / "snapshots").exists())
            self.assertNotIn("action_catalog_snapshot", metadata)
            self.assertFalse((root / "context").exists())
            self.assertFalse((root / "system").exists())
            self.assertEqual(json.loads((root / "settings.json").read_text()), {"model": "test"})
            self.assertNotIn("secret", (system / "model.jsonl").read_text())
