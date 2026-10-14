"""Focused checks for compact observations without SC2 or model calls."""

import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock
from sc2.ids.ability_id import AbilityId

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

    def test_workers_cluster_by_state_and_preserve_selectable_ids(self):
        workers = [entity(1, "SCV", is_idle=True), entity(2, "SCV", x=30, health=80, is_idle=True),
                   entity(3, "SCV", is_gathering=True, order_target=99)]
        blocks = self.builder.entities.own_unit_blocks(workers, self.bot)
        self.assertEqual(blocks, ["SCV[2] | state=gathering_minerals", "SCV[0,1] | state=idle"])
        self.assertEqual(self.builder.entities.enemy_unit_blocks(workers, self.bot),
                         ['SCV[0,1,2] | state=visible'])

    def test_complex_entities_have_one_line_each_and_unknown_health(self):
        units = [entity(20, "BATTLECRUISER"), entity(10, "BATTLECRUISER", health=10)]
        units[0].health = None
        blocks = self.builder.entities.own_unit_blocks(units, self.bot)
        lines = blocks
        self.assertEqual(len(lines), 2)
        self.assertIn("BATTLECRUISER[0]@(10.0,10.0) | state=unknown | HP=10%", lines[0])
        self.assertIn("BATTLECRUISER[1]@(10.0,10.0) | state=unknown | HP=?", lines[1])
        self.assertTrue(all("EFFECT_TACTICALJUMP" not in line for line in lines))

    def test_simple_combat_entities_preserve_distant_positions(self):
        units = [entity(1, "MARINE"), entity(2, "MARINE", x=100)]
        blocks = self.builder.entities.own_unit_blocks(units, self.bot)
        self.assertEqual(blocks, ["MARINE[0]@(10.0,10.0) | state=unknown | HP=100%",
                                  "MARINE[1]@(100.0,10.0) | state=unknown | HP=100%"])

    def test_structures_preserve_damage_progress_and_addons(self):
        structures = [entity(1, "SUPPLYDEPOT"), entity(2, "SUPPLYDEPOT", x=30, health=30),
                      entity(3, "STARPORT", build_progress=0.7, has_techlab=True)]
        own = self.builder.entities.structure_blocks(structures, self.bot, own=True)
        enemy = self.builder.entities.structure_blocks(structures, self.bot, own=False)
        self.assertIn('SUPPLYDEPOT[1,2] | state=visible', enemy)
        self.assertIn('STARPORT[0]@(10.0,10.0) | state=building(70%) | HP=100%', enemy)
        self.assertTrue(all('addon=' not in line for line in enemy))
        self.assertIn("SUPPLYDEPOT[1,2] | state=unknown", own)
        self.assertIn("STARPORT[0]@(10.0,10.0) | state=building(70%),addon=TECHLAB | HP=100%", own)

    def test_energy_cargo_and_missing_position_are_per_entity(self):
        unit = entity(1, "MEDIVAC", energy=75, energy_max=200, cargo_used=4, cargo_max=8)
        unit.position = None
        blocks = self.builder.entities.own_unit_blocks([unit], self.bot)
        self.assertEqual(blocks, ["MEDIVAC[0]@[Unknown] | state=unknown | HP=100% | energy=75 | cargo=4"])

    def test_active_actions_apply_to_individuals_groups_and_structures(self):
        units = [entity(1, 'MARINE', is_idle=True), entity(2, 'MARINE', is_idle=True)]
        self.builder.entities.own_unit_blocks(units, self.bot)
        self.builder.sync_active_actions([
            {'id': 'StutterGroupBack', 'args': {'group': ['0', '1'], 'target': 'enemy_main'}},
            {'id': 'KeepUnitSafe', 'args': {'unit': '0'}},
            {'id': 'AddonSwap', 'args': {'structure_needing_addon': 'FACTORY', 'addon_required': 'REACTOR'}},
        ])
        own = self.builder.entities.own_unit_blocks(units, self.bot)
        self.assertIn('state=StutterGroupBack(target=enemy_main),KeepUnitSafe()', own[0])
        self.assertIn('state=StutterGroupBack(target=enemy_main)', own[1])
        enemy = self.builder.entities.enemy_unit_blocks(units, self.bot)
        self.assertTrue(all('state=visible' in line for line in enemy))
        factory = entity(3, 'FACTORY', is_idle=True)
        line = self.builder.entities.structure_blocks([factory], self.bot, own=True)[0]
        self.assertIn('state=AddonSwap(addon_required=REACTOR)', line)
        self.builder.sync_active_actions([])
        self.assertIn('state=idle', self.builder.entities.own_unit_blocks(units, self.bot)[0])

    def test_whitelisted_available_abilities_only_shown_for_own_entities(self):
        unit = entity(1, 'GHOST', is_idle=True,
                      abilities={AbilityId.EMP_EMP})
        own = self.builder.entities.own_unit_blocks([unit], self.bot)[0]
        self.assertTrue(own.endswith(' | EMP_EMP'))
        enemy = self.builder.entities.enemy_unit_blocks([unit], self.bot)[0]
        self.assertNotIn('EMP_EMP', enemy)

    def test_abilities_intersect_whitelist_and_current_availability(self):
        unit = entity(1, 'BATTLECRUISER', abilities={AbilityId.YAMATO_YAMATOGUN, AbilityId.MOVE_BATTLECRUISER})
        own = self.builder.entities.own_unit_blocks([unit], self.bot)[0]
        self.assertTrue(own.endswith(' | YAMATO_YAMATOGUN'))
        self.assertNotIn('EFFECT_TACTICALJUMP', own)
        self.assertNotIn('MOVE_BATTLECRUISER', own)
        self.assertEqual(unit.abilities, {AbilityId.YAMATO_YAMATOGUN, AbilityId.MOVE_BATTLECRUISER})
        starport = entity(2, 'STARPORT', abilities={AbilityId.BUILD_TECHLAB_STARPORT, AbilityId.SMART})
        self.assertTrue(self.builder.entities.structure_blocks([starport], self.bot, own=True)[0]
                        .endswith(' | BUILD_TECHLAB_STARPORT'))

    def test_empty_missing_whitelists_and_filtered_abilities_omit_field(self):
        units = [entity(1, 'MARINE', abilities={AbilityId.MOVE_MOVE}),
                 entity(2, 'AUTOTURRET', abilities={AbilityId.ATTACK_ATTACK}),
                 entity(3, 'ZEALOT', abilities={AbilityId.MOVE_MOVE})]
        blocks = self.builder.entities.own_unit_blocks(units, self.bot)
        self.assertTrue(all(line.endswith(' | HP=100%') for line in blocks))

    def test_compact_gas_and_building_orders_keep_distinct_states(self):
        self.bot.gas_buildings = [entity(99, 'REFINERY')]
        workers = [entity(1, 'SCV', is_gathering=True, order_target=99),
                   entity(2, 'SCV', is_returning=True, is_carrying_vespene=True)]
        self.assertEqual(self.builder.entities.own_unit_blocks(workers, self.bot),
                         ['SCV[0,1] | state=gathering_gas'])
        structures = [entity(3, 'BARRACKSTECHLAB', orders=[
            NS(ability=NS(id=AbilityId.BARRACKSTECHLABRESEARCH_STIMPACK), progress=.5)]),
                      entity(4, 'BARRACKSTECHLAB', is_idle=True)]
        lines = self.builder.entities.structure_blocks(structures, self.bot, own=True)
        self.assertEqual(lines, ['BARRACKSTECHLAB[3] | state=idle',
                                'BARRACKSTECHLAB[2] | state=researching'])

    def test_production_queue_is_summarized_without_repeating_orders(self):
        orders = [NS(ability=NS(exact_id=AbilityId.COMMANDCENTERTRAIN_SCV), progress=p)
                  for p in (.05, 0, 0, 0)]
        orbital = entity(1, 'ORBITALCOMMAND', orders=orders)
        self.assertEqual(self.builder.entities._core_state(orbital, [], set()), 'producing')
        factory = entity(2, 'FACTORY', has_reactor=True, orders=[
            NS(ability=NS(exact_id=AbilityId.FACTORYTRAIN_HELLION), progress=p) for p in (.2, .2, 0)])
        self.assertEqual(self.builder.entities._core_state(factory, [], set()), 'producing,addon=REACTOR')

    def test_special_orders_show_only_current_order(self):
        orders = [NS(ability=NS(exact_id=a), progress=0) for a in
                  (AbilityId.MORPH_HELLBAT, AbilityId.MOVE_MOVE)]
        unit = entity(1, 'HELLION', orders=orders)
        self.assertEqual(self.builder.entities._core_state(unit, [], set()), 'MORPH_HELLBAT')

    def test_enemy_ignores_orders_cargo_and_only_renders_observable_states(self):
        from sc2.ids.buff_id import BuffId
        unit = entity(1, 'MEDIVAC', is_idle=True, is_attacking=True,
                      is_gathering=True, has_reactor=True, is_flying=True,
                      cargo_used=4, cargo_max=8, energy=75, energy_max=200,
                      orders=[NS(ability=NS(id=NS(name='MOVE')), progress=0)],
                      buffs={BuffId.STIMPACK})
        line = self.builder.entities.enemy_unit_blocks([unit], self.bot)[0]
        self.assertEqual(line, 'MEDIVAC[0]@(10.0,10.0) | state=flying,buff=STIMPACK'
                              ' | HP=100% | energy=75')
        burrowed = entity(2, 'WIDOWMINEBURROWED', is_burrowed=True, is_cloaked=True)
        line = self.builder.entities.enemy_unit_blocks([burrowed], self.bot)[0]
        self.assertIn('state=burrowed,cloaked', line)

    def test_memory_uses_individual_format_and_compact_age_ranges(self):
        units = [entity(1, "MARINE", age=8), entity(2, "MARINE", age=12)]
        blocks = self.builder.entities.remembered_enemy_unit_blocks(units, self.bot)
        self.assertEqual(blocks, ['MARINE[0]@(10.0,10.0) | state=8s ago | HP=100%',
                                  'MARINE[1]@(10.0,10.0) | state=12s ago | HP=100%'])
        workers = [entity(3, 'SCV', age=8), entity(4, 'SCV', age=12)]
        self.assertEqual(self.builder.entities.remembered_enemy_unit_blocks(workers, self.bot),
                         ['SCV[2,3] | state=8-12s ago'])
        self.assertEqual(self.context.enemy_entities, {})

    def test_structure_memory_preserves_true_visible_time_and_snapshot(self):
        self.bot.time = 10
        structure = entity(1, 'BARRACKS', health=78)
        self.bot.enemy_structures = [structure]
        self.builder.entities.update_structure_memory(self.bot)
        self.bot.time = 22
        snapshot = entity(1, 'BARRACKS', health=0, age=0)
        unseen = entity(2, 'STARPORT', health=0, age=0)
        blocks = self.builder.entities.remembered_enemy_structure_blocks([snapshot, unseen], self.bot)
        self.assertEqual(blocks, ['BARRACKS[0]@(10.0,10.0) | state=12s ago | HP=78%',
                                  'STARPORT[1]@(10.0,10.0) | state=unknown | HP=?'])
        self.assertNotIn('1', self.context.enemy_entities)
        self.bot.time = 25
        refreshed = entity(1, 'BARRACKS', health=90)
        self.bot.enemy_structures = [refreshed]
        self.builder.entities.update_structure_memory(self.bot)
        self.bot.time = 27
        self.assertIn('state=2s ago | HP=90%',
                      self.builder.entities.remembered_enemy_structure_blocks([snapshot], self.bot)[0])

    def test_build_uses_last_visible_frame_between_model_requests(self):
        structure = entity(10, "BARRACKS", health=78, is_visible=True)
        self.bot = NS(
            time=10, time_formatted="00:10", units=[entity(1, "SCV", is_idle=True)],
            structures=[], enemy_units=[], enemy_structures=[structure],
            game_data=NS(units={}, upgrades={}), state=NS(upgrades=set()),
            already_pending=Mock(side_effect=AssertionError("unused pending statistics")),
        )
        initial = self.builder.build(self.bot, 10)
        self.assertEqual(len(initial.context.own_entities), 1)
        self.assertEqual(len(initial.context.enemy_entities), 1)
        self.assertIn("SCV[0] | state=idle", initial.text)

        self.bot.time = 19
        self.bot.enemy_structures = [entity(10, "BARRACKS", health=90, is_visible=True)]
        self.builder.execution_context(self.bot)
        self.bot.time = 22
        self.bot.enemy_structures = [entity(10, "BARRACKS", health=0, is_visible=False)]
        observation = self.builder.build(self.bot, 22)
        self.assertIn("BARRACKS[1]@(10.0,10.0) | state=3s ago | HP=90%", observation.text)
        self.assertEqual(observation.context.enemy_entities, {})
        self.bot.already_pending.assert_not_called()

        self.bot.enemy_structures = []
        self.builder.execution_context(self.bot)
        self.assertEqual(self.builder.entities._structure_memory, {})

    def test_short_visible_sighting_is_saved_without_rendering(self):
        self.bot = NS(time=19, units=[], structures=[], enemy_units=[],
                      enemy_structures=[entity(10, "STARPORT", health=90, is_visible=True)])
        self.builder.execution_context(self.bot)
        self.bot.time = 22
        self.bot.enemy_structures = [entity(10, "STARPORT", health=0, is_visible=False)]
        self.builder.execution_context(self.bot)
        blocks = self.builder.entities.remembered_enemy_structure_blocks(
            self.bot.enemy_structures, self.bot
        )
        self.assertEqual(blocks, ["STARPORT[0]@(10.0,10.0) | state=3s ago | HP=90%"])

    def test_native_unit_states_take_priority_over_raw_orders(self):
        from sc2.constants import IS_ATTACKING, IS_CONSTRUCTING_SCV, IS_REPAIRING
        from sc2.unit import Unit

        bot = NS(state=NS(game_loop=224), techlab_tags=set(), reactor_tags=set())
        for state, ability in (
            ("moving", AbilityId.MOVE), ("attacking", next(iter(IS_ATTACKING))),
            ("constructing", next(iter(IS_CONSTRUCTING_SCV))),
            ("repairing", next(iter(IS_REPAIRING))),
        ):
            with self.subTest(state=state):
                unit = Unit(NS(tag=1, orders=[object()], build_progress=1,
                               is_flying=False, add_on_tag=0), bot)
                unit.orders = [NS(ability=NS(id=ability, exact_id=ability), progress=0)]
                self.assertEqual(self.builder.entities._core_state(unit, [], set()), state)
                self.assertEqual(self.builder.entities._core_state(unit, ["KeepUnitSafe()"], set()),
                                 "KeepUnitSafe()")

    def test_execution_context_reads_enemy_natural_once(self):
        reads = []

        class Mediator:
            @property
            def get_enemy_nat(self):
                reads.append(True)
                return NS(x=70, y=60)

        bot = NS(time=0, units=[], structures=[], enemy_units=[], enemy_structures=[],
                 mediator=Mediator())
        context = self.builder.execution_context(bot)
        self.assertEqual(len(reads), 1)
        self.assertEqual(context.positions["enemy_natural"].x, 70)

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
        }).split("\n\n# Own Units", 1)[0],
            "# Overview\n"
            "metadata: time=04:30, matchup=TerranvsProtoss, map_size=88x96\n"
            "resources: minerals=450, vespene=180, income=850m/240g\n"
            "supply: used=56, cap=56, workers=30, army=24")

    def test_enemy_shield_damage_and_rendered_sections(self):
        unit = entity(1, "STALKER", shield=20, shield_max=80)
        blocks = self.builder.entities.enemy_unit_blocks([unit], self.bot)
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
        self.assertIn("# Overview\ntime=00:10, matchup=TerranvsProtoss", text)
        self.assertIn(blocks[0], text)
        self.assertIn("\n\n# Remembered Enemy Units\n", text)
        self.assertIn("\n\n# Own Units\n", text)
        self.assertEqual([line for line in text.splitlines() if line.startswith("#")], [
            "# Overview", "# Own Units", "# Own Structures", "# Visible Enemy Units",
            "# Visible Enemy Structures", "# Remembered Enemy Units",
            "# Remembered Enemy Structures", "# Production & Upgrades", "# Action History",
        ])
        self.assertNotRegex(text, r"</?\w+>")
        self.assertNotIn("\n\n\n", text)
        self.assertTrue(text.endswith("# Action History\n[None]"))
        self.assertNotIn("recent_history", text)
        self.assertNotIn("state_changes", text)
        data["action_history"] = "active: AMove(unit=0, target=enemy_main)"
        self.assertTrue(observation_text(data).endswith("# Action History\n" + data["action_history"]))


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
            "producting:\n- MARINE=[0%, 60%, 60%]\n- SCV=[20%]",
            "researching:\n- STIMPACK=[35%]",
            "upgraded:\nSTIMPACK",
        ])

    def test_static_ability_mappings_are_loaded_once_but_orders_stay_current(self):
        units = Mock(wraps=self.bot.game_data.units)
        upgrades = Mock(wraps=self.bot.game_data.upgrades)
        self.bot.game_data.units = units
        self.bot.game_data.upgrades = upgrades
        self.builder.build(self.bot, [])
        structures = [NS(build_progress=1, orders=[NS(ability=NS(exact_id=101), progress=.5)])]
        self.assertEqual(self.builder.build(self.bot, structures)[0],
                         "producting:\n- MARINE=[50%]")
        units.values.assert_called_once()
        upgrades.items.assert_called_once()

    def test_empty_native_facts_have_no_technology_hint(self):
        self.bot.state.upgrades.clear()
        self.assertEqual(self.builder.build(self.bot, []), [
            "producting:\n[None]", "researching:\n[None]", "upgraded:\n[None]",
        ])
