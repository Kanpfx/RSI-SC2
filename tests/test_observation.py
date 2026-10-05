"""Focused checks for compact observations without SC2 or model calls."""

import unittest
from types import SimpleNamespace as NS

from agent.game.observation.builder import ObservationBuilder
from agent.game.observation.technology import ProductionTechnologyBuilder
from agent.game.observation.renderer import OverviewBuilder, observation_text
from agent.game.observation.execution_context import TagIdMapper
from agent.game.actions.resolution.resolver import EntityContext


def entity(tag, name, x=10, health=100, **kwargs):
    return NS(tag=tag, type_id=NS(name=name), position=NS(x=x, y=10),
              health=health, health_max=100, **kwargs)


class CompactObservationTests(unittest.TestCase):
    def setUp(self):
        self.builder = ObservationBuilder(TagIdMapper())
        self.context = EntityContext()
        self.bot = NS(start_location=NS(x=10, y=10))

    def test_entities_have_individual_lines_positions_and_states(self):
        workers = [entity(1, "SCV"), entity(2, "SCV", x=30, health=80)]
        self.builder.entities._role_labels = lambda bot: {1: "scouting"}
        blocks = self.builder.entities.own_unit_blocks(workers, self.context, self.bot)
        self.assertEqual(blocks, ["SCV: 0@(10.0,10.0) scouting", "SCV: 1@(30.0,10.0) working hp=80%"])
        self.assertEqual(set(self.context.own_entities), {"0", "1"})

    def test_complex_entities_have_one_line_each_and_unknown_health(self):
        units = [entity(20, "BATTLECRUISER"), entity(10, "BATTLECRUISER", health=10)]
        units[0].health = None
        blocks = self.builder.entities.own_unit_blocks(units, self.context, self.bot)
        lines = blocks
        self.assertEqual(len(lines), 2)
        self.assertIn("0@(10.0,10.0) active hp=10%", lines[0])
        self.assertIn("1@(10.0,10.0) active hp=?", lines[1])
        self.assertTrue(all("EFFECT_TACTICALJUMP: [Unknown]" in line for line in lines))
        self.assertEqual([unit.tag for unit in self.context.own_entities.values()], [10, 20])

    def test_simple_combat_entities_preserve_distant_positions(self):
        units = [entity(1, "MARINE"), entity(2, "MARINE", x=100)]
        blocks = self.builder.entities.own_unit_blocks(units, self.context, self.bot)
        self.assertEqual(blocks, ["MARINE: 0@(10.0,10.0) active", "MARINE: 1@(100.0,10.0) active"])

    def test_structures_preserve_damage_progress_and_addons(self):
        structures = [entity(1, "SUPPLYDEPOT"), entity(2, "SUPPLYDEPOT", x=30, health=30),
                      entity(3, "STARPORT", build_progress=0.7, has_techlab=True)]
        own = self.builder.entities.structure_blocks(structures, self.context, self.bot, own=True)
        enemy = self.builder.entities.structure_blocks(structures, self.context, self.bot, own=False)
        self.assertEqual(own, enemy)
        self.assertIn("SUPPLYDEPOT: 1@(10.0,10.0) ready", own)
        self.assertIn("SUPPLYDEPOT: 2@(30.0,10.0) ready hp=30%", own)
        self.assertIn("STARPORT: 0@(10.0,10.0) building=70% addon=STARPORTTECHLAB", own)

    def test_energy_cargo_and_missing_position_are_per_entity(self):
        unit = entity(1, "MEDIVAC", energy=75, energy_max=200, cargo_used=4, cargo_max=8)
        unit.position = None
        blocks = self.builder.entities.own_unit_blocks([unit], self.context, self.bot)
        self.assertEqual(blocks, ["MEDIVAC: 0@[Unknown] active energy=75 cargo=4"])

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
            "production_and_technology": [], "action_history": [],
        }).split("\n\n# own_state", 1)[0],
            "# overview\n"
            "metadata: time=04:30, matchup=TerranvsProtoss, map_size=88x96\n"
            "resources: minerals=450, vespene=180, income=850m/240g\n"
            "supply: used=56, cap=56, workers=30, army=24")

    def test_enemy_shield_damage_and_rendered_sections(self):
        unit = entity(1, "STALKER", shield=20, shield_max=80)
        blocks = self.builder.entities.enemy_unit_blocks([unit], self.context, self.bot)
        self.assertIn("shield=25%", blocks[0])
        data = dict.fromkeys((
            "own_unit_blocks", "own_structure_blocks", "enemy_structure_blocks",
            "remembered_enemy_unit_blocks", "remembered_enemy_structure_blocks",
            "production_and_technology", "action_history",
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
        self.assertTrue(text.endswith("# action_history\n[None]"))
        self.assertNotIn("recent_history", text)
        self.assertNotIn("state_changes", text)
        data["action_history"] = "active: AMove(unit=0, target=enemy_main)"
        self.assertTrue(observation_text(data).endswith("# action_history\n" + data["action_history"]))


class ProductionTechnologyTests(unittest.TestCase):
    def setUp(self):
        from sc2.data import Attribute
        from sc2.ids.unit_typeid import UnitTypeId
        from sc2.ids.upgrade_id import UpgradeId
        self.stimpack = UpgradeId.STIMPACK
        self.bot = NS(
            game_data=NS(
                units={
                    1: NS(id=UnitTypeId.MARINE, creation_ability=NS(exact_id=101), attributes=[]),
                    2: NS(id=UnitTypeId.SCV, creation_ability=NS(exact_id=102), attributes=[]),
                    3: NS(id=UnitTypeId.ORBITALCOMMAND, creation_ability=NS(exact_id=103),
                          attributes=[Attribute.Structure.value]),
                    4: NS(id=UnitTypeId.SUPPLYDEPOT, creation_ability=None, attributes=[]),
                },
                upgrades={self.stimpack.value: NS(research_ability=NS(exact_id=201))},
            ),
            state=NS(upgrades={self.stimpack}),
        )
        self.builder = ProductionTechnologyBuilder()

    def test_native_mappings_include_workers_research_and_queues(self):
        def order(ability, progress):
            return NS(ability=NS(exact_id=ability), progress=progress)
        structures = [
            NS(build_progress=1.0, orders=[order(101, .609), order(101, .60), order(101, 0)]),
            NS(build_progress=1.0, orders=[order(102, .20), order(103, .40), order(999, .10)]),
            NS(build_progress=1.0, orders=[order(201, .35)]),
            NS(build_progress=.50, orders=[order(101, .80)]),
        ]
        self.assertEqual(self.builder.build(self.bot, structures), [
            "production: MARINE: progress=[0%, 60%?2]; SCV: progress=[20%]",
            "research: STIMPACK: progress=[35%]",
            "upgrades: STIMPACK",
        ])

    def test_empty_native_facts_have_no_technology_hint(self):
        self.bot.state.upgrades.clear()
        self.assertEqual(self.builder.build(self.bot, []), [
            "production: [None]", "research: [None]", "upgrades: [None]",
        ])
