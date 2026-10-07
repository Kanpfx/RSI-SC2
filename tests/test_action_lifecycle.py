"""Decision-cycle renewal and one-time execution contracts."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import run
from agent.config import GameConfig
from agent.game.actions.execution.executor import ActionExecutor
from agent.game.actions.execution.lifecycle import PersistentActionRegistry
from agent.game.actions.policy.validation import ActionReview
from agent.game.actions.resolution.loader import ActionCatalog
from agent.game.observation.builder import ObservationBuilder
from agent.game.observation.execution_context import TagIdMapper


class ActionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.catalog = ActionCatalog.load()
        self.feedback = Mock()
        self.executor = ActionExecutor(
            self.catalog, GameConfig(), policy=Mock(), action_exposure=Mock(),
            observation_builder=ObservationBuilder(TagIdMapper()), automation=SimpleNamespace(worker_action=None),
            telemetry=Mock(), on_feedback=self.feedback, request_iteration=lambda: 1,
        )
        self.bot = SimpleNamespace(
            time=0, time_formatted="00:00", can_afford=Mock(return_value=False),
            minerals=100, vespene=0,
            calculate_cost=Mock(return_value=SimpleNamespace(minerals=150, vespene=0)),
            register_behavior=Mock(),
        )

    def test_registry_uses_catalog_flags_for_every_action(self):
        registry = PersistentActionRegistry(self.catalog)
        for entry in self.catalog.entries.values():
            self.assertEqual(registry.is_persistent({"id": entry["name"]}), entry["persistent"])
        entry = self.catalog.get("AMove")
        entry["persistent"] = True
        self.assertTrue(registry.is_persistent({"id": "AMove"}))

    def test_one_time_resource_failure_is_not_retried(self):
        action = {"id": "BuildStructure", "args": {"structure_id": "BARRACKS"}}
        self.executor.adapter.construct = Mock()
        review = ActionReview([action], [], [], arguments=[{"structure_id": object()}])
        states = self.executor.dispatch(self.bot, 1, review, None)
        self.assertEqual(states[0]["status"], "failed")
        self.assertIn("requires 150 minerals", states[0]["reason"])
        self.assertIn("missing 50 minerals", states[0]["reason"])
        self.bot.register_behavior.assert_not_called()
        self.assertEqual(self.executor.persistent_actions.actions, [])
        self.executor.run_persistent(self.bot, 2, None)
        self.bot.register_behavior.assert_not_called()
        self.feedback.assert_called_once()

    def test_behavior_is_reused_within_cycle_and_recreated_after_renewal(self):
        action = {"id": "KeepUnitSafe", "args": {"unit": "1"}}
        review = ActionReview([action], [], [], arguments=[{}])
        self.executor.policy.review.return_value = review
        self.executor.adapter.construct = Mock(side_effect=lambda action, kwargs, previous=None: previous or object())
        self.executor.automation.replace_worker_override = Mock()
        self.executor.dispatch(self.bot, 1, review, None)
        self.executor.run_persistent(self.bot, 1, None)
        key = self.executor.action_key(action)
        first = self.executor._persistent_behaviors[key]
        self.executor.run_persistent(self.bot, 2, None)
        self.assertIs(self.executor._persistent_behaviors[key], first)
        self.executor.begin_decision(self.bot)
        self.executor.run_persistent(self.bot, 3, None)
        self.assertEqual(self.bot.register_behavior.call_count, 2)
        self.executor.dispatch(self.bot, 3, review, None)
        self.executor.run_persistent(self.bot, 3, None)
        self.assertIsNot(self.executor._persistent_behaviors[key], first)

    def test_one_time_no_work_reports_failure_once(self):
        action = {"id": "UseAbility", "args": {"unit": "1", "ability": "STOP"}}
        behavior = Mock(execute=Mock(return_value=False))
        self.executor.adapter.construct = Mock(return_value=behavior)
        review = ActionReview([action], [], [], arguments=[{}])
        self.executor.dispatch(self.bot, 1, review, None)
        tracked = self.bot.register_behavior.call_args.args[0]
        tracked.execute(self.bot, {}, None)
        self.executor.run_persistent(self.bot, 2, None)
        behavior.execute.assert_called_once()
        self.bot.register_behavior.assert_called_once()
        self.assertEqual(self.feedback.call_args.args[0]["kind"], "action")
        history = self.executor.observation_builder.action_history.render()
        self.assertIn("failed:", history)
        self.assertNotIn("submitted:", history)

    def test_submission_is_recorded_only_after_successful_execution(self):
        action = {"id": "UseAbility", "args": {"unit": "1", "ability": "STOP"}}
        self.executor.adapter.construct = Mock(return_value=Mock(execute=Mock(return_value=True)))
        review = ActionReview([action], [], [], arguments=[{}])
        self.executor.dispatch(self.bot, 1, review, None)
        history = self.executor.observation_builder.action_history
        self.assertEqual(history.render(), "[None]")
        self.bot.time_formatted = "00:01"
        self.bot.register_behavior.call_args.args[0].execute(self.bot, {}, None)
        self.assertIn("submitted:\n- time@00:01 UseAbility", history.render())
        self.feedback.assert_not_called()

    def test_persistent_no_work_remains_active(self):
        action = {"id": "KeepUnitSafe", "args": {"unit": "1"}}
        review = ActionReview([action], [], [], arguments=[{}])
        self.executor.adapter.construct = Mock(return_value=Mock(execute=Mock(return_value=False)))
        self.executor.policy.review.return_value = review
        self.executor.dispatch(self.bot, 1, review, None)
        self.executor.run_persistent(self.bot, 1, None)
        self.bot.register_behavior.call_args.args[0].execute(self.bot, {}, None)
        self.assertIn("active:", self.executor.observation_builder.action_history.render())
        self.assertEqual(self.executor.persistent_actions.actions, [action])
        self.feedback.assert_not_called()

    def test_build_failure_reports_confirmed_prerequisite(self):
        from sc2.ids.unit_typeid import UnitTypeId
        from sc2.data import Race
        self.bot.race = Race.Terran
        self.bot.can_afford.return_value = True
        self.bot.tech_requirement_progress = Mock(return_value=0.5)
        behavior = SimpleNamespace(structure_id=UnitTypeId.STARPORT, tech_progress_check=0.85)
        reason = self.executor._no_order_reason(self.bot, {'id': 'BuildStructure', 'args': {}}, behavior)
        self.assertIn('prerequisite_unmet: STARPORT requires FACTORY', reason)
        self.assertIn('tech progress=50%, required=85%', reason)

    def test_near_target_and_empty_cargo_have_specific_reasons(self):
        from sc2.position import Point2
        unit = SimpleNamespace(position=Point2((10, 10)), cargo_used=0)
        behavior = SimpleNamespace(unit=unit, target=Point2((11, 10)), success_at_distance=7)
        action = {'id': 'AMove', 'args': {'unit': '12'}}
        self.assertIn('distance=1.00, success_at_distance=7', self.executor._no_order_reason(self.bot, action, behavior))
        action['id'] = 'DropCargo'
        self.assertIn('unit=12 has no cargo', self.executor._no_order_reason(self.bot, action, behavior))

    def test_unknown_execution_cause_is_not_guessed(self):
        reason = self.executor._no_order_reason(self.bot, {'id': 'AttackTarget', 'args': {}}, object())
        self.assertIn('exact cause unavailable', reason)
        self.assertNotIn('resource_insufficient', reason)

    def test_group_attack_duplicate_does_not_report_failure(self):
        from ares.behaviors.combat.group.a_move_group import AMoveGroup
        from sc2.ids.ability_id import AbilityId
        from sc2.position import Point2

        target = Point2((20, 20))
        for position in (Point2((0, 0)), target):
            with self.subTest(position=position):
                unit = SimpleNamespace(position=position, order_target=target,
                                       orders=[SimpleNamespace(ability=SimpleNamespace(id=AbilityId.ATTACK))])
                behavior = AMoveGroup([unit], {1}, target)
                action = {'id': 'AMoveGroup', 'args': {'group': ['1'], 'target': {'x': 20, 'y': 20}}}
                self.executor._register_behaviors(self.bot, 1, action, [behavior], False)
                self.bot.register_behavior.call_args.args[0].execute(self.bot, {}, None)
                self.feedback.assert_not_called()
                self.assertEqual(self.executor.observation_builder.action_history.render(), '[None]')
        behavior.group = []
        self.assertIn('no_order_issued', self.executor._no_order_reason(self.bot, action, behavior))

    def test_enemy_natural_is_resolvable_as_a_target(self):
        from agent.game.observation.execution_context import ExecutionContextBuilder
        from agent.game.actions.resolution.resolver import EntityContext
        from sc2.position import Point2

        self.bot.mediator = SimpleNamespace(get_enemy_nat=Point2((149, 74)))
        context = EntityContext(bot=self.bot)
        ExecutionContextBuilder(TagIdMapper())._add_execution_context(self.bot, context)
        self.assertEqual(context.resolve_point('enemy_natural'), Point2((149, 74)))


if __name__ == "__main__":
    unittest.main()
