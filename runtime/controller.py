"""Apply asynchronous model decisions at game-frame boundaries."""

from __future__ import annotations

import asyncio
import json
from time import perf_counter
from uuid import uuid4
from dataclasses import asdict
from pathlib import Path
from typing import Any

from loguru import logger

from agent.config import GameConfig, LLMConfig
from agent.runtime.actions.adapter import AresActionAdapter
from agent.runtime.actions.execution import TrackedBehavior
from agent.runtime.actions.exposure import ActionExposure
from agent.runtime.actions.formatting import format_feedback, format_indexed_actions
from agent.runtime.actions.persistent import PersistentActionRegistry
from agent.runtime.actions.validation import ActionReview, PolicyValidator
from agent.runtime.actions.deferred import DeferredActionQueue
from agent.runtime.automation import AutomationController
from agent.runtime.observation.builder import ObservationBuilder
from agent.runtime.observation.state import TagIdMapper
from agent.runtime.actions.loader import ActionCatalog
from agent.context.builder import ContextBuilder
from agent.runtime.llm import LLMClient, ModelAgent, ModelResult
from agent.logger.recorder import Telemetry


class LLMGameController:
    """Keep one request in flight while automation and accepted controls run."""

    def __init__(
        self,
        game_config: GameConfig | None = None,
        llm_config: LLMConfig | None = None,
        *,
        tactic_name: str = "BattleCruiserRush",
        run_metadata: dict[str, Any] | None = None,
        log_directory: Path | None = None,
    ):
        self.game_config = game_config or GameConfig()
        self.llm_config = llm_config or LLMConfig.from_env()
        self.context_builder = ContextBuilder(tactic_name)
        self.catalog = ActionCatalog.load()
        self.action_exposure = ActionExposure(self.catalog)
        self.adapter = AresActionAdapter(self.catalog)
        self.policy = PolicyValidator(self.catalog, self.game_config)
        self.deferred_actions = DeferredActionQueue(
            self.catalog,
            self.game_config.deferred_action_ttl_iterations,
            self.game_config.resource_queue_mineral_tolerance,
            self.game_config.resource_queue_vespene_tolerance,
        )
        self.observation_builder = ObservationBuilder(TagIdMapper())
        self.automation = AutomationController()
        self.persistent_actions = PersistentActionRegistry(self.catalog)
        self.telemetry = Telemetry(
            {
                "model": self.llm_config.model,
                "temperature": self.llm_config.temperature,
                "max_tokens": self.llm_config.max_tokens,
                "action_interval_seconds": self.game_config.model_interval_seconds,
                "tactic": tactic_name,
                **(run_metadata or {}),
            },
            directory=log_directory,
        )
        self.client = LLMClient(self.llm_config)
        self.model_agent = ModelAgent(self.client)
        self.telemetry.snapshot_context(self.context_builder.sources, {
            "game": asdict(self.game_config),
            "model": {key: value for key, value in asdict(self.llm_config).items()
                      if key not in {"api_key", "base_url"}},
            "automation": ["mining", "supply", "depot_lowering", "requested_workers"],
        })
        self._pending: asyncio.Task[ModelResult] | None = None
        self._request_iteration = -1
        self._request_time = 0.0
        self._next_model_time = 0.0
        self._feedback: list[dict[str, Any]] = []
        self._closed = False
        self._action_origins: dict[str, dict[str, Any]] = {}

    @property
    def active(self) -> bool:
        return self.llm_config.configured and not self._closed

    async def close(self) -> None:
        """Discard unfinished replies when the match ends."""
        self._closed = True
        task, self._pending = self._pending, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run_iteration(self, bot: Any, iteration: int) -> None:
        if self._closed:
            return
        await self.automation.run(bot, iteration)
        try:
            self.observation_builder.collect_frame(bot)
            applied = self._pending is not None and self._pending.done()
            if applied:
                try:
                    await self._apply_pending(bot, iteration)
                except (KeyError, TypeError, ValueError) as exc:
                    self._record_failure(bot, iteration, "Model application", str(exc))
            context = self.observation_builder.execution_context(bot)
            for action in self.persistent_actions.prune_missing_actors(context.own_entities):
                self._record_failure(bot, iteration, action, "controlled unit no longer exists")
            self._sync_active(bot)
            self._run_persistent_actions(bot, iteration, context)
            self._run_queued_actions(bot, iteration, context)
            if (
                self.active
                and not applied
                and self._pending is None
                and float(bot.time) >= self._next_model_time
            ):
                self._start_request(bot, iteration)
        finally:
            self.automation.register_worker_production(bot)

    def _start_request(self, bot: Any, iteration: int) -> None:
        observation = self.observation_builder.build(bot, iteration)
        surface = self.action_exposure.build(bot, observation.context)
        self.telemetry.observation(
            iteration=iteration, time=bot.time_formatted, observation=observation.text
        )
        feedback, self._feedback = self._feedback, []
        self._request_iteration = iteration
        self._request_time = float(bot.time)
        self._next_model_time = self._request_time + self.game_config.model_interval_seconds
        # Pass detached prompt data only; the task must never access the live bot.
        messages = self.context_builder.build(
            observation.text, surface.entries, feedback,
            max_actions=self.game_config.max_actions_per_decision,
        )
        self._pending = asyncio.create_task(
            self.model_agent.run(messages, trace=self.telemetry, iteration=iteration)
        )

    async def _apply_pending(self, bot: Any, iteration: int) -> None:
        task, self._pending = self._pending, None
        try:
            result = task.result()
        except Exception as exc:
            self._record_failure(bot, iteration, "Model request", str(exc), stage="system")
            return

        try:
            if self.context_builder.update_working(result.working):
                self.telemetry.working_memory(self.context_builder.working, iteration=iteration,
                                              request_iteration=self._request_iteration)
        except ValueError as exc:
            self._feedback.append({"kind": "working_memory", "error": str(exc)})
            self._event("working_memory_rejected", iteration=iteration, reason=str(exc))

        # Resolve aliases and availability against this frame, not the request frame.
        context = self.observation_builder.execution_context(bot)
        surface = self.action_exposure.build(bot, context)
        review = self.policy.review(bot, result.actions, context, surface)
        validation_feedback = list(result.validation_feedback)
        validation_feedback.extend(
            {"kind": "action", "action": issue.action, "error": issue.text()}
            for issue in review.issues
        )
        for feedback in validation_feedback:
            submitted = feedback.get(
                "action",
                feedback.get("submitted_action", feedback.get("submitted_output", "Model output")),
            )
            self._record_failure(
                bot, iteration, submitted, feedback["error"],
                stage="validation" if feedback.get("kind") == "action" else "parse",
            )
        notices = [
            {"kind": "action_notice", "action_index": item.index + 1, "action": item.action, "error": item.reason}
            for item in review.notices
        ]
        validation_feedback.extend(notices)
        self._feedback.extend(notices)
        rejected = {issue.index: issue for issue in review.issues}
        normalized = iter(review.actions)
        report = []
        for index, submitted in enumerate(result.actions):
            issue = rejected.get(index)
            accepted = None if issue else next(normalized)
            report.append({
                "parsed_index": index + 1,
                "status": "rejected" if issue else "passed",
                "reason": issue.text() if issue else None,
                **({"before": submitted, "after": accepted}
                   if accepted != submitted and not any(item.index == index for item in review.notices) else {}),
            })
        self.telemetry.model_conversation(
            stage="validated", iteration=self._request_iteration,
            applied_iteration=iteration,
            observation_age_seconds=round(float(bot.time) - self._request_time, 2),
            response_to_apply_ms=round((perf_counter() - result.received_at) * 1000) if result.received_at else None,
            valid=not review.issues, validation_report=report, normalizations=review.normalizations,
            validated_actions=review.actions, notices=notices,
        )
        if review.normalizations:
            self._event(
                "model_actions_normalized", iteration=iteration, notes=review.normalizations
            )
        feedback_start = len(self._feedback)
        states = self._dispatch_actions(bot, iteration, review, context)
        source_indices = {
            self._action_key(action): item["parsed_index"]
            for action, item in zip(review.actions, (row for row in report if row["status"] == "passed"))
        }
        for state in states:
            state["parsed_index"] = source_indices.get(self._action_key(state["action"]))
        validation_feedback.extend(self._feedback[feedback_start:])
        self._sync_active(bot)
        await self._publish_decision(bot, iteration, result, states, validation_feedback)

    def _dispatch_actions(
        self, bot: Any, iteration: int, review: ActionReview, context: Any,
        *, from_queue: bool = False,
    ) -> list[dict[str, Any]]:
        states = []
        for action in review.actions:
            key = self._action_key(action)
            retained = (
                from_queue or action in self.deferred_actions.actions
                or action in self.persistent_actions.actions
                or action == self.automation.worker_action
            )
            if not retained or key not in self._action_origins:
                self._action_origins[key] = {
                    "decision_id": f"d{self._request_iteration}",
                    "request_iteration": self._request_iteration,
                    "action_id": uuid4().hex,
                }
            try:
                action_id = self.catalog.get(action["id"])["id"]
                if action_id == "macro.build_workers":
                    _, changed = self.automation.replace_worker_override(action)
                    states.append({"action": action, "status": "active", "unchanged": not changed})
                    continue
                persistent = self.persistent_actions.is_persistent(action)
                # Check construction and parameters before replacing a valid old task.
                behaviors = self.adapter.compile([action], context)
                if not persistent:
                    if (
                        action in self.deferred_actions.actions
                        and self.deferred_actions.construction_pending(bot, action)
                    ):
                        self.deferred_actions.discard(action)
                        reason = "matching construction already in progress; no additional order submitted"
                        self.observation_builder.action_history.record(
                            action, bot.time_formatted, "accepted", reason
                        )
                        states.append({"action": action, "status": "accepted", "reason": reason})
                        continue
                    resource_status = self.deferred_actions.resource_status(bot, action)
                    if resource_status == "queued":
                        self._queue_action(bot, iteration, action)
                        states.append({"action": action, "status": "queued"})
                        continue
                    if resource_status == "blocked":
                        reason = "resource shortfall exceeds queue tolerance; not submitted"
                        self._feedback.append({"kind": "action", "action": action, "error": reason})
                        self._event(
                            "action_not_submitted", iteration=iteration, action=action, reason=reason
                        )
                        continue
                    self.deferred_actions.discard(action)
                changed = self.persistent_actions.replace(action)
                if not changed:
                    states.append({"action": action, "status": "active", "unchanged": True})
                    continue
                if not persistent:
                    self._register_behaviors(bot, iteration, action, behaviors, False)
                    self.observation_builder.record_registered_actions(
                        [action], bot.time_formatted
                    )
                states.append({
                    "action": action,
                    "status": "active" if persistent else "accepted",
                })
            except (KeyError, TypeError, ValueError) as exc:
                self._record_failure(bot, iteration, action, str(exc))
                states.append({"action": action, "status": "failed", "reason": str(exc)})
        for item in states:
            item.update(self._action_origins.get(self._action_key(item["action"]), {}))
        return states

    @staticmethod
    def _action_key(action: Any) -> str:
        return json.dumps(action, sort_keys=True, separators=(",", ":"), default=str)

    def _event(self, name: str, **fields: Any) -> None:
        origin = self._action_origins.get(self._action_key(fields.get("action")), {})
        metadata = {
            "request_iteration": self._request_iteration,
            "decision_id": f"d{self._request_iteration}",
            **origin,
            **fields,
        }
        self.telemetry.event(name, **metadata)

    def _queue_action(self, bot: Any, iteration: int, action: dict[str, Any]) -> None:
        if not self.deferred_actions.enqueue(action, iteration):
            return
        self.observation_builder.action_history.record(
            action, bot.time_formatted, "queued", "waiting for resources"
        )
        self._event(
            "action_queued", iteration=iteration, action=action,
            status="queued", reason="waiting for resources",
        )

    def _run_queued_actions(self, bot: Any, iteration: int, context: Any) -> None:
        ready, expired = self.deferred_actions.pop_ready(bot, iteration)
        for action in expired:
            self.observation_builder.action_history.forget_queued(action)
            reason = "resource wait ended without submission; reconsider against current state"
            self._feedback.append({"kind": "action", "action": action, "error": reason})
            self._event(
                "action_wait_ended", iteration=iteration, action=action, reason=reason
            )
        for action in ready:
            try:
                if self.deferred_actions.construction_pending(bot, action):
                    self.observation_builder.action_history.record(
                        action, bot.time_formatted, "accepted",
                        "matching construction already in progress; no additional order submitted",
                    )
                    self._event(
                        "action_wait_ended", iteration=iteration, action=action,
                        reason="matching construction already in progress",
                    )
                    continue
                surface = self.action_exposure.build(bot, context)
                review = self.policy.review(bot, [action], context, surface)
                if review.issues:
                    for issue in review.issues:
                        self._record_failure(bot, iteration, action, issue.text())
                    continue
                self._dispatch_actions(bot, iteration, review, context, from_queue=True)
            except (KeyError, TypeError, ValueError) as exc:
                self._record_failure(bot, iteration, action, str(exc))

    def _run_persistent_actions(self, bot: Any, iteration: int, context: Any) -> None:
        for action in self.persistent_actions.actions:
            try:
                behaviors = self.adapter.compile([action], context)
                self._register_behaviors(bot, iteration, action, behaviors, True)
            except (KeyError, TypeError, ValueError) as exc:
                self.persistent_actions.discard(action)
                self._record_failure(bot, iteration, action, str(exc))
        self._sync_active(bot)

    def _register_behaviors(
        self, bot: Any, iteration: int, action: dict[str, Any],
        behaviors: list[Any], persistent: bool,
    ) -> None:
        origin = dict(self._action_origins.get(self._action_key(action), {}))

        def failed(reason: str) -> None:
            if persistent:
                self.persistent_actions.discard(action)
            self._record_failure(bot, iteration, action, reason, **origin)
            self._sync_active(bot)

        def executed(result: bool) -> None:
            if persistent:
                return
            reason = (
                "Ares started work"
                if result
                else "Ares started no new work; check prerequisites, pending work and target counts before retrying"
            )
            self.observation_builder.action_history.annotate(action, reason)
            self._event(
                "action_execution", iteration=iteration, action=action,
                status="accepted", started=bool(result), reason=reason, **origin,
            )
            if not result:
                self._feedback.append({"kind": "action", "action": action, "error": reason})

        for behavior in behaviors:
            bot.register_behavior(TrackedBehavior(
                behavior, on_failure=failed, on_result=executed,
            ))

    def _sync_active(self, bot: Any) -> None:
        actions = self.persistent_actions.actions
        if self.automation.worker_action is not None:
            actions.append(self.automation.worker_action)
        self.observation_builder.sync_active_actions(actions, bot.time_formatted)

    def _record_failure(
        self, bot: Any, iteration: int, action: Any, reason: str, **metadata: Any
    ) -> None:
        self.observation_builder.record_failed_actions(
            [action], bot.time_formatted, reason
        )
        feedback = {"kind": "action", "action": action, "error": reason}
        self._feedback.append(feedback)
        self._event(
            "action_failed", iteration=iteration,
            action=action, status="failed", reason=reason, **metadata,
        )

    async def _publish_decision(
        self, bot: Any, iteration: int, result: ModelResult,
        states: list[dict[str, Any]], feedback: list[dict[str, Any]],
    ) -> None:
        actions = [item["action"] for item in states if item["status"] != "failed"]
        self.telemetry.accepted_decision(
            iteration=self._request_iteration,
            applied_iteration=iteration,
            time=bot.time_formatted,
            actions=actions,
            action_states=states,
            active_actions=self.persistent_actions.actions + (
                [self.automation.worker_action] if self.automation.worker_action else []
            ),
            queued_actions=self.deferred_actions.actions,
            validation_feedback=feedback,
            latency_ms=result.latency_ms,
        )
        header = (
            f"[model iteration={self._request_iteration} applied={iteration}] "
            f"t={bot.time_formatted} model={result.latency_ms / 1000:.2f}s "
            f"active={len(self.persistent_actions.actions) + int(self.automation.worker_action is not None)} "
            f"queued={len(self.deferred_actions.actions)}"
        )
        await self._chat_model_decision(bot, header, actions)
        lines = format_indexed_actions(actions) if actions else ["No new action submissions."]
        logger.info("{}\n{}\n", header, "\n".join(lines))
        if feedback:
            logger.warning("Validation feedback:\n{}", format_feedback(feedback))

    @staticmethod
    async def _chat_model_decision(
        bot: Any, header: str, actions: list[dict[str, Any]]
    ) -> None:
        try:
            await bot.chat_send(header, team_only=True)
            lines = format_indexed_actions(actions) if actions else ["No new action submissions."]
            for line in lines:
                await bot.chat_send(
                    line if len(line) <= 240 else f"{line[:237]}...", team_only=True
                )
        except Exception as exc:
            logger.warning("{} chat output failed: {}", header, exc)
