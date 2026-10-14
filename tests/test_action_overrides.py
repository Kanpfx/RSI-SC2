"""Later legal instructions replace only the control scope they own."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import run
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

from agent.config import GameConfig, LLMConfig
from agent.game.actions.execution.lifecycle import PersistentActionRegistry
from agent.game.actions.policy.validation import PolicyValidator
from agent.game.actions.resolution.loader import ActionCatalog
from agent.game.actions.resolution.resolver import EntityContext
from agent.harness.controller import LLMGameController
from agent.harness.llm import ModelResult


class ActionOverrideTests(unittest.TestCase):
    def setUp(self):
        self.catalog = ActionCatalog.load()
        self.policy = PolicyValidator(self.catalog, GameConfig())
        self.bot = SimpleNamespace(mediator=SimpleNamespace(get_ground_grid=object()))
        self.context = EntityContext(own_entities={
            str(i): SimpleNamespace(tag=100+i, type_id=UnitTypeId.MARINE,
                position=Point2((i, 1)), is_flying=False, is_structure=False,
                movement_speed=3, can_attack=True, abilities=[]) for i in (1, 2, 3)
        })

    def group(self, members):
        return {'id': 'PathGroupToTarget', 'args': {'group': members, 'target': 'main'}}

    def single(self, member):
        return {'id': 'AMove', 'args': {'unit': member, 'target': 'main'}}

    def review(self, actions):
        self.context.positions['main'] = Point2((20, 20))
        return self.policy.review(self.bot, actions, self.context)

    def test_later_single_preserves_other_members_of_earlier_group(self):
        review = self.review([self.group(['1', '2']), self.single('2')])
        self.assertFalse(review.issues)
        self.assertEqual(review.indices, [0, 1])
        self.assertEqual(review.actions[0]['args']['group'], ['1'])
        self.assertEqual(review.arguments[0][0]['group_tags'], {101})
        self.assertEqual(review.arguments[0][0]['start'], Point2((1, 1)))

    def test_later_group_replaces_single_and_part_of_another_group(self):
        review = self.review([self.single('1'), self.group(['2', '3']), self.group(['1', '2'])])
        self.assertFalse(review.issues)
        self.assertEqual(review.indices, [1, 2])
        self.assertEqual([a['args']['group'] for a in review.actions], [['3'], ['1', '2']])

    def test_illegal_later_instruction_does_not_claim_units(self):
        later = self.single('1')
        later['args']['target'] = 'unknown_landmark'
        review = self.review([self.group(['1', '2']), later])
        self.assertEqual(review.indices, [0])
        self.assertEqual(review.actions[0]['args']['group'], ['1', '2'])
        self.assertEqual(review.issues[0].index, 1)

    def test_only_valid_members_of_later_group_override(self):
        self.context.own_entities['2'].movement_speed = 0
        later = {'id': 'AMoveGroup', 'args': {'group': ['1', '2', '99'], 'target': 'main'}}
        review = self.review([self.group(['1', '2']), later])
        self.assertFalse(review.issues)
        self.assertEqual([a['args']['group'] for a in review.actions], [['2'], ['1']])

    def test_global_macro_configuration_is_replaced_as_a_whole(self):
        actions = [{'id': 'BuildWorkers', 'args': {'to_count': count}} for count in (20, 30)]
        review = self.review(actions)
        self.assertFalse(review.issues)
        self.assertEqual(review.actions, [actions[1]])
        self.assertEqual(review.indices, [1])

    def test_illegal_macro_configuration_does_not_replace_previous_one(self):
        actions = [{'id': 'BuildWorkers', 'args': {'to_count': count}} for count in (20, -1)]
        review = self.review(actions)
        self.assertEqual(review.actions, [actions[0]])
        self.assertEqual(review.issues[0].index, 1)

    def test_tech_targets_coexist_and_same_target_uses_latest_configuration(self):
        actions = [{'id': 'TechUp', 'args': {'desired_tech': tech, 'base_location': point}}
                   for tech, point in [('MARAUDER', 'main'), ('SIEGETANK', 'main'),
                                       ('marauder', {'x': 30, 'y': 30})]]
        review = self.review(actions)
        self.assertFalse(review.issues)
        self.assertEqual(review.indices, [1, 2])
        registry = PersistentActionRegistry(self.catalog)
        for action in review.actions:
            registry.replace(action)
        self.assertEqual(len(registry.actions), 2)
        registry.replace({'id': 'TechUp', 'args': {'desired_tech': 'MARAUDER', 'base_location': 'main'}})
        self.assertEqual(len(registry.actions), 2)

    def test_repeated_one_time_macro_submissions_remain_independent(self):
        action = {'id': 'BuildStructure', 'args': {'structure_id': 'BARRACKS', 'base_location': 'main'}}
        review = self.review([action, action])
        self.assertFalse(review.issues)
        self.assertEqual(review.indices, [0, 1])
        self.assertEqual(len(review.actions), 2)
        self.assertIsNone(self.catalog.override_key(review.actions[0]))

    def test_registry_uses_actor_parameter_for_worker_controls(self):
        registry = PersistentActionRegistry(self.catalog)
        worker = {'id': 'SpeedMining', 'args': {'worker': '1', 'target': 'main'}}
        registry.replace(worker)
        registry.replace(self.group(['1', '2']))
        self.assertEqual([a['id'] for a in registry.actions], ['PathGroupToTarget'])

    def test_override_keys_must_refer_to_model_parameters(self):
        entry = self.catalog.get('TechUp')
        entry['override_keys'] = ['not_a_parameter']
        with self.assertRaisesRegex(ValueError, 'invalid override_keys'):
            ActionCatalog(self.catalog.entries)


class OverrideReportingTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_indices_and_full_replacement_across_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = LLMGameController(GameConfig(), LLMConfig(), log_directory=Path(directory))
            bot = SimpleNamespace(time=1, time_formatted='00:01',
                                  chat_send=AsyncMock(), register_behavior=Mock())
            controller.observation_builder.execution_context = Mock(return_value=EntityContext())
            controller.action_exposure.build = Mock(return_value=SimpleNamespace(validate=Mock()))
            controller._request_iteration = 1
            actions = [{'id': 'BuildWorkers', 'args': {'to_count': count}} for count in (20, 30)]
            controller._pending = asyncio.get_running_loop().create_future()
            controller._pending.set_result(ModelResult(actions, [], 0, parse_report={'sources': [
                {'parsed_index': 1, 'source_index': 1, 'source': 'BuildWorkers(to_count=20)'},
                {'parsed_index': 2, 'source_index': 3, 'source': 'BuildWorkers(to_count=30)'},
            ]}))
            await controller._apply_pending(bot, 2)
            self.assertEqual(controller.automation.worker_target, 30)
            rows = [json.loads(line) for line in (Path(directory) / 'accepted_actions.jsonl').read_text().splitlines()]
            decision = next(row for row in rows if 'action_states' in row)
            self.assertEqual(len(decision['action_states']), 1)
            self.assertEqual(decision['action_states'][0]['action_index'], 3)
            self.assertEqual(decision['action_states'][0]['parsed_index'], 2)
            validated = json.loads((Path(directory) / 'model.jsonl').read_text().splitlines()[0])
            self.assertEqual([row['status'] for row in validated['validation_report']], ['superseded', 'passed'])
            controller._pending = asyncio.get_running_loop().create_future()
            controller._pending.set_result(ModelResult([], [], 0))
            await controller._apply_pending(bot, 3)
            self.assertIsNone(controller.automation.worker_target)
            self.assertEqual(controller.executor.persistent_actions.actions, [])
            await controller.close()

    async def test_identical_one_time_submissions_keep_distinct_source_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = LLMGameController(GameConfig(), LLMConfig(), log_directory=Path(directory))
            bot = SimpleNamespace(time=1, time_formatted='00:01', can_afford=Mock(return_value=True),
                                  chat_send=AsyncMock(), register_behavior=Mock())
            context = EntityContext(positions={'main': Point2((20, 20))})
            controller.observation_builder.execution_context = Mock(return_value=context)
            controller.action_exposure.build = Mock(return_value=SimpleNamespace(validate=Mock()))
            action = {'id': 'BuildStructure', 'args': {'structure_id': 'BARRACKS', 'base_location': 'main'}}
            controller._pending = asyncio.get_running_loop().create_future()
            controller._pending.set_result(ModelResult([action, action], [], 0))
            await controller._apply_pending(bot, 2)
            decision = json.loads((Path(directory) / 'accepted_actions.jsonl').read_text().splitlines()[0])
            states = decision['action_states']
            self.assertEqual([row['action_index'] for row in states], [1, 2])
            self.assertEqual([row['parsed_index'] for row in states], [1, 2])
            self.assertNotEqual(states[0]['action_id'], states[1]['action_id'])
            self.assertEqual(bot.register_behavior.call_count, 2)
            await controller.close()
