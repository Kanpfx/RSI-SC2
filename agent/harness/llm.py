"""OpenAI-compatible HTTP client using a worker thread while callers await results."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from time import perf_counter
from urllib import request
from urllib.parse import urlparse

from agent.config import LLMConfig
from agent.game.actions.parser import parse_model_payload
from agent.logger.recorder import Telemetry


class LLMClientError(RuntimeError):
    pass


def _is_official_deepseek_api(base_url: str) -> bool:
    return urlparse(base_url).hostname == "api.deepseek.com"


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config

    async def complete(self, messages: list[dict[str, str]], *, phase="excutor", trace=None, iteration=None) -> str:
        if not self.config.configured:
            raise LLMClientError("LLM_MODEL, LLM_BASE_URL and LLM_API_KEY are required")
        messages = self.prepare_messages(messages)
        started = perf_counter()
        timeout = self.config.planner_timeout_seconds if phase == "planner" else self.config.excutor_timeout_seconds
        if trace is not None:
            trace.model_conversation(
                stage="request", iteration=iteration,
                request_body=self._request_body(messages, phase=phase), endpoint=self.config.base_url,
                timeout_seconds=timeout, max_retries=0,
            )
            trace.event("transport_attempt_started", iteration=iteration, attempt=1, stage="system", level="INFO")
        try:
            # The outer deadline also covers a slow response body. Late thread
            # results are discarded and cannot write telemetry or update a plan.
            raw, http_status = await asyncio.wait_for(
                asyncio.to_thread(self._complete_sync, messages, phase=phase, timeout=timeout),
                timeout=timeout,
            )
            metadata = {
                "stage": "response", "iteration": iteration, "attempt": 1,
                "http_status": http_status,
                "request_elapsed_seconds": round(perf_counter() - started, 3),
                "raw_response": raw,
            }
            try:
                result, choice = self._parse_response(raw)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                if trace is not None:
                    trace.model_conversation(**metadata, error=str(exc))
                raise ValueError(f"Invalid LLM response: {exc}") from exc
            if trace is not None:
                trace.model_conversation(
                    **metadata, reply=choice["message"]["content"], usage=result.get("usage"),
                    finish_reason=choice.get("finish_reason"), response_id=result.get("id"),
                    model=result.get("model"),
                )
            return choice["message"]["content"]
        except (OSError, ValueError) as exc:
            reason = f"{phase} request timed out after {timeout:g} real seconds" if isinstance(exc, TimeoutError) else str(exc)
            if trace is not None:
                trace.event(
                    "transport_error", iteration=iteration, attempt=1, stage="system", level="ERROR",
                    error_type=type(exc).__name__, error=reason,
                    http_status=getattr(exc, "code", None), retrying=False,
                    request_elapsed_seconds=round(perf_counter() - started, 3),
                )
            raise LLMClientError(f"LLM request failed: {reason}") from exc

    @staticmethod
    def _parse_response(raw: str) -> tuple[dict[str, Any], dict[str, Any]]:
        result = json.loads(raw)
        choice = result["choices"][0]
        content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("LLM response content is empty or not text")
        if choice.get("finish_reason") == "length":
            raise ValueError("LLM response exceeded token limit")
        return result, choice

    @staticmethod
    def prepare_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
        """Return the agent-authored messages without adding another system role."""
        return list(messages)

    def _request_body(self, messages: list[dict[str, str]], *, phase="excutor") -> dict[str, Any]:
        if phase not in {"planner", "excutor"}:
            raise ValueError(f"Unknown request phase: {phase}")
        planner = phase == "planner"
        body = {
            "model": self.config.model, "messages": messages,
            "max_tokens": self.config.planner_max_tokens if planner else self.config.excutor_max_tokens,
        }
        if _is_official_deepseek_api(self.config.base_url):
            body["thinking"] = {"type": "enabled"}
            body["reasoning_effort"] = self.config.reasoning_effort
        elif urlparse(self.config.base_url).hostname == "openrouter.ai":
            body["reasoning"] = {"effort": self.config.reasoning_effort}
        else:
            body["reasoning_effort"] = self.config.reasoning_effort
        return body

    def _complete_sync(
        self, messages: list[dict[str, str]], *, phase: str, timeout: float,
    ) -> tuple[str, int | None]:
        url = self.config.base_url
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        payload = json.dumps(self._request_body(messages, phase=phase)).encode("utf-8")
        req = request.Request(
            url, data=payload,
            headers={"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            http_status = getattr(response, "status", None)
        return raw, http_status


@dataclass(frozen=True)
class ModelResult:
    actions: list[dict[str, Any]]
    validation_feedback: list[dict[str, Any]]
    request_elapsed_seconds: float
    working: str | None = None
    received_at: float = 0.0
    parse_report: dict[str, Any] = field(default_factory=dict)


class ModelAgent:
    def __init__(self, client: LLMClient):
        self.client = client

    async def run(self, messages: list[dict[str, str]], *, trace: Telemetry,
                  iteration: int, working: str) -> ModelResult:
        started = perf_counter()
        excutor_trace = trace.request_trace(iteration, "excutor")
        reply = await self.client.complete(messages, phase="excutor", trace=excutor_trace, iteration=iteration)
        received = perf_counter()
        elapsed = round(received - started, 3)
        try:
            payload = parse_model_payload(reply)
        except ValueError as exc:
            feedback = [{"kind": "output_format", "submitted_output": reply, "error": str(exc)}]
            report = {"valid": False, "errors": feedback, "sources": []}
            excutor_trace.model_conversation(stage="parsed", iteration=iteration, actions=[], parse_report=report)
            return ModelResult([], feedback, elapsed, received_at=received, parse_report=report)
        feedback = [{"kind": "action_format", "action_index": error["index"] + 1,
                     "submitted_action": error["submitted_action"], "error": error["error"]}
                    for error in payload["errors"]]
        report = {"valid": not feedback, "errors": feedback, "sources": payload["sources"]}
        excutor_trace.model_conversation(stage="parsed", iteration=iteration, actions=payload["actions"],
                                 working=working, parse_report=report)
        return ModelResult(payload["actions"], feedback, elapsed, working, received, report)
