import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import run
from ares.consts import UnitRole
from sc2.position import Point2
from agent.runtime.scouting import ScoutController


class ScoutTests(unittest.TestCase):
    def test_route_role_timeout_and_replacement(self):
        main, enemy, expansion = Point2((0, 0)), Point2((50, 50)), Point2((40, 40))
        worker = SimpleNamespace(tag=1, is_carrying_resource=False,
                                 is_constructing_scv=False, distance_to=lambda p: main.distance_to(p),
                                 move=Mock())

        class Workers(list):
            def find_by_tag(self, tag):
                return next((w for w in self if w.tag == tag), None)

        bot = SimpleNamespace(time=59, workers=Workers([worker] * 12), start_location=main,
                              enemy_start_locations=[enemy], expansion_locations_list=[main, expansion, enemy],
                              mediator=SimpleNamespace(get_units_from_role=Mock(return_value=[worker]),
                                                       assign_role=Mock()))
        scout = ScoutController()
        scout.run(bot)
        worker.move.assert_not_called()
        bot.time = 60
        scout.run(bot)
        self.assertEqual(scout.route, [enemy, expansion])
        bot.mediator.assign_role.assert_called_once_with(tag=1, role=UnitRole.MAP_CONTROL)
        worker.move.assert_called_with(enemy)
        bot.time = 70
        scout.run(bot)
        worker.move.assert_called_with(expansion)
        bot.time = 80
        scout.run(bot)
        worker.move.assert_called_with(enemy)
        bot.workers.clear()
        bot.time = 81
        scout.run(bot)
        self.assertIsNone(scout.tag)
        bot.workers.extend([worker] * 12)
        bot.time = 90
        scout.run(bot)
        self.assertIsNone(scout.tag)
        bot.time = 91
        scout.run(bot)
        self.assertEqual(scout.tag, 1)
