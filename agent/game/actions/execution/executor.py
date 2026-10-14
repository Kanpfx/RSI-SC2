"""Dispatch reviewed actions and maintain their execution lifecycle."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from agent.config import GameConfig
from agent.game.actions.errors import ResourceError, exception_reason
from agent.logger.recorder import Telemetry
from agent.game.actions.execution.adapter import AresActionAdapter, TrackedBehavior
from agent.game.actions.execution.lifecycle import PersistentActionRegistry
from agent.game.actions.policy.exposure import ActionExposure
from agent.game.actions.policy.validation import ActionReview, PolicyValidator
from agent.game.actions.resolution.loader import ActionCatalog
from agent.game.automation import AutomationController
from agent.game.observation.builder import ObservationBuilder


class ActionExecutor:
    """Own decision-cycle controls and execution feedback."""

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
        self._feedback_sink = on_feedback
        self._request_iteration = request_iteration
        self.persistent_actions = PersistentActionRegistry(catalog)
        self._persistent_behaviors = {}
        self._action_origins: dict[str, dict[str, Any]] = {}

    def record_feedback(self, bot: Any, item: dict[str, Any]) -> None:
        origin = self._action_origins.get(self.action_key(item.get("action")), {})
        item = {
            **{key: origin[key] for key in ("action_index", "submitted_action") if key in origin},
            **item,
        }
        action = item.get("action", item.get("submitted_action", "Model output"))
        history = self.observation_builder.action_history
        if item.get("kind") == "action_notice":
            history.annotate(action, item["error"])
        else:
            history.record(action, bot.time_formatted, "failed", item["error"],
                           submitted_action=item.get("submitted_action"))
        self._feedback_sink(item)

    def begin_decision(self, bot: Any) -> None:
        """End the previous cycle before validating a received decision."""
        self.persistent_actions.clear()
        self._persistent_behaviors.clear()
        self.automation.replace_worker_override(None)
        self._action_origins.clear()
        self.sync_active(bot)

    def dispatch(
        self, bot: Any, iteration: int, review: ActionReview, context: Any,
        *, sources: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        states, origins = [], []
        for action_index, action in enumerate(review.actions):
            key = self.action_key(action)
            retained = (
                action in self.persistent_actions.actions
                or action == self.automation.worker_action
            )
            if not retained or key not in self._action_origins:
                self._action_origins[key] = {
                    "decision_id": f"d{self._request_iteration()}",
                    "request_iteration": self._request_iteration(),
                    "action_id": uuid4().hex,
                    **(sources[action_index] if sources is not None else {}),
                }
            origins.append(dict(self._action_origins[key]))
            try:
                action_id = self.catalog.get(action["id"])["name"]
                persistent = self.persistent_actions.is_persistent(action)
                if action_id == "BuildWorkers" and persistent:
                    _, changed = self.automation.replace_worker_override(action)
                    states.append({"action": action, "status": "active", "unchanged": not changed})
                    continue
                # Check construction and parameters before replacing a valid old task.
                behaviors = self._construct_behaviors(action, review.arguments[action_index])
                if (
                    not persistent and action_id == "BuildStructure"
                    and not bot.can_afford(review.arguments[action_index]["structure_id"])
                ):
                    reason = str(ResourceError.for_cost(bot, review.arguments[action_index]["structure_id"]))
                    self.record_failure(bot, iteration, action, reason)
                    states.append({"action": action, "status": "failed", "reason": reason})
                    continue
                changed = self.persistent_actions.replace(action)
                if not changed:
                    states.append({"action": action, "status": "active", "unchanged": True})
                    continue
                if not persistent:
                    self._register_behaviors(bot, iteration, action, behaviors, False)
                states.append({
                    "action": action,
                    "status": "active" if persistent else "accepted",
                })
            except (KeyError, TypeError, ValueError) as exc:
                self.record_failure(bot, iteration, action, exception_reason(exc))
                states.append({"action": action, "status": "failed", "reason": exception_reason(exc)})
        for item, origin in zip(states, origins):
            item.update(origin)
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

    def run_persistent(self, bot: Any, iteration: int, context: Any) -> None:
        active_keys = {self.action_key(a) for a in self.persistent_actions.actions}
        self._persistent_behaviors = {k: v for k, v in self._persistent_behaviors.items() if k in active_keys}
        for action in self.persistent_actions.actions:
            current = action
            try:
                # Re-resolve current entities and runtime inputs. Temporary cooldowns
                # do not invalidate a persistent intent; Ares handles readiness.
                review = self.policy.review(bot, [action], context, persistent=True)
                if review.issues:
                    self.persistent_actions.discard(action)
                    self._persistent_behaviors.pop(self.action_key(action), None)
                    self.record_failure(bot, iteration, action, review.issues[0].reason)
                    continue
                current = review.actions[0]
                old_key = self.action_key(action)
                key = self.action_key(current)
                previous = self._persistent_behaviors.get(old_key)
                if key != old_key:
                    self.persistent_actions.discard(action)
                    self.persistent_actions.replace(current)
                    self._persistent_behaviors.pop(old_key, None)
                    if old_key in self._action_origins:
                        self._action_origins[key] = self._action_origins.pop(old_key)
                for notice in review.notices:
                    self.record_feedback(bot, {'kind': 'action_notice', 'action': current, 'error': notice.reason})
                behaviors = self._construct_behaviors(current, review.arguments[0], previous)
                self._persistent_behaviors[key] = behaviors if isinstance(review.arguments[0], list) else behaviors[0]
                self._register_behaviors(bot, iteration, current, behaviors, True)
            except (KeyError, TypeError, ValueError) as exc:
                self.persistent_actions.discard(current)
                self._persistent_behaviors.pop(self.action_key(current), None)
                self.record_failure(bot, iteration, current, exception_reason(exc))
        self.sync_active(bot)

    def _construct_behaviors(self, action, arguments, previous=None) -> list[Any]:
        if not isinstance(arguments, list):
            return [self.adapter.construct(action, arguments, previous)]
        cached = previous if isinstance(previous, list) else []
        return [self.adapter.construct(action, kwargs, cached[index] if index < len(cached) else None)
                for index, kwargs in enumerate(arguments)]

    def _no_order_reason(self, bot: Any, action: dict[str, Any], behavior: Any) -> str | None:
        """Explain only blockers observable without issuing another command."""
        name = self.catalog.get(action["id"])["name"]
        if name == "AMoveGroup" and behavior.group:
            from sc2.ids.ability_id import AbilityId

            unit = max(behavior.group, key=lambda u: u.position.distance_to(behavior.target.position))
            if behavior.duplicate_or_similar_order(unit, behavior.target, AbilityId.ATTACK):
                return None
        if name == "BuildStructure":
            structure = behavior.structure_id
            if not bot.can_afford(structure):
                return str(ResourceError.for_cost(bot, structure))
            if behavior.tech_progress_check:
                progress = bot.tech_requirement_progress(structure)
                if progress < behavior.tech_progress_check:
                    from sc2.constants import TERRAN_TECH_REQUIREMENT, PROTOSS_TECH_REQUIREMENT, ZERG_TECH_REQUIREMENT
                    from sc2.data import Race
                    requirements = {Race.Terran: TERRAN_TECH_REQUIREMENT, Race.Protoss: PROTOSS_TECH_REQUIREMENT,
                                    Race.Zerg: ZERG_TECH_REQUIREMENT}
                    prerequisite = requirements[bot.race].get(structure)
                    return (
                        f"prerequisite_unmet: {structure.name} requires "
                        f"{getattr(prerequisite, 'name', 'prerequisite technology')}; "
                        f"tech progress={progress:.0%}, required={behavior.tech_progress_check:.0%}"
                    )
            on_route = bot.not_started_but_in_building_tracker(structure)
            if on_route >= behavior.max_on_route:
                return (f"no_order_issued: {structure.name} already has {on_route} worker(s) on route; "
                        f"max_on_route={behavior.max_on_route}; check construction progress")
        if name == "AMove":
            distance = behavior.unit.position.distance_to(behavior.target.position)
            if behavior.success_at_distance > 0 and distance < behavior.success_at_distance:
                return (f"no_order_issued: unit={action['args']['unit']} already near target; "
                        f"distance={distance:.2f}, success_at_distance={behavior.success_at_distance:g}")
        if name == "DropCargo" and behavior.unit.cargo_used == 0:
            return f"no_order_issued: unit={action['args']['unit']} has no cargo to unload"
        if name in {"UseAbility", "UseAOEAbility", "GroupUseAbility"}:
            ability = behavior.ability_id if name == "UseAOEAbility" else behavior.ability
            units = behavior.group if name == "GroupUseAbility" else [behavior.unit]
            unavailable = [u for u in units if ability not in u.abilities]
            blocked = (len(unavailable) == len(units) or behavior.sync_command) if name == "GroupUseAbility" else True
            if unavailable and blocked:
                return (f"ability_unavailable: {ability.name} is not currently available; "
                        f"unit(s)={action['args'].get('unit', action['args'].get('group'))}; "
                        f"energy={[u.energy for u in unavailable]}; check ability readiness and unit state; exact cause unavailable")
            if name == "UseAOEAbility" and len(behavior.targets) < behavior.min_targets:
                return (f"target_unavailable: {ability.name} has {len(behavior.targets)} candidate targets; "
                        f"requires at least {behavior.min_targets}")
        return "no_order_issued: Ares returned False; exact cause unavailable; check current unit, target and action prerequisites"

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

        def executed(behavior: Any, result: bool) -> None:
            if persistent:
                return
            if not result:
                try:
                    reason = self._no_order_reason(bot, action, behavior)
                except (AttributeError, KeyError, TypeError, ValueError) as exc:
                    reason = f"no_order_issued: Ares returned False; cause unavailable; diagnostic {exception_reason(exc)}"
                if reason is None:
                    return
                self.record_failure(bot, iteration, action, reason, **origin)
                return
            reason = "Ares started work"
            self.observation_builder.record_submitted_actions([action], bot.time_formatted)
            self.observation_builder.action_history.annotate(action, reason)
            self.event(
                "action_execution", game_time=float(bot.time),
                game_loop=getattr(getattr(bot, "state", None), "game_loop", None), iteration=iteration, action=action,
                status="submitted", started=bool(result), reason=reason, **origin,
            )

        for behavior in behaviors:
            bot.register_behavior(TrackedBehavior(
                behavior, on_failure=failed, on_result=lambda result, current=behavior: executed(current, result),
            ))

    def sync_active(self, bot: Any) -> None:
        actions = self.persistent_actions.actions
        if self.automation.worker_action is not None:
            actions.append(self.automation.worker_action)
        self.observation_builder.sync_active_actions(actions, bot.time_formatted)

    def record_failure(
        self, bot: Any, iteration: int, action: Any, reason: str, **metadata: Any
    ) -> None:
        feedback = {"kind": "action", "action": action, "error": reason,
                    **{key: metadata[key] for key in ("action_index", "submitted_action") if key in metadata}}
        self.record_feedback(bot, feedback)
        self.event(
            "action_failed", game_time=float(bot.time),
            game_loop=getattr(getattr(bot, "state", None), "game_loop", None), iteration=iteration,
            action=action, status="failed", reason=reason, **metadata,
        )
