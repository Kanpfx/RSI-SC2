"""Apply asynchronous model decisions at game-frame boundaries."""

from __future__ import annotations

import asyncio
from time import perf_counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from loguru import logger

from agent.config import GameConfig, LLMConfig
from agent.game.actions.errors import exception_reason
from agent.game.actions.errors import exception_reason
from agent.game.actions.policy.exposure import ActionExposure
from agent.game.actions.formatting import format_feedback, format_indexed_actions
from agent.game.actions.policy.validation import PolicyValidator
from agent.game.actions.execution.executor import ActionExecutor
from agent.game.automation import AutomationController
from agent.game.observation.builder import ObservationBuilder
from agent.game.observation.execution_context import TagIdMapper
from agent.game.actions.resolution.loader import ActionCatalog
from agent.harness.context import ContextBuilder
from agent.harness.llm import LLMClient, ModelAgent, ModelResult
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
        self.policy = PolicyValidator(self.catalog, self.game_config)
        self.observation_builder = ObservationBuilder(TagIdMapper())
        self.automation = AutomationController()
        self.telemetry = Telemetry(
            {
                "model": self.llm_config.model,
                "planner_reasoning_effort": self.llm_config.planner_reasoning_effort,
                "planner_max_tokens": self.llm_config.planner_max_tokens,
                "excutor_temperature": self.llm_config.excutor_temperature,
                "excutor_max_tokens": self.llm_config.excutor_max_tokens,
                "action_interval_seconds": self.game_config.model_interval_seconds,
                "tactic": tactic_name,
                **(run_metadata or {}),
            },
            directory=log_directory,
        )
        self.client = LLMClient(self.llm_config)
        self.model_agent = ModelAgent(self.client)
        self.telemetry.save_settings({
            "game": asdict(self.game_config),
            "model": {key: value for key, value in asdict(self.llm_config).items()
                      if key not in {"api_key", "base_url"}},
            "automation": ["mining", "supply", "depot_lowering", "requested_workers", "worker_scouting"],
        })
        self._pending: asyncio.Task[ModelResult] | None = None
        self._excutor_context: asyncio.Future[tuple[str, list[dict[str, Any]]]] | None = None
        self._request_iteration = -1
        self._request_time = 0.0
        self._next_model_time = 0.0
        self._feedback: list[dict[str, Any]] = []
        self._closed = False
        self.executor = ActionExecutor(
            self.catalog, self.game_config, policy=self.policy,
            action_exposure=self.action_exposure,
            observation_builder=self.observation_builder, automation=self.automation,
            telemetry=self.telemetry, on_feedback=lambda item: self._feedback.append(item),
            request_iteration=lambda: self._request_iteration,
        )

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
            applied = self._pending is not None and self._pending.done()
            if applied:
                try:
                    await self._apply_pending(bot, iteration)
                except (KeyError, TypeError, ValueError) as exc:
                    self.executor.record_failure(bot, iteration, "Model application", exception_reason(exc))
            context = self.observation_builder.execution_context(bot)
            for action in self.executor.persistent_actions.prune_missing_actors(context.own_entities):
                self.executor.record_failure(bot, iteration, action, "entity_missing: controlled unit no longer exists")
            self.executor.sync_active(bot)
            self.executor.run_persistent(bot, iteration, context)
            self.executor.run_queued(bot, iteration, context)
            if self._excutor_context is not None and not self._excutor_context.done():
                observation = self.observation_builder.build(bot, iteration)
                surface = self.action_exposure.build(bot, observation.context)
                self.telemetry.observation(
                    iteration=iteration, request_iteration=self._request_iteration,
                    phase="excutor", time=bot.time_formatted, observation=observation.text,
                    game_time=float(bot.time),
                    game_loop=getattr(getattr(bot, "state", None), "game_loop", None),
                )
                self._excutor_context.set_result((observation.text, surface.entries))
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
        self.observation_builder.action_history.begin_decision()
        observation = self.observation_builder.build(bot, iteration)
        surface = self.action_exposure.build(bot, observation.context)
        self.telemetry.observation(
            iteration=iteration, time=bot.time_formatted, observation=observation.text,
            game_time=float(bot.time), game_loop=getattr(getattr(bot, "state", None), "game_loop", None)
        )
        self._feedback = []
        self._request_iteration = iteration
        self._request_time = float(bot.time)
        self._next_model_time = self._request_time + self.game_config.model_interval_seconds
        # Pass detached prompt data only; the task must never access the live bot.
        messages = self.context_builder.build_planner_messages(observation.text, surface.entries)
        decision_time = bot.time_formatted
        self._pending = asyncio.create_task(
            self.model_agent.run(
                messages, trace=self.telemetry, iteration=iteration,
                build_excutor_messages=self._build_excutor_messages,
                on_working=lambda working: self._save_working(working, iteration, decision_time),
            )
        )

    async def _build_excutor_messages(self, working: str) -> list[dict[str, str]]:
        # The game loop supplies a fresh, detached snapshot on the next frame.
        self._excutor_context = asyncio.get_running_loop().create_future()
        try:
            observation, action_entries = await self._excutor_context
        finally:
            self._excutor_context = None
        return self.context_builder.build_excutor_messages(
            working, observation=observation, action_entries=action_entries,
            max_actions=self.game_config.max_actions_per_decision,
        )

    def _save_working(self, working: str, iteration: int, time: str) -> None:
        self.context_builder.update_working(working, time)
        self.telemetry.working_memory(working, request_iteration=iteration)

    async def _apply_pending(self, bot: Any, iteration: int) -> None:
        task, self._pending = self._pending, None
        try:
            result = task.result()
        except Exception as exc:
            self.executor.record_failure(bot, iteration, "Model request", exception_reason(exc), stage="system")
            return

        # Resolve aliases and availability against this frame, not the request frame.
        context = self.observation_builder.execution_context(bot)
        surface = self.action_exposure.build(bot, context)
        review = self.policy.review(bot, result.actions, context, surface)
        sources = {
            item["parsed_index"] - 1: {"action_index": item["source_index"], "submitted_action": item["source"]}
            for item in result.parse_report.get("sources", [])
        }
        validation_feedback = list(result.validation_feedback)
        validation_feedback.extend(
            {"kind": "action", "action": issue.action, "error": issue.reason,
             **sources.get(issue.index, {"action_index": issue.index + 1})}
            for issue in review.issues
        )
        for feedback in validation_feedback:
            submitted = feedback.get(
                "action",
                feedback.get("submitted_action", feedback.get("submitted_output", "Model output")),
            )
            self.executor.record_failure(
                bot, iteration, submitted, feedback["error"],
                stage="validation" if feedback.get("kind") == "action" else "parse",
                **{key: feedback[key] for key in ("action_index", "submitted_action") if key in feedback},
            )
        notices = [
            {"kind": "action_notice", "action": item.action, "error": f"{item.action}: {item.reason}",
             **sources.get(item.index, {"action_index": item.index + 1})}
            for item in review.notices
        ]
        validation_feedback.extend(notices)
        for notice in notices:
            self.executor.record_feedback(bot, notice)
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
            applied_game_time=float(bot.time),
            applied_game_loop=getattr(getattr(bot, "state", None), "game_loop", None),
            observation_age_seconds=round(float(bot.time) - self._request_time, 2),
            response_to_apply_ms=round((perf_counter() - result.received_at) * 1000) if result.received_at else None,
            valid=not review.issues, validation_report=report, normalizations=review.normalizations,
            validated_actions=review.actions, notices=notices,
        )
        if review.normalizations:
            self.executor.event(
                "model_actions_normalized", iteration=iteration, notes=review.normalizations
            )
        feedback_start = len(self._feedback)
        states = self.executor.dispatch(bot, iteration, review, context, sources=[
            sources.get(index, {"action_index": index + 1})
            for index in range(len(result.actions)) if index not in rejected
        ])
        source_indices = {
            self.executor.action_key(action): item["parsed_index"]
            for action, item in zip(review.actions, (row for row in report if row["status"] == "passed"))
        }
        for state in states:
            state["parsed_index"] = source_indices.get(self.executor.action_key(state["action"]))
        validation_feedback.extend(self._feedback[feedback_start:])
        self.executor.sync_active(bot)
        await self._publish_decision(bot, iteration, result, states, validation_feedback)

    async def _publish_decision(
        self, bot: Any, iteration: int, result: ModelResult,
        states: list[dict[str, Any]], feedback: list[dict[str, Any]],
    ) -> None:
        actions = [item["action"] for item in states if item["status"] != "failed"]
        self.telemetry.accepted_decision(
            iteration=self._request_iteration,
            applied_iteration=iteration,
            applied_game_time=float(bot.time),
            applied_game_loop=getattr(getattr(bot, "state", None), "game_loop", None),
            time=bot.time_formatted,
            actions=actions,
            action_states=states,
            active_actions=self.executor.persistent_actions.actions + (
                [self.automation.worker_action] if self.automation.worker_action else []
            ),
            queued_actions=self.executor.deferred_actions.actions,
            validation_feedback=feedback,
            latency_ms=result.latency_ms,
        )
        header = (
            f"[model iteration={self._request_iteration} applied={iteration}] "
            f"t={bot.time_formatted} model={result.latency_ms / 1000:.2f}s "
            f"active={len(self.executor.persistent_actions.actions) + int(self.automation.worker_action is not None)} "
            f"queued={len(self.executor.deferred_actions.actions)}"
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
