"""Focused checks for compact observations without SC2 or model calls."""

import unittest
from types import SimpleNamespace as NS

from agent.runtime.observation.builder import ObservationBuilder
from agent.runtime.observation.overview import OverviewBuilder
from agent.runtime.observation.renderer import observation_text
from agent.runtime.observation.state import TagIdMapper
from agent.runtime.actions.resolution.resolver import EntityContext


def entity(tag, name, x=10, health=100, **kwargs):
    return NS(tag=tag, type_id=NS(name=name), position=NS(x=x, y=10),
              health=health, health_max=100, **kwargs)


class CompactObservationTests(unittest.TestCase):
    def setUp(self):
        self.builder = ObservationBuilder(TagIdMapper())
        self.context = EntityContext()
        self.bot = NS(start_location=NS(x=10, y=10))

    def test_workers_aggregate_but_scouts_and_controlled_workers_keep_ids(self):
        workers = [entity(1, "SCV"), entity(2, "SCV")]
        self.builder.entities._role_labels = lambda bot: {}
        blocks = self.builder.entities.own_unit_blocks(workers, self.context, self.bot)
        self.assertEqual(blocks, ["SCV*2 @main(6,6) working"])
        alias = self.builder.ids.alias(1)
        self.builder.sync_active_actions([{"id": "move", "args": {"unit": alias}}])
        self.assertIn(f"SCV[{alias},", self.builder.entities.own_unit_blocks(workers, self.context, self.bot)[0])
        self.builder.sync_active_actions([])
        self.builder.entities._role_labels = lambda bot: {1: "scouting"}
        blocks = self.builder.entities.own_unit_blocks(workers, self.context, self.bot)
        self.assertTrue(any(f"SCV[{alias}]" in block and "scouting" in block for block in blocks))

    def test_important_units_preserve_order_and_unknown_values(self):
        units = [entity(20, "BATTLECRUISER", health=100), entity(10, "BATTLECRUISER", health=10)]
        units[0].health = None
        blocks = self.builder.entities.own_unit_blocks(units, self.context, self.bot)
        self.assertEqual(len(blocks), 1)
        self.assertIn("health=[10%,?]", blocks[0])
        self.assertIn("Tactical Jump=[", blocks[0])
        self.assertEqual([unit.tag for unit in self.context.own_entities.values()], [10, 20])

    def test_combat_groups_keep_ids_and_separate_distant_positions(self):
        units = [entity(1, "MARINE"), entity(2, "MARINE", x=100)]
        blocks = self.builder.entities.own_unit_blocks(units, self.context, self.bot)
        self.assertEqual(len(blocks), 2)
        self.assertTrue(all("Marine[" in block for block in blocks))
        self.assertNotEqual(blocks[0].split("@")[1], blocks[1].split("@")[1])

    def test_structures_preserve_damage_progress_and_addons(self):
        structures = [entity(1, "SUPPLYDEPOT"), entity(2, "SUPPLYDEPOT"),
                      entity(3, "SUPPLYDEPOT", x=30, health=30),
                      entity(4, "STARPORT", build_progress=0.7, has_techlab=True)]
        blocks = self.builder.entities.structure_blocks(structures, self.context, self.bot, own=True)
        self.assertTrue(any("Supply Depot*2" in block for block in blocks))
        self.assertTrue(any("Supply Depot[" in block and "health=[30%]" in block for block in blocks))
        self.assertTrue(any("building=70% addon=TechLab" in block for block in blocks))

    def test_memory_has_counts_and_age_but_no_selectable_ids(self):
        units = [entity(1, "MARINE", age=8), entity(2, "MARINE", age=12)]
        blocks = self.builder.entities.remembered_enemy_unit_blocks(units, self.bot)
        self.assertEqual(len(blocks), 1)
        self.assertIn("Marine*2", blocks[0])
        self.assertIn("last_seen=8-12s_ago", blocks[0])
        self.assertNotIn("[", blocks[0])

    def test_overview_keeps_resources_income_saturation_and_supply_alert(self):
        bot = NS(time_formatted="04:30", minerals=450, vespene=180,
                 supply_used=56, supply_cap=56, supply_army=24,
                 workers=[NS(is_idle=True), NS(is_idle=False)],
                 townhalls=[NS(is_ready=True, assigned_harvesters=16, ideal_harvesters=16)],
                 state=NS(score=NS(collection_rate_minerals=850, collection_rate_vespene=240)))
        overview = OverviewBuilder().build(bot, [], [], [], [])
        self.assertIn("time=04:30 minerals=450 vespene=180 Supply=56/56", overview["resources"])
        self.assertIn("income=850/240/min", overview["resources"])
        self.assertIn("supply_blocked", overview["resources"])
        self.assertIn("idle=1", overview["economy"])
        self.assertIn("main 16/16", overview["economy"])

    def test_enemy_shield_damage_and_rendered_sections(self):
        unit = entity(1, "STALKER", shield=20, shield_max=80)
        blocks = self.builder.entities.enemy_unit_blocks([unit], self.context, self.bot)
        self.assertIn("shield=[25%]", blocks[0])
        data = dict.fromkeys((
            "own_unit_blocks", "own_structure_blocks", "enemy_structure_blocks",
            "remembered_enemy_unit_blocks", "remembered_enemy_structure_blocks",
            "production_and_technology", "recent_changes", "action_history",
        ), [])
        data.update(overview={"resources": "time=00:10 Supply=8/13", "economy": "workers=8",
                              "match": "matchup=TerranvsProtoss", "military": ""},
                    situational_hints={}, enemy_unit_blocks=blocks)
        text = observation_text(data)
        self.assertIn("# overview\ntime=00:10 Supply=8/13", text)
        self.assertIn(blocks[0], text)
        self.assertIn("\n\n# memory_enemy_state\n", text)
        self.assertIn("\n\n# own_state\n## units\n", text)
        self.assertNotRegex(text, r"</?\w+>")
        self.assertNotIn("\n\n\n", text)
