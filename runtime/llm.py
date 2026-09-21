"""OpenAI-compatible HTTP client using a worker thread while callers await results."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable
from time import perf_counter
from urllib import request
from urllib.error import HTTPError
from urllib.parse import urlparse

from agent.config import LLMConfig
from agent.runtime.parser import parse_model_payload
from agent.logger.recorder import Telemetry


class LLMClientError(RuntimeError):
    pass


def _is_official_deepseek_api(base_url: str) -> bool:
    return urlparse(base_url).hostname == "api.deepseek.com"


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config

    async def complete(self, messages: list[dict[str, str]], *, trace=None, iteration=None) -> str:
        if not self.config.configured:
            raise LLMClientError("LLM_MODEL, LLM_BASE_URL and LLM_API_KEY are required")
        messages = self.prepare_messages(messages)
        started = perf_counter()
        if trace is not None:
            trace.model_conversation(
                stage="request", iteration=iteration,
                request_body=self._request_body(messages), endpoint=self.config.base_url,
                timeout_s=self.config.timeout_s, max_retries=self.config.transport_retries,
            )
        last_error: Exception | None = None
        for attempt in range(1, self.config.transport_retries + 2):
            attempt_started = perf_counter()
            if trace is not None:
                trace.event("transport_attempt_started", iteration=iteration, attempt_id=f"d{iteration}:t{attempt}",
                            attempt=attempt, stage="system", level="INFO")
            try:
                return await asyncio.to_thread(
                    self._complete_sync, messages, trace=trace, iteration=iteration,
                    attempt=attempt, request_started=started,
                )
            except (OSError, TimeoutError, ValueError) as exc:
                last_error = exc
                retrying = attempt <= self.config.transport_retries
                if trace is not None:
                    trace.event(
                        "transport_error", iteration=iteration, attempt_id=f"d{iteration}:t{attempt}",
                        stage="system", level="WARNING" if retrying else "ERROR",
                        attempt=attempt, error_type=type(exc).__name__, error=str(exc),
                        http_status=getattr(exc, "code", None), retrying=retrying,
                        latency_ms=round((perf_counter() - attempt_started) * 1000),
                        total_latency_ms=round((perf_counter() - started) * 1000),
                        response_body=exc.read().decode("utf-8", errors="replace") if isinstance(exc, HTTPError) else None,
                    )
                if retrying:
                    await asyncio.sleep(0.5 * attempt)
        raise LLMClientError(f"LLM request failed: {last_error}")

    @staticmethod
    def prepare_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
        """Return the agent-authored messages without adding another system role."""
        return list(messages)

    def _request_body(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        body = {
            "model": self.config.model, "messages": messages,
            "temperature": self.config.temperature, "max_tokens": self.config.max_tokens,
        }
        if _is_official_deepseek_api(self.config.base_url):
            body["thinking"] = {"type": "disabled"}
        elif urlparse(self.config.base_url).hostname == "openrouter.ai":
            body["reasoning"] = {"enabled": False}
        return body

    def _complete_sync(
        self, messages: list[dict[str, str]], *, trace=None, iteration=None,
        attempt=1, request_started=None,
    ) -> str:
        started = perf_counter()
        url = self.config.base_url
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        payload = json.dumps(self._request_body(messages)).encode("utf-8")
        req = request.Request(
            url, data=payload,
            headers={"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=self.config.timeout_s) as response:
            raw = response.read().decode("utf-8")
            http_status = getattr(response, "status", None)
        elapsed = round((perf_counter() - started) * 1000)
        metadata = {
            "stage": "response", "iteration": iteration,
            "attempt_id": f"d{iteration}:t{attempt}", "attempt": attempt,
            "http_status": http_status, "latency_ms": elapsed,
            "total_latency_ms": round((perf_counter() - (request_started or started)) * 1000),
            "raw_response": raw,
        }
        try:
            result = json.loads(raw)
            choice = result["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("LLM response content is not text")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            if trace is not None:
                trace.model_conversation(**metadata, error=str(exc))
            raise ValueError(f"Invalid LLM response: {exc}") from exc
        if trace is not None:
            trace.model_conversation(
                **metadata, reply=content, usage=result.get("usage"),
                finish_reason=choice.get("finish_reason"), response_id=result.get("id"),
                model=result.get("model"),
            )
        return content


@dataclass(frozen=True)
class ModelResult:
    actions: list[dict[str, Any]]
    validation_feedback: list[dict[str, Any]]
    latency_ms: int
    working: str | None = None
    received_at: float = 0.0
    parse_report: dict[str, Any] = field(default_factory=dict)


class ModelAgent:
    def __init__(self, client: LLMClient):
        self.client = client

    async def run(self, messages: list[dict[str, str]], *, trace: Telemetry,
                  iteration: int, action_message: dict[str, str], action_system: str,
                  on_working: Callable[[str], None]) -> ModelResult:
        started = perf_counter()
        working_trace = trace.request_trace(iteration, "working")
        working = await self.client.complete(messages, trace=working_trace, iteration=iteration)
        on_working(working)
        messages = [{"role": "system", "content": action_system}, {"role": "user", "content":
                    "<core_missions>\n" + working + "\n</core_missions>\n\n" + action_message["content"]}]
        action_trace = trace.request_trace(iteration, "actions")
        reply = await self.client.complete(messages, trace=action_trace, iteration=iteration)
        received = perf_counter()
        latency = round((received - started) * 1000)
        try:
            payload = parse_model_payload(reply)
        except ValueError as exc:
            feedback = [{"kind": "output_format", "submitted_output": reply, "error": str(exc)}]
            report = {"valid": False, "errors": feedback, "sources": []}
            action_trace.model_conversation(stage="parsed", iteration=iteration, actions=[], parse_report=report)
            return ModelResult([], feedback, latency, received_at=received, parse_report=report)
        feedback = [{"kind": "action_format", "action_index": error["index"] + 1,
                     "submitted_action": error["submitted_action"], "error": error["error"]}
                    for error in payload["errors"]]
        report = {"valid": not feedback, "errors": feedback, "sources": payload["sources"]}
        action_trace.model_conversation(stage="parsed", iteration=iteration, actions=payload["actions"],
                                 working=working, parse_report=report)
        return ModelResult(payload["actions"], feedback, latency, working, received, report)
