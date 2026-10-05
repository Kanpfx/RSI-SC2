"""Concise action error categories survive parsing, resolution and execution."""
import unittest

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
