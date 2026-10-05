"""Contracts of clean action tables and their native Ares adapter."""
import inspect
import unittest
from types import SimpleNamespace

import run
from sc2.position import Point2

from agent.game.actions.execution.adapter import AresActionAdapter, behavior_class
from agent.game.actions.resolution.loader import ActionCatalog
from agent.game.actions.resolution.resolver import EntityContext
from agent.harness.action_reference import _actions_reference


class CleanCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = ActionCatalog.load()
        self.adapter = AresActionAdapter(self.catalog)

    def test_all_tables_match_installed_ares_constructor_fields(self):
        self.assertEqual(len(self.catalog.entries), 47)
        for entry in self.catalog.entries.values():
            with self.subTest(action=entry['name']):
                params = inspect.signature(behavior_class(entry['import'])).parameters
                self.assertEqual(set(params), {p['name'] for p in entry['params']})
                for p in entry['params']:
                    self.assertEqual(p['required'], params[p['name']].default is inspect.Parameter.empty)

    def test_fixed_values_are_injected_and_cannot_be_submitted(self):
        action = {'id': 'GasBuildingController', 'args': {'to_count': 2}}
        normalized, kwargs = self.adapter.prepare(action, EntityContext())
        self.assertEqual(normalized, action)
        self.assertEqual(kwargs['max_pending'], 1)
        with self.assertRaisesRegex(ValueError, 'max_pending'):
            self.adapter.prepare({'id': action['id'], 'args': {'to_count': 2, 'max_pending': 2}}, EntityContext())

    def test_runtime_grid_follows_actor_and_rejects_mixed_group(self):
        air, ground = object(), object()
        bot = SimpleNamespace(mediator=SimpleNamespace(get_air_grid=air, get_ground_grid=ground))
        context = EntityContext(bot=bot)
        flying = SimpleNamespace(is_flying=True, tag=1, position=Point2((2, 4)))
        walking = SimpleNamespace(is_flying=False, tag=2, position=Point2((4, 6)))
        self.assertIs(context.runtime_value('unit_grid', {'unit': flying}), air)
        self.assertIs(context.runtime_value('unit_grid', {'unit': walking}), ground)
        self.assertEqual(context.runtime_value('group_center', {'group': [flying, walking]}), Point2((3, 5)))
        with self.assertRaisesRegex(ValueError, 'mixed air/ground'):
            context.runtime_value('group_grid', {'group': [flying, walking]})

    def test_optional_target_is_exposed_and_private_state_is_retained(self):
        text = _actions_reference([self.catalog.get('UseAbility')])
        self.assertIn('target: Point2 | Unit | null = null', text)
        action = {'id': 'SpawnController', 'args': {'army_composition_dict': {'MARINE': {'proportion': 1.0, 'priority': 0}}}}
        _, kwargs = self.adapter.prepare(action, EntityContext())
        first = self.adapter.construct(action, kwargs)
        first._SpawnController__supply_available = 123
        second = self.adapter.construct(action, kwargs)
        self.assertIsNot(first._SpawnController__build_dict, second._SpawnController__build_dict)
        self.assertIs(self.adapter.construct(action, kwargs, first), first)
        self.assertEqual(first._SpawnController__supply_available, 123)
