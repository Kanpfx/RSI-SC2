"""Stage-specific request settings and reasoning response handling."""

import asyncio
import json
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

from agent.config import LLMConfig
from agent.harness.llm import LLMClient, LLMClientError


class LLMRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = LLMClient(LLMConfig(
            model="test", base_url="https://openrouter.ai/api/v1", api_key="test",
        ))

    def test_stage_settings(self):
        messages = [{"role": "user", "content": "observation"}]
        planner = self.client._request_body(messages, phase="planner")
        excutor = self.client._request_body(messages, phase="excutor")
        self.assertEqual(planner, {
            "model": "test", "messages": messages, "max_tokens": 1024,
            "reasoning": {"effort": "low"},
        })
        self.assertEqual(excutor, {
            "model": "test", "messages": messages, "max_tokens": 1024,
            "reasoning": {"effort": "low"},
        })

    def test_gpt6_luna_omits_unsupported_temperature(self):
        client = LLMClient(LLMConfig(model="openai/gpt-6-luna", base_url="https://openrouter.ai/api/v1"))
        self.assertNotIn("temperature", client._request_body([], phase="excutor"))
        self.assertEqual(client._request_body([], phase="planner")["reasoning"], {"effort": "low"})

    def test_env_uses_shared_settings(self):
        with patch.dict('os.environ', {
            'LLM_MODEL': 'anthropic/claude-haiku-5.5',
            'LLM_BASE_URL': 'https://openrouter.ai/api/v1/',
            'LLM_API_KEY': 'test',
        }, clear=True):
            config = LLMConfig.from_env()
        client = LLMClient(config)
        planner = client._request_body([], phase='planner')
        excutor = client._request_body([], phase='excutor')
        self.assertEqual(planner['reasoning'], {'effort': 'low'})
        self.assertEqual(planner['max_tokens'], 1024)
        self.assertEqual(excutor['reasoning'], {'effort': 'low'})
        self.assertEqual(excutor['max_tokens'], 1024)
        for body in (planner, excutor):
            self.assertNotIn('temperature', body)
            self.assertNotIn('provider', body)
        self.assertEqual(config.planner_timeout_seconds, 10.0)
        self.assertEqual(config.excutor_timeout_seconds, 10.0)

    def test_other_models_and_services_keep_default_settings(self):
        for model, base_url in (
            ('test', 'https://openrouter.ai/api/v1'),
            ('anthropic/claude-haiku-5.5', 'https://example.invalid/v1'),
        ):
            with self.subTest(model=model, base_url=base_url), patch.dict('os.environ', {
                'LLM_MODEL': model, 'LLM_BASE_URL': base_url,
            }, clear=True):
                config = LLMConfig.from_env()
                self.assertEqual(config.planner_max_tokens, 1024)
                self.assertEqual(config.excutor_max_tokens, 1024)
                self.assertEqual(config.reasoning_effort, 'low')

    def test_services_use_native_reasoning_fields_for_both_phases(self):
        for base_url in ('https://api.deepseek.com', 'https://example.invalid/v1'):
            client = LLMClient(LLMConfig(model='test', base_url=base_url))
            for phase in ('planner', 'excutor'):
                with self.subTest(base_url=base_url, phase=phase):
                    body = client._request_body([], phase=phase)
                    self.assertEqual(body['reasoning_effort'], 'low')
                    self.assertNotIn('temperature', body)
                    self.assertNotIn('provider', body)
                    if base_url == 'https://api.deepseek.com':
                        self.assertEqual(body['thinking'], {'type': 'enabled'})

    def response(self, content, finish_reason="stop"):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"choices": [{
            "message": {"content": content, "reasoning": "# actions\nInvalid()"},
            "finish_reason": finish_reason,
        }]}).encode("utf-8")
        return response

    async def test_reads_only_final_content(self):
        content = "## assessment\nNo immediate threat.\n## stage\nopening\n## objective\nGrow economy.\n## guidence\nPrepare expansion."
        with patch("agent.harness.llm.request.urlopen", return_value=self.response(content)):
            self.assertEqual(await self.client.complete([], phase="planner"), content)

    async def test_rejects_empty_or_truncated_content(self):
        for content, finish_reason in [("", "stop"), ("# actions\nMove(", "length")]:
            with self.subTest(content=content):
                with patch("agent.harness.llm.request.urlopen", return_value=self.response(content, finish_reason)):
                    with self.assertRaises(LLMClientError):
                        await self.client.complete([], phase="excutor")

    async def test_phase_deadlines_are_passed_to_transport(self):
        for phase, expected in [('planner', 10.0), ('excutor', 10.0)]:
            with self.subTest(phase=phase), patch('agent.harness.llm.request.urlopen', return_value=self.response('ok')) as send:
                self.assertEqual(await self.client.complete([], phase=phase), 'ok')
                self.assertEqual(send.call_args.kwargs['timeout'], expected)

    async def test_transport_error_is_not_retried(self):
        with patch.object(self.client, '_complete_sync', side_effect=OSError('failed')) as send:
            with self.assertRaisesRegex(LLMClientError, 'failed'):
                await self.client.complete([])
            self.assertEqual(send.call_count, 1)

    async def test_deadline_discards_late_response_without_logging_it(self):
        client = LLMClient(LLMConfig(model='test', base_url='https://example.invalid', api_key='test',
                                     planner_timeout_seconds=0.03, excutor_timeout_seconds=0.03))
        for phase in ('planner', 'excutor'):
            release, finished = threading.Event(), threading.Event()
            trace = Mock()

            def slow_response(*args, **kwargs):
                release.wait(1)
                finished.set()
                return json.dumps({'choices': [{'message': {'content': 'late'}}]}), 200

            with self.subTest(phase=phase), patch.object(client, '_complete_sync', side_effect=slow_response) as send:
                try:
                    with self.assertRaisesRegex(LLMClientError, 'timed out'):
                        await client.complete([], phase=phase, trace=trace)
                    self.assertEqual(send.call_count, 1)
                    self.assertFalse(finished.is_set())
                finally:
                    release.set()
                    await asyncio.to_thread(finished.wait, 1)
                self.assertEqual([c.kwargs['stage'] for c in trace.model_conversation.call_args_list], ['request'])
                self.assertFalse(trace.event.call_args.kwargs['retrying'])
