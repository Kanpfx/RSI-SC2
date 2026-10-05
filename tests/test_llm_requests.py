"""Stage-specific request settings and reasoning response handling."""

import json
import unittest
from unittest.mock import MagicMock, patch

from agent.config import LLMConfig
from agent.harness.llm import LLMClient


class LLMRequestTests(unittest.TestCase):
    def setUp(self):
        self.client = LLMClient(LLMConfig(
            model="test", base_url="https://openrouter.ai/api/v1", api_key="test",
        ))

    def test_stage_settings(self):
        messages = [{"role": "user", "content": "observation"}]
        planner = self.client._request_body(messages, phase="planner")
        excutor = self.client._request_body(messages, phase="excutor")
        self.assertEqual(planner, {
            "model": "test", "messages": messages, "max_tokens": 768,
            "reasoning": {"effort": "low"}, "provider": {"sort": "latency"},
        })
        self.assertEqual(excutor, {
            "model": "test", "messages": messages, "max_tokens": 256,
            "temperature": 0.0, "reasoning": {"enabled": False},
            "provider": {"sort": "latency"},
        })

    def test_gpt6_luna_omits_unsupported_temperature(self):
        client = LLMClient(LLMConfig(model="openai/gpt-6-luna", base_url="https://openrouter.ai/api/v1"))
        self.assertNotIn("temperature", client._request_body([], phase="excutor"))
        self.assertEqual(client._request_body([], phase="planner")["reasoning"], {"effort": "low"})

    def response(self, content, finish_reason="stop"):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"choices": [{
            "message": {"content": content, "reasoning": "# actions\nInvalid()"},
            "finish_reason": finish_reason,
        }]}).encode("utf-8")
        return response

    def test_reads_only_final_content(self):
        content = "## Current phase\nopening\n## Guidance\nPrepare expansion."
        with patch("agent.harness.llm.request.urlopen", return_value=self.response(content)):
            self.assertEqual(self.client._complete_sync([], phase="planner"), content)

    def test_rejects_empty_or_truncated_content(self):
        for content, finish_reason in [("", "stop"), ("# actions\nMove(", "length")]:
            with self.subTest(content=content):
                with patch("agent.harness.llm.request.urlopen", return_value=self.response(content, finish_reason)):
                    with self.assertRaises(ValueError):
                        self.client._complete_sync([], phase="excutor")
