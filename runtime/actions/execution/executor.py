"""Dispatch reviewed actions and maintain their execution lifecycle."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from agent.config import GameConfig
from agent.logger.recorder import Telemetry
from agent.runtime.actions.execution.adapter import AresActionAdapter
from agent.runtime.actions.execution.deferred import DeferredActionQueue
from agent.runtime.actions.execution.persistent import PersistentActionRegistry
from agent.runtime.actions.execution.tracking import TrackedBehavior
from agent.runtime.actions.policy.exposure import ActionExposure
from agent.runtime.actions.policy.validation import ActionReview, PolicyValidator
from agent.runtime.actions.resolution.loader import ActionCatalog
from agent.runtime.automation import AutomationController
from agent.runtime.observation.builder import ObservationBuilder


class ActionExecutor:
    """Own action queues, persistent controls and execution feedback."""

    def __init__(
        self, catalog: ActionCatalog, config: GameConfig, *,
        policy: PolicyValidator, action_exposure: ActionExposure,
        observation_builder: ObservationBuilder, automation: AutomationController,
        telemetry: Telemetry, on_feedback: Callable[[dict[str, Any]], None],
        request_iteration: Callable[[], int],
    ):
        self.catalog = catalog
        self.adapter = AresActionAdapter(catalog)
        self.policy = policy
        self.action_exposure = action_exposure
        self.observation_builder = observation_builder
        self.automation = automation
        self.telemetry = telemetry
        self._on_feedback = on_feedback
        self._request_iteration = request_iteration
        self.deferred_actions = DeferredActionQueue(
            catalog, config.deferred_action_ttl_iterations,
            config.resource_queue_mineral_tolerance,
            config.resource_queue_vespene_tolerance,
        )
        self.persistent_actions = PersistentActionRegistry(catalog)
        self._action_origins: dict[str, dict[str, Any]] = {}

    def dispatch(
        self, bot: Any, iteration: int, review: ActionReview, context: Any,
        *, from_queue: bool = False,
    ) -> list[dict[str, Any]]:
        states = []
        for action_index, action in enumerate(review.actions):
            key = self.action_key(action)
            retained = (
                from_queue or action in self.deferred_actions.actions
                or action in self.persistent_actions.actions
                or action == self.automation.worker_action
            )
            if not retained or key not in self._action_origins:
                self._action_origins[key] = {
                    "decision_id": f"d{self._request_iteration()}",
                    "request_iteration": self._request_iteration(),
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
                behaviors = [self.adapter.construct(action, review.arguments[action_index])]
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
                        self._on_feedback({"kind": "action", "action": action, "error": reason})
                        self.event(
                            "action_not_submitted", game_time=float(bot.time),
                            game_loop=getattr(getattr(bot, "state", None), "game_loop", None), iteration=iteration, action=action, reason=reason
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
                self.record_failure(bot, iteration, action, str(exc))
                states.append({"action": action, "status": "failed", "reason": str(exc)})
        for item in states:
            item.update(self._action_origins.get(self.action_key(item["action"]), {}))
        return states

    @staticmethod
    def action_key(action: Any) -> str:
        return json.dumps(action, sort_keys=True, separators=(",", ":"), default=str)

    def event(self, name: str, **fields: Any) -> None:
        origin = self._action_origins.get(self.action_key(fields.get("action")), {})
        metadata = {
            "request_iteration": self._request_iteration(),
            "decision_id": f"d{self._request_iteration()}",
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
        self.event(
            "action_queued", game_time=float(bot.time),
            game_loop=getattr(getattr(bot, "state", None), "game_loop", None), iteration=iteration, action=action,
            status="queued", reason="waiting for resources",
        )

    def run_queued(self, bot: Any, iteration: int, context: Any) -> None:
        ready, expired = self.deferred_actions.pop_ready(bot, iteration)
        for action in expired:
            self.observation_builder.action_history.forget_queued(action)
            reason = "resource wait ended without submission; reconsider against current state"
            self._on_feedback({"kind": "action", "action": action, "error": reason})
            self.event(
                "action_wait_ended", game_time=float(bot.time),
                game_loop=getattr(getattr(bot, "state", None), "game_loop", None), iteration=iteration, action=action, reason=reason
            )
        for action in ready:
            try:
                if self.deferred_actions.construction_pending(bot, action):
                    self.observation_builder.action_history.record(
                        action, bot.time_formatted, "accepted",
                        "matching construction already in progress; no additional order submitted",
                    )
                    self.event(
                        "action_wait_ended", game_time=float(bot.time),
                        game_loop=getattr(getattr(bot, "state", None), "game_loop", None), iteration=iteration, action=action,
                        reason="matching construction already in progress",
                    )
                    continue
                surface = self.action_exposure.build(bot, context)
                review = self.policy.review(bot, [action], context, surface)
                if review.issues:
                    for issue in review.issues:
                        self.record_failure(bot, iteration, action, issue.text())
                    continue
                self.dispatch(bot, iteration, review, context, from_queue=True)
            except (KeyError, TypeError, ValueError) as exc:
                self.record_failure(bot, iteration, action, str(exc))

    def run_persistent(self, bot: Any, iteration: int, context: Any) -> None:
        for action in self.persistent_actions.actions:
            try:
                # Re-resolve current entities and runtime inputs. Temporary cooldowns
                # do not invalidate a persistent intent; Ares handles readiness.
                review = self.policy.review(bot, [action], context, persistent=True)
                if review.issues:
                    raise ValueError(review.issues[0].text())
                current = review.actions[0]
                self.persistent_actions.replace(current)
                behaviors = [self.adapter.construct(current, review.arguments[0])]
                self._register_behaviors(bot, iteration, current, behaviors, True)
            except (KeyError, TypeError, ValueError) as exc:
                self.persistent_actions.discard(action)
                self.record_failure(bot, iteration, action, str(exc))
        self.sync_active(bot)

    def _register_behaviors(
        self, bot: Any, iteration: int, action: dict[str, Any],
        behaviors: list[Any], persistent: bool,
    ) -> None:
        origin = dict(self._action_origins.get(self.action_key(action), {}))

        def failed(reason: str) -> None:
            if persistent:
                self.persistent_actions.discard(action)
            self.record_failure(bot, iteration, action, reason, **origin)
            self.sync_active(bot)

        def executed(result: bool) -> None:
            if persistent:
                return
            reason = (
                "Ares started work"
                if result
                else "Ares started no new work; check prerequisites, pending work and target counts before retrying"
            )
            self.observation_builder.action_history.annotate(action, reason)
            self.event(
                "action_execution", game_time=float(bot.time),
                game_loop=getattr(getattr(bot, "state", None), "game_loop", None), iteration=iteration, action=action,
                status="accepted", started=bool(result), reason=reason, **origin,
            )
            if not result:
                self._on_feedback({"kind": "action", "action": action, "error": reason})

        for behavior in behaviors:
            bot.register_behavior(TrackedBehavior(
                behavior, on_failure=failed, on_result=executed,
            ))

    def sync_active(self, bot: Any) -> None:
        actions = self.persistent_actions.actions
        if self.automation.worker_action is not None:
            actions.append(self.automation.worker_action)
        self.observation_builder.sync_active_actions(actions, bot.time_formatted)

    def record_failure(
        self, bot: Any, iteration: int, action: Any, reason: str, **metadata: Any
    ) -> None:
        self.observation_builder.record_failed_actions(
            [action], bot.time_formatted, reason
        )
        feedback = {"kind": "action", "action": action, "error": reason}
        self._on_feedback(feedback)
        self.event(
            "action_failed", game_time=float(bot.time),
            game_loop=getattr(getattr(bot, "state", None), "game_loop", None), iteration=iteration,
            action=action, status="failed", reason=reason, **metadata,
        )
