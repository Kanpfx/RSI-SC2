"""Group membership survives partial failures across decision and execution frames."""

import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import run
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

from agent.config import GameConfig
from agent.game.actions.execution.executor import ActionExecutor
from agent.game.actions.policy.exposure import ActionExposure
from agent.game.actions.policy.validation import PolicyValidator
from agent.game.actions.resolution.loader import ActionCatalog
from agent.game.actions.resolution.resolver import EntityContext
from agent.game.observation.builder import ObservationBuilder
from agent.game.observation.execution_context import TagIdMapper


class GroupFilteringTests(unittest.TestCase):
    def setUp(self):
        self.catalog = ActionCatalog.load()
        self.policy = PolicyValidator(self.catalog, GameConfig())
        self.exposure = ActionExposure(self.catalog)
        self.ground_grid, self.air_grid = object(), object()
        self.bot = SimpleNamespace(
            time=0, time_formatted='00:00', register_behavior=Mock(),
            mediator=SimpleNamespace(get_ground_grid=self.ground_grid, get_air_grid=self.air_grid),
        )
        self.context = EntityContext(bot=self.bot)
        self.add_unit('1')
        self.add_unit('2')
        self.executor = ActionExecutor(
            self.catalog, GameConfig(), policy=self.policy, action_exposure=self.exposure,
            observation_builder=ObservationBuilder(TagIdMapper()),
            automation=SimpleNamespace(worker_action=None), telemetry=Mock(),
            on_feedback=Mock(), request_iteration=lambda: 1,
        )

    def add_unit(self, alias, *, flying=False, speed=3):
        unit = SimpleNamespace(
            tag=int(alias) + 100, type_id=UnitTypeId.MEDIVAC if flying else UnitTypeId.MARINE,
            position=Point2((int(alias), 1)), is_flying=flying, is_structure=False,
            movement_speed=speed, can_attack=not flying, abilities=[], energy=0,
        )
        self.context.own_entities[alias] = unit
        self.context.known_own_aliases.add(alias)
        return unit

    def action(self, name='PathGroupToTarget', group=None):
        return {'id': name, 'args': {'group': ['1', '2'] if group is None else group,
                                    'target': {'x': 20, 'y': 20}}}

    def review(self, actions, *, surface=False):
        return self.policy.review(self.bot, actions, self.context,
                                  self.exposure.build(self.bot, self.context) if surface else None)

    def test_invalid_members_do_not_reject_valid_siblings(self):
        enemy = self.context.own_entities.pop('2')
        self.context.enemy_entities['2'] = enemy
        action = self.action('AMoveGroup', [1, '[1]', '2', '99', True, {}])
        original = deepcopy(action)
        review = self.review([action], surface=True)
        self.assertFalse(review.issues)
        self.assertEqual(review.actions[0]['args']['group'], ['1'])
        self.assertEqual(review.arguments[0][0]['group_tags'], {101})
        self.assertEqual(len(review.notices), 5)
        self.assertEqual(action, original)

    def test_later_group_overrides_earlier_single_member_group(self):
        review = self.review([self.action('AMoveGroup', ['1']), self.action(group=['1', '2'])])
        self.assertFalse(review.issues)
        self.assertEqual(len(review.actions), 1)
        self.assertEqual(review.actions[0]['args']['group'], ['1', '2'])
        self.assertEqual(review.indices, [1])
        self.assertIn('Action 2', review.notices[0].reason)

    def test_empty_or_entirely_invalid_group_is_rejected(self):
        for group in ([], ['99'], ['2']):
            with self.subTest(group=group):
                self.context.own_entities.pop('2', None)
                review = self.review([self.action(group=group)])
                self.assertEqual(review.actions, [])
                self.assertEqual(len(review.issues), 1)

    def test_ability_is_sent_only_to_ready_members(self):
        self.context.own_entities['1'].abilities = [AbilityId.EFFECT_STIM]
        action = {'id': 'GroupUseAbility', 'args': {
            'group': ['1', '2'], 'ability': 'EFFECT_STIM', 'target': None}}
        review = self.review([action], surface=True)
        self.assertFalse(review.issues)
        self.assertEqual(review.actions[0]['args']['group'], ['1'])
        self.executor.dispatch(self.bot, 1, review, self.context)
        self.bot.give_same_action = Mock()
        self.bot.register_behavior.call_args.args[0].execute(self.bot, {}, None)
        self.bot.give_same_action.assert_called_once_with(AbilityId.EFFECT_STIM, {101}, None)

    def test_mixed_group_uses_separate_grids_centers_and_tags(self):
        self.add_unit('2', flying=True)
        review = self.review([self.action()], surface=True)
        self.assertFalse(review.issues)
        ground, air = review.arguments[0]
        self.assertIs(ground['grid'], self.ground_grid)
        self.assertIs(air['grid'], self.air_grid)
        self.assertEqual(ground['group_tags'], {101})
        self.assertEqual(air['group_tags'], {102})
        self.assertEqual(ground['start'], Point2((1, 1)))
        self.assertEqual(air['start'], Point2((2, 1)))
        self.executor.dispatch(self.bot, 1, review, self.context)
        self.executor.run_persistent(self.bot, 1, self.context)
        self.assertEqual(self.bot.register_behavior.call_count, 2)

    def test_dead_member_is_removed_without_failing_remaining_control(self):
        action = self.action()
        self.executor.dispatch(self.bot, 1, self.review([action]), self.context)
        self.executor.run_persistent(self.bot, 1, self.context)
        self.context.own_entities.pop('2')
        self.executor.persistent_actions.prune_missing_actors(self.context.own_entities)
        self.executor.run_persistent(self.bot, 2, self.context)
        controls = self.executor.persistent_actions.actions
        self.assertEqual(len(controls), 1)
        self.assertEqual(controls[0]['args']['group'], ['1'])
        behavior = self.bot.register_behavior.call_args.args[0].behavior
        self.assertEqual(behavior.group_tags, {101})
        self.assertEqual(behavior.start, Point2((1, 1)))
        self.assertNotIn('failed:', self.executor.observation_builder.action_history.render())
        self.context.own_entities.clear()
        self.executor.run_persistent(self.bot, 3, self.context)
        self.assertEqual(self.executor.persistent_actions.actions, [])

    def test_temporary_inability_pauses_members_and_recovers(self):
        action = self.action()
        self.context.own_entities['2'].movement_speed = 0
        review = self.review([action], surface=True)
        self.assertEqual(review.actions[0]['args']['group'], ['1', '2'])
        self.assertEqual([u.tag for u in review.arguments[0][0]['group']], [101])
        self.executor.dispatch(self.bot, 1, review, self.context)
        self.context.own_entities['1'].movement_speed = 0
        self.executor.run_persistent(self.bot, 2, self.context)
        self.bot.register_behavior.assert_not_called()
        self.assertEqual(self.executor.persistent_actions.actions, [action])
        self.context.own_entities['2'].movement_speed = 3
        self.executor.run_persistent(self.bot, 3, self.context)
        self.assertEqual(self.bot.register_behavior.call_args.args[0].behavior.group_tags, {102})
        self.assertEqual(self.executor.persistent_actions.actions, [action])

    def test_one_time_action_filters_incapable_members(self):
        self.context.own_entities['2'].movement_speed = 0
        review = self.review([self.action('AMoveGroup')])
        self.assertFalse(review.issues)
        self.assertEqual(review.actions[0]['args']['group'], ['1'])
        self.context.own_entities['1'].movement_speed = 0
        self.assertTrue(self.review([self.action('AMoveGroup')]).issues)

    def test_persistent_ability_cooldown_keeps_members_until_ready(self):
        self.catalog.get('GroupUseAbility')['persistent'] = True
        action = {'id': 'GroupUseAbility', 'args': {
            'group': ['1', '2'], 'ability': 'EFFECT_STIM', 'target': None}}
        review = self.review([action], surface=True)
        self.assertFalse(review.issues)
        self.assertEqual(review.arguments, [[]])
        self.executor.dispatch(self.bot, 1, review, self.context)
        self.executor.run_persistent(self.bot, 2, self.context)
        self.bot.register_behavior.assert_not_called()
        self.assertEqual(self.executor.persistent_actions.actions, [action])
        self.context.own_entities['1'].abilities = [AbilityId.EFFECT_STIM]
        self.executor.run_persistent(self.bot, 3, self.context)
        self.assertEqual(self.bot.register_behavior.call_args.args[0].behavior.group_tags, {101})
        self.assertEqual(self.executor.persistent_actions.actions, [action])

    def test_construction_failure_clears_the_updated_group(self):
        self.executor.dispatch(self.bot, 1, self.review([self.action()]), self.context)
        self.context.own_entities.pop('2')
        self.executor.adapter.construct = Mock(side_effect=ValueError('construction failed'))
        self.executor.run_persistent(self.bot, 2, self.context)
        self.assertEqual(self.executor.persistent_actions.actions, [])
        self.assertEqual(self.executor._persistent_behaviors, {})
