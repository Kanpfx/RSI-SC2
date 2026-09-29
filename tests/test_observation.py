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

    def test_workers_aggregate_by_area_with_ids_for_each_status(self):
        workers = [entity(1, "SCV"), entity(2, "SCV")]
        self.builder.entities._role_labels = lambda bot: {}
        blocks = self.builder.entities.own_unit_blocks(workers, self.context, self.bot)
        self.assertEqual(blocks, ["SCV@main(10,10) working:[0,1]"])
        alias = self.builder.ids.alias(1)
        self.builder.sync_active_actions([{"id": "move", "args": {"unit": alias}}])
        self.assertIn(f"working:[{alias},", self.builder.entities.own_unit_blocks(workers, self.context, self.bot)[0])
        self.builder.sync_active_actions([])
        self.builder.entities._role_labels = lambda bot: {1: "scouting"}
        blocks = self.builder.entities.own_unit_blocks(workers, self.context, self.bot)
        self.assertEqual(blocks, [f"SCV@main(10,10) scouting:[{alias}]; working:[1]"])

    def test_important_units_preserve_order_and_unknown_values(self):
        units = [entity(20, "BATTLECRUISER", health=100), entity(10, "BATTLECRUISER", health=10)]
        units[0].health = None
        blocks = self.builder.entities.own_unit_blocks(units, self.context, self.bot)
        self.assertEqual(len(blocks), 1)
        self.assertIn("hp=[10%,?]", blocks[0])
        self.assertIn("EFFECT_TACTICALJUMP=[", blocks[0])
        self.assertEqual([unit.tag for unit in self.context.own_entities.values()], [10, 20])

    def test_combat_groups_keep_ids_and_separate_distant_positions(self):
        units = [entity(1, "MARINE"), entity(2, "MARINE", x=100)]
        blocks = self.builder.entities.own_unit_blocks(units, self.context, self.bot)
        self.assertEqual(len(blocks), 2)
        self.assertTrue(all("MARINE@" in block and "hp=[100%]" in block for block in blocks))
        self.assertNotEqual(blocks[0].split("@")[1], blocks[1].split("@")[1])

    def test_structures_preserve_damage_progress_and_addons(self):
        structures = [entity(1, "SUPPLYDEPOT"), entity(2, "SUPPLYDEPOT"),
                      entity(3, "SUPPLYDEPOT", x=30, health=30),
                      entity(4, "STARPORT", build_progress=0.7, has_techlab=True)]
        blocks = self.builder.entities.structure_blocks(structures, self.context, self.bot, own=True)
        self.assertTrue(any("SUPPLYDEPOT@main(10,10)" in block and "hp=[100%,100%,30%]" in block for block in blocks))
        self.assertTrue(any("building=70% addon=STARPORTTECHLAB:[" in block and "hp=[100%]" in block for block in blocks))

    def test_memory_has_counts_and_age_but_no_selectable_ids(self):
        units = [entity(1, "MARINE", age=8), entity(2, "MARINE", age=12)]
        blocks = self.builder.entities.remembered_enemy_unit_blocks(units, self.bot)
        self.assertEqual(len(blocks), 1)
        self.assertIn("MARINE*2", blocks[0])
        self.assertIn("last_seen=8-12s_ago", blocks[0])
        self.assertNotIn("[", blocks[0])

    def test_overview_renders_compact_game_totals(self):
        bot = NS(time_formatted="04:30", minerals=450, vespene=180,
                 supply_used=56, supply_cap=56, supply_workers=30, supply_army=24,
                 race=NS(name="Terran"), enemy_race=NS(name="Protoss"),
                 game_info=NS(map_size=NS(x=88, y=96)),
                 townhalls=[NS(is_ready=True, assigned_harvesters=16, ideal_harvesters=16)],
                 gas_buildings=[NS(is_ready=True, assigned_harvesters=3, ideal_harvesters=3)],
                 state=NS(score=NS(collection_rate_minerals=850, collection_rate_vespene=240,
                                    killed_value_units=650, killed_value_structures=0,
                                    lost_minerals_army=1050, lost_vespene_army=500)))
        overview = OverviewBuilder().build(bot)
        self.assertEqual(observation_text({
            "overview": overview, "situational_hints": {},
            "own_unit_blocks": [], "own_structure_blocks": [],
            "enemy_unit_blocks": [], "enemy_structure_blocks": [],
            "remembered_enemy_unit_blocks": [], "remembered_enemy_structure_blocks": [],
            "production_and_technology": [], "recent_changes": [], "action_history": [],
        }).split("\n\n# own_state", 1)[0],
            "# overview\n"
            "time=04:30, matchup=TerranvsProtoss, map_size=88x96\n"
            "minerals=450, vespene=180, income/min=850m/240g\n"
            "supply: used=56, cap=56, workers=30, army=24\n"
            "economy: bases=1, refineries=1, saturation=main 16/16 refineries 3/3\n"
            "combat: killed_value=650 units 0 structures, army_lost=1050m/500g")

    def test_enemy_shield_damage_and_rendered_sections(self):
        unit = entity(1, "STALKER", shield=20, shield_max=80)
        blocks = self.builder.entities.enemy_unit_blocks([unit], self.context, self.bot)
        self.assertIn("shield=[25%]", blocks[0])
        data = dict.fromkeys((
            "own_unit_blocks", "own_structure_blocks", "enemy_structure_blocks",
            "remembered_enemy_unit_blocks", "remembered_enemy_structure_blocks",
            "production_and_technology", "recent_changes", "action_history",
        ), [])
        data.update(overview={"match": "time=00:10, matchup=TerranvsProtoss",
                              "resources": "minerals=50, vespene=0",
                              "supply": "supply: used=8, cap=13, workers=8, army=0",
                              "economy": "economy: bases=1, refineries=0", "military": ""},
                    situational_hints={}, enemy_unit_blocks=blocks)
        text = observation_text(data)
        self.assertIn("# overview\ntime=00:10, matchup=TerranvsProtoss", text)
        self.assertIn(blocks[0], text)
        self.assertIn("\n\n# memory_enemy_state\n", text)
        self.assertIn("\n\n# own_state\n## units\n", text)
        self.assertNotRegex(text, r"</?\w+>")
        self.assertNotIn("\n\n\n", text)
