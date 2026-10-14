"""Concise action error categories survive parsing, resolution and execution."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import run
from agent.game.actions.errors import (
    ActionNameError, AvailabilityError, ConflictError, ParameterError,
    ResourceError, exception_reason,
)
from agent.game.actions.parser import parse_model_payload
from agent.game.actions.resolution.resolver import EntityContext
from agent.game.actions.resolution.types import TypeResolver


class ActionErrorTests(unittest.TestCase):
    def test_common_coordinate_forms(self):
        resolver = TypeResolver()
        for coordinate in ('{x:40,y:60}', '{X:40,Y:60}', '(40,60)', '[40,60]'):
            with self.subTest(coordinate=coordinate):
                action = parse_model_payload(f'# actions\nAMove(unit=12,target={coordinate})')['actions'][0]
                wire, _ = resolver.resolve(action['args']['target'], 'Point2', EntityContext(), 'target')
                self.assertEqual(wire, {'x': 40.0, 'y': 60.0})
        for value in ([1], [1, 2, 3], {'x': 1, 'X': 2, 'y': 3}, [True, 2]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                EntityContext().resolve_point(value)

    def test_action_section_extraction(self):
        for text in (
            'Explanation\n```dsl\n# ACTIONS\nBuildWorkers(to_count=20)\n```\nAfterword',
            '# working\nPlan\n# actions\nBuildWorkers(to_count=20)\n# notes\nAfterword',
            '# actions\n```dsl\nBuildWorkers(to_count=20)\n```\nAfterword',
        ):
            with self.subTest(text=text):
                payload = parse_model_payload(text)
                self.assertEqual(payload['actions'], [{'id': 'BuildWorkers', 'args': {'to_count': 20}}])
                self.assertEqual(payload['errors'], [])

    def test_common_categories(self):
        cases = [
            (ParameterError.missing('target'), "format_error: 'target' is required"),
            (ActionNameError.unknown('Bad'), 'action_unavailable:'),
            (AvailabilityError.unavailable('AMove', 'actor cannot move'), 'action_unavailable:'),
            (ConflictError.unit_reused(12), 'action_conflict:'),
            (ResourceError.insufficient('BuildStructure'), 'resource_insufficient:'),
            (RuntimeError('failed'), 'execution_error: RuntimeError: failed'),
        ]
        for error, expected in cases:
            with self.subTest(error=error):
                self.assertTrue(exception_reason(error).startswith(expected))

    def test_repeated_headings_use_first_section(self):
        payload = parse_model_payload(
            '# working\nFirst plan\n# working\nSecond plan\n'
            '# ACTIONS\nBuildWorkers(to_count=20)\n'
            '# actions\nBuildWorkers(to_count=30)'
        )
        self.assertEqual(payload['actions'], [{'id': 'BuildWorkers', 'args': {'to_count': 20}}])
        self.assertEqual(payload['errors'], [])
        self.assertEqual(parse_model_payload('# actions\n# actions\nBuildWorkers(to_count=30)')['actions'], [])

    def test_missing_entity_survives_union_resolution(self):
        context = EntityContext(known_own_aliases={'12'})
        for expression in ('Unit', 'Point2 | Unit | null'):
            with self.subTest(expression=expression), self.assertRaisesRegex(
                ValueError, 'entity_missing: unit 12: unit disappeared'
            ):
                TypeResolver().resolve(12, expression, context, 'target')

    def test_parse_failure_keeps_valid_sibling(self):
        payload = parse_model_payload('# actions\nBad(unit=lookup(1))\nBuildWorkers(to_count=20)')
        self.assertEqual(len(payload['actions']), 1)
        self.assertTrue(payload['errors'][0]['error'].startswith('format_error:'))

    def test_resource_feedback_reports_cost_balance_and_shortfall(self):
        bot = SimpleNamespace(minerals=100, vespene=60,
                              calculate_cost=Mock(return_value=SimpleNamespace(minerals=150, vespene=100)))
        reason = str(ResourceError.for_cost(bot, SimpleNamespace(name='TEST_STRUCTURE')))
        self.assertIn('TEST_STRUCTURE requires 150 minerals, 100 gas', reason)
        self.assertIn('available 100 minerals, 60 gas', reason)
        self.assertIn('missing 50 minerals, 40 gas', reason)

    def test_missing_entity_reports_parameter_path(self):
        with self.assertRaisesRegex(ValueError, "parameter 'targets\\[0\\]'"):
            TypeResolver().resolve(['99'], 'list[Unit]', EntityContext(), 'targets')

    def test_conflict_identifies_previous_action(self):
        reason = str(ConflictError.unit_reused('12', 'PathGroupToTarget', 2))
        self.assertIn("unit '12'", reason)
        self.assertIn('Action 2: PathGroupToTarget', reason)

    def test_validation_conflict_retains_the_later_control(self):
        from agent.config import GameConfig
        from agent.game.actions.policy.validation import PolicyValidator
        from agent.game.actions.resolution.loader import ActionCatalog
        from sc2.ids.unit_typeid import UnitTypeId
        unit = SimpleNamespace(tag=100, type_id=UnitTypeId.MARINE)
        context = EntityContext(own_entities={'12': unit})
        validator = PolicyValidator(ActionCatalog.load(), GameConfig())
        validator.adapter.prepare = Mock(side_effect=lambda action, context: (action, {'unit': unit}))
        actions = [{'id': 'KeepUnitSafe', 'args': {'unit': '12'}}, {'id': 'AMove', 'args': {'unit': '12'}}]
        review = validator.review(SimpleNamespace(), actions, context)
        self.assertEqual(len(review.actions), 1)
        self.assertEqual(review.actions, [actions[1]])
        self.assertFalse(review.issues)
        self.assertEqual(review.indices, [1])
        self.assertIn('Action 2: AMove', review.notices[0].reason)

    def test_ability_feedback_includes_actor_and_observed_energy(self):
        from agent.game.actions.policy.rules import validate_resolved
        from agent.game.actions.resolution.loader import ActionCatalog
        from sc2.ids.unit_typeid import UnitTypeId
        from sc2.ids.ability_id import AbilityId
        unit = SimpleNamespace(tag=100, type_id=UnitTypeId.GHOST, abilities=[], energy=50)
        context = EntityContext(own_entities={'12': unit})
        with self.assertRaisesRegex(ValueError, 'EMP_EMP.*12.*energy=50'):
            validate_resolved(ActionCatalog.load().get('UseAbility'),
                              {'unit': unit, 'ability': AbilityId.EMP_EMP}, context)
