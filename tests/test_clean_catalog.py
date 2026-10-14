"""Contracts of clean action tables and their native Ares adapter."""
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import run
from sc2.position import Point2
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.ability_id import AbilityId
from sc2.units import Units

from agent.game.actions.execution.adapter import AresActionAdapter, behavior_class
from agent.game.actions.resolution.loader import ActionCatalog
from agent.game.actions.resolution.resolver import EntityContext
from agent.game.actions.policy.exposure import ActionExposure
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

    def test_individual_actions_only_disable_zerg_exclusive_behaviors(self):
        disabled = {entry['name'] for entry in self.catalog.entries.values()
                    if '.combat.individual.' in entry['import'] and not entry['enabled']}
        self.assertEqual(disabled, {'NydusPathUnitToTarget', 'QueenSpreadCreep',
                                    'TumorSpreadCreep', 'UseTransfuse'})

    def test_predictive_aoe_builds_native_path_without_cached_runtime(self):
        unit = SimpleNamespace(tag=1, position=Point2((2, 4)), is_flying=False)
        enemy = SimpleNamespace(tag=2, position=Point2((40, 4)))
        path = [Point2((x, 4)) for x in range(2, 41)]
        grid = object()
        finder = Mock(return_value=path)
        context = EntityContext(own_entities={'1': unit}, enemy_entities={'2': enemy},
                                bot=SimpleNamespace(mediator=SimpleNamespace(
                                    get_ground_grid=grid, find_raw_path=finder)))
        action = {'id': 'PlacePredictiveAoE', 'args': {
            'unit': '1', 'enemy_center_unit': '2', 'aoe_ability': 'KD8CHARGE_KD8CHARGE'}}
        _, kwargs = self.adapter.prepare(action, context)
        finder.assert_called_once_with(start=unit.position, target=enemy.position,
                                       grid=grid, sensitivity=1)
        self.assertEqual(kwargs['path'], path[:30])
        self.assertEqual(kwargs['ability_delay'], 34)
        self.assertEqual(self.adapter.construct(action, kwargs).aoe_ability,
                         AbilityId.KD8CHARGE_KD8CHARGE)
        finder.return_value = None
        with self.assertRaisesRegex(ValueError, 'runtime dependency unavailable'):
            self.adapter.prepare(action, context)
        finder.return_value = path
        action['args']['aoe_ability'] = 'EMP_EMP'
        with self.assertRaisesRegex(ValueError, 'runtime dependency unavailable'):
            self.adapter.prepare(action, context)

    def test_addon_swap_accepts_structure_type(self):
        action = {'id': 'AddonSwap', 'args': {
            'structure_needing_addon': 'FACTORY', 'addon_required': 'REACTOR'}}
        _, kwargs = self.adapter.prepare(action, EntityContext())
        self.assertEqual(kwargs['structure_needing_addon'], UnitTypeId.FACTORY)
        self.assertEqual(self.adapter.construct(action, kwargs).structure_needing_addon,
                         UnitTypeId.FACTORY)
        factory = SimpleNamespace(tag=1, type_id=UnitTypeId.FACTORY)
        context = EntityContext(own_entities={'1': factory})
        entry = self.catalog.get('AddonSwap')
        entry['enabled'] = True
        surface = ActionExposure(self.catalog).build(SimpleNamespace(), context)
        surface.validate(entry, action['args'])
        self.assertIn('FACTORY', surface.parameter_domains['AddonSwap']['structure_needing_addon'])

    def test_command_center_upgrade_rejects_unrelated_unit_types(self):
        for target in ('ORBITALCOMMAND', 'PLANETARYFORTRESS'):
            action = {'id': 'UpgradeCCs', 'args': {'to': target}}
            _, kwargs = self.adapter.prepare(action, EntityContext())
            self.assertEqual(self.adapter.construct(action, kwargs).to, UnitTypeId[target])
        with self.assertRaisesRegex(ValueError, 'ORBITALCOMMAND or PLANETARYFORTRESS'):
            self.adapter.prepare({'id': 'UpgradeCCs', 'args': {'to': 'MARINE'}}, EntityContext())

    def test_exposure_preserves_group_candidates_and_fixed_ability_readiness(self):
        raven = SimpleNamespace(tag=1, type_id=UnitTypeId.RAVEN, abilities=[])
        marine = SimpleNamespace(tag=2, type_id=UnitTypeId.MARINE)
        context = EntityContext(own_entities={'1': raven, '2': marine})
        exposure = ActionExposure(self.catalog)
        bot = SimpleNamespace()
        surface = exposure.build(bot, context)
        self.assertEqual(surface.parameter_domains['GroupUseAbility']['group'], {'1', '2'})
        self.assertNotIn('RavenAutoTurret', surface.action_ids)
        raven.abilities = [AbilityId.BUILDAUTOTURRET_AUTOTURRET]
        surface = exposure.build(bot, context)
        self.assertEqual(surface.parameter_domains['RavenAutoTurret']['unit'], {'1'})

    def test_group_enemy_lists_accept_native_units_branch(self):
        enemy = SimpleNamespace(tag=2, position=Point2((4, 6)))
        context = EntityContext(enemy_entities={'2': enemy}, bot=SimpleNamespace())
        for action, param in [('KeepGroupSafe', 'close_enemy'),
                              ('StutterGroupForward', 'enemies')]:
            spec = next(p for p in self.catalog.get(action)['params'] if p['name'] == param)
            _, native = self.catalog.type_resolver.resolve(['2'], spec['type'], context, param)
            self.assertIsInstance(native, Units)
            self.assertEqual(list(native), [enemy])

    def test_speed_mining_resolves_coordinates_before_runtime(self):
        worker = SimpleNamespace(tag=1, position=Point2((2, 4)))
        mineral = SimpleNamespace(tag=2, position=Point2((4, 6)), is_mineral_field=True)
        gas = SimpleNamespace(tag=3, position=Point2((8, 6)), is_gas_building=True)
        approach = Point2((3, 5))
        bot = SimpleNamespace(mineral_field=[mineral], vespene_geyser=[],
                              mediator=SimpleNamespace(get_mineral_target_dict={mineral.position: approach}))
        context = EntityContext(own_entities={'1': worker, '3': gas}, bot=bot)
        for resource in (mineral, gas):
            action = {'id': 'SpeedMining', 'args': {
                'worker': '1', 'target': {'x': resource.position.x, 'y': resource.position.y}}}
            _, kwargs = self.adapter.prepare(action, context)
            self.assertIs(kwargs['target'], resource)
            self.assertEqual(kwargs['worker_position'], worker.position)
            self.assertEqual(kwargs['resource_target_pos'], approach if resource is mineral
                             else gas.position.towards(worker.position, 2.75 * 1.21))
            self.assertIs(self.adapter.construct(action, kwargs).target, resource)
        with self.assertRaisesRegex(ValueError, 'exact coordinates'):
            context.resolve_mining_target(Point2((20, 20)))

    def test_speed_mining_geyser_uses_ares_approach_lookup(self):
        geyser = SimpleNamespace(tag=2, position=Point2((4, 6)), is_vespene_geyser=True)
        approach = Point2((3, 5))
        bot = SimpleNamespace(mineral_field=[], vespene_geyser=[geyser],
                              mediator=SimpleNamespace(get_mineral_target_dict={geyser.position: approach}))
        context = EntityContext(bot=bot)
        target = context.resolve_mining_target(geyser.position)
        self.assertIs(target, geyser)
        self.assertEqual(context.runtime_value('resource_target_pos', {'target': target}), approach)

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
