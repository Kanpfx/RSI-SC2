"""Offline checks for action arbitration, construction, telemetry and spatial services."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

import numpy as np
from sc2.ids.ability_id import AbilityId as A
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2

from bot.core.actions import Actions
from bot.core.frame import Frame
from bot.core.units import UnitControl
from bot.spatial import Spatial
from bot.telemetry.logger import Logger
from bot.components.behaviors.combat.attack_move import AttackMove
from bot.components.behaviors.economy.mining import Mining
from bot.components.behaviors.production.build_structure import BuildStructure
from bot.components.behaviors.production.train_units import TrainUnits
from bot.components.behaviors.production.research import Research
from bot.components.tactics.bc_rush import BcRush


PRODUCTS = {
    U.SCV: (A.COMMANDCENTERTRAIN_SCV, 50, 0, 1),
    U.MARINE: (A.BARRACKSTRAIN_MARINE, 50, 0, 1),
    U.BATTLECRUISER: (A.STARPORTTRAIN_BATTLECRUISER, 400, 300, 6),
    U.SUPPLYDEPOT: (A.TERRANBUILD_SUPPLYDEPOT, 100, 0, 0),
    U.BARRACKS: (A.TERRANBUILD_BARRACKS, 150, 0, 0),
    U.FACTORY: (A.TERRANBUILD_FACTORY, 150, 100, 0),
    U.STARPORT: (A.TERRANBUILD_STARPORT, 150, 100, 0),
    U.FUSIONCORE: (A.TERRANBUILD_FUSIONCORE, 150, 150, 0),
    U.REFINERY: (A.TERRANBUILD_REFINERY, 75, 0, 0),
    U.COMMANDCENTER: (A.TERRANBUILD_COMMANDCENTER, 400, 0, 0),
    U.STARPORTTECHLAB: (A.BUILD_TECHLAB_STARPORT, 50, 25, 0),
}


class Units(list):
    def find_by_tag(self, tag):
        """Mimic native tag lookup and return None when the test unit is absent."""
        return next((u for u in self if u.tag == tag), None)

    def __add__(self, other):
        """Preserve the test collection's lookup API when joining unit groups."""
        return Units(list(self) + list(other))


class Unit(NS):
    """Native-shaped observations; UnitCommand validates the class name."""
    def __init__(self, tag, product=U.SCV, position=(10, 10), **fields):
        """Build a native-shaped unit observation with explicit per-test field overrides."""
        super().__init__(tag=tag, type_id=product, position=Point2(position), orders=[],
            health=45, health_max=45, shield=0, radius=.5, build_progress=1,
            is_structure=product not in {U.SCV, U.MARINE, U.BATTLECRUISER},
            is_constructing_scv=False, is_carrying_resource=False, is_flying=False,
            is_visible=True, mineral_contents=0, vespene_contents=0,
            add_on_tag=0, weapon_cooldown=0, can_attack_ground=False, can_attack_air=False,
            ground_range=0, air_range=0, ground_dps=0, air_dps=0, assigned_harvesters=0)
        self.__dict__.update(fields)

    @property
    def is_idle(self):
        """Derive idle status from the test unit's current order list."""
        return not self.orders

    @property
    def is_ready(self):
        """Derive completion from the test unit's build progress."""
        return self.build_progress >= 1

    @property
    def add_on_position(self):
        """Mimic the fixed Terran addon offset from its parent structure."""
        return self.position + Point2((2.5, -.5))

    def distance_to(self, target):
        """Measure point distance using the native Point2 implementation."""
        return self.position.distance_to(target.position if hasattr(target, "position") else Point2(target))


def make_frame(*own, minerals=150, gas=300, supply=12):
    """Build offline game/services with real enums and commands but mocked engine queries."""
    game = NS(state=NS(game_loop=0, action_errors=[]), minerals=minerals, vespene=gas,
              supply_left=supply, supply_used=15-supply, supply_cap=15)
    game.units = Units(u for u in own if not u.is_structure)
    game.structures = Units(u for u in own if u.is_structure)
    game.workers = Units(u for u in own if u.type_id == U.SCV)
    game.townhalls = Units(u for u in own if u.type_id == U.COMMANDCENTER)
    game.gas_buildings = Units(u for u in own if u.type_id == U.REFINERY)
    game.enemy_units, game.enemy_structures = Units(), Units()
    game.mineral_field, game.vespene_geyser = Units(), Units()
    game.all_units = game.units + game.structures
    game.start_location, game.enemy_start_locations = Point2((10, 10)), [Point2((50, 50))]
    game.expansion_locations_list = [Point2((10, 10)), Point2((35, 15))]
    game.main_base_ramp = NS(top_center=Point2((15, 15)))
    grid = NS(data_numpy=np.ones((64, 64), dtype=np.uint8))
    game.game_info = NS(map_center=Point2((32, 32)), terrain_height=grid,
        pathing_grid=grid, placement_grid=grid, map_ramps=[],
        playable_area=NS(x=0, y=0, width=64, height=64))
    abilities = {a.value: NS(id=a) for a in A}
    data = {product.value: NS(creation_ability=NS(id=cost[0]), footprint_radius=1 if product == U.STARPORTTECHLAB else 1.5)
            for product, cost in PRODUCTS.items()}
    game.game_data = NS(abilities=abilities, units=data, upgrades={})
    game.client = NS(query_available_abilities=AsyncMock(side_effect=lambda units, **kwargs:
                     [[a for a in A] for _ in units]))
    game.tech_requirement_progress = lambda product: 1
    game.already_pending = lambda product: sum(not u.is_ready and u.type_id == product for u in own)
    game.already_pending_upgrade = lambda upgrade: 0
    game.calculate_cost = lambda product: NS(minerals=PRODUCTS[product][1] if product in PRODUCTS else 0,
                                            vespene=PRODUCTS[product][2] if product in PRODUCTS else 0)
    game.calculate_supply_cost = lambda product: PRODUCTS[product][3]
    game.can_place = AsyncMock(side_effect=lambda product, positions: [True]*len(positions))
    game.can_place_single = AsyncMock(return_value=True)
    game.is_visible = lambda point: False
    game.commands = []
    game.do = lambda command: game.commands.append(command) or True
    units, spatial = UnitControl(), Spatial(game)
    units.assign((w.tag for w in game.workers), "economy", "miner")
    return Frame(game, spatial, units, Actions(game, units, spatial.placement))


class Request:
    def __init__(self, tag, ability=A.MOVE, **fields):
        """Retain an explicit request payload for execution-system tests."""
        self.tag, self.ability, self.fields = tag, ability, fields

    async def execute(self, frame):
        """Propose the stored request inside the test's tracked module scope."""
        frame.actions.propose(self.tag, self.ability, **self.fields)
        return True


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_inherited_priority_skip_and_exception_are_distinct(self):
        """Verify inherited call metadata and separate disabled, invoked and exceptional outcomes."""
        frame = make_frame(Unit(1))
        child = Request(1, target=Point2((20, 20)))

        class Parent:
            async def execute(self, frame):
                """Invoke a child without an override to exercise priority and parent-call inheritance."""
                return await frame.run(child)

        await frame.run(Parent(), priority=71)
        self.assertEqual(frame.actions.requests[0].priority, 71)
        self.assertEqual(frame.records[0]["parent_id"], frame.records[1]["call_id"])
        await frame.run(child, enabled=False, skip_reason="phase_inactive")
        self.assertEqual(frame.records[-1]["status"], "skipped")
        with self.assertRaises(ValueError):
            await frame.run(child, enabled=False)

        class Broken:
            async def execute(self, frame):
                """Raise deliberately so the test can inspect exception recording and scope restoration."""
                raise RuntimeError("test failure")

        with self.assertRaises(RuntimeError):
            await frame.run(Broken())
        self.assertEqual(frame.records[-1]["status"], "exception")

    async def test_resource_saving_blocks_workers_but_allows_free_moves(self):
        """Verify BC savings defer worker spending while zero-cost movement remains eligible."""
        frame = make_frame(Unit(1, U.STARPORT), Unit(2, U.COMMANDCENTER), Unit(3))
        await frame.run(TrainUnits(), product=U.BATTLECRUISER, producers=(1,), target=1,
                        priority=85, save_if_unaffordable=True)
        await frame.run(TrainUnits(), product=U.SCV, producers=(2,), target=28, priority=30)
        await frame.run(Request(3, target=Point2((20, 20))), priority=10)
        await frame.actions.resolve()
        self.assertEqual([r.status for r in frame.actions.requests], ["deferred", "deferred", "submitted"])
        self.assertEqual(frame.actions.requests[1].reason, "reserved_by_higher_priority")
        self.assertEqual(frame.game.minerals, 150)

    async def test_saving_expires_and_supply_does_not_lock_money(self):
        """Verify saving timeout and supply shortage both allow unrelated affordable production."""
        frame = make_frame(Unit(1, U.STARPORT), Unit(2, U.COMMANDCENTER))
        for loop in (0, 225):
            frame.game.state.game_loop = loop
            frame.actions.begin()
            await frame.run(TrainUnits(), product=U.BATTLECRUISER, producers=(1,), target=1,
                            priority=85, save_if_unaffordable=True)
            await frame.run(TrainUnits(), product=U.SCV, producers=(2,), target=28, priority=30)
            await frame.actions.resolve()
        self.assertEqual(frame.actions.requests[0].reason, "saving_timeout")
        self.assertEqual(frame.actions.requests[1].status, "submitted")
        frame = make_frame(Unit(1, U.STARPORT), Unit(2, U.COMMANDCENTER), supply=1)
        await frame.run(TrainUnits(), product=U.BATTLECRUISER, producers=(1,), target=1,
                        priority=85, save_if_unaffordable=True)
        await frame.run(TrainUnits(), product=U.SCV, producers=(2,), target=28, priority=30)
        await frame.actions.resolve()
        self.assertEqual(frame.actions.requests[0].reason, "insufficient_supply")
        self.assertEqual(frame.actions.requests[1].status, "submitted")

    async def test_nonresource_prerequisite_failure_cannot_reserve(self):
        """Verify unavailable production abilities cannot hide budget from valid demands."""
        frame = make_frame(Unit(1, U.STARPORT), Unit(2, U.COMMANDCENTER))
        frame.game.client.query_available_abilities = AsyncMock(return_value=[[A.COMMANDCENTERTRAIN_SCV]])
        await frame.run(TrainUnits(), product=U.BATTLECRUISER, producers=(1,), target=1,
                        priority=85, save_if_unaffordable=True)
        await frame.run(TrainUnits(), product=U.SCV, producers=(2,), target=28)
        await frame.actions.resolve()
        self.assertEqual(len(frame.actions.requests), 1)
        self.assertEqual(frame.actions.requests[0].status, "submitted")

    async def test_construction_occupancy_completion_and_spatial_conflict(self):
        """Verify accepted construction claims space/workers and completion releases both."""
        frame = make_frame(Unit(1), Unit(2), minerals=400)
        build = BuildStructure()
        await frame.run(build, product=U.BARRACKS, near=Point2((25, 25)), priority=80)
        first = frame.actions.requests[0]
        await frame.run(Request(2, A.TERRANBUILD_BARRACKS, target=first.target, product=U.BARRACKS,
                                construction=True, footprint=first.footprint), priority=70)
        await frame.run(Request(1, A.HARVEST_GATHER, target=Point2((5, 5))), priority=20)
        await frame.actions.resolve()
        self.assertEqual(first.status, "submitted")
        self.assertEqual(frame.actions.requests[1].reason, "placement_conflict")
        self.assertEqual(frame.actions.requests[2].reason, "unit_occupied")
        self.assertNotIn(1, [w.tag for w in frame.units.workers(frame.game)])
        building = Unit(10, U.BARRACKS, first.target)
        frame.game.structures.append(building)
        frame.game.state.game_loop = 10
        frame.actions.begin()
        self.assertEqual(first.status, "completed")
        self.assertFalse(frame.units.occupied)
        self.assertFalse(frame.spatial.placement.reserved)

    async def test_construction_failure_releases_worker_and_resume_costs_zero(self):
        """Verify engine rejection releases claims and resuming an unfinished structure is free."""
        frame = make_frame(Unit(1), minerals=150, gas=0)
        await frame.run(BuildStructure(), product=U.BARRACKS, near=Point2((25, 25)))
        await frame.actions.resolve()
        request = frame.actions.requests[0]
        frame.game.state.action_errors = [NS(unit_tag=1, ability_id=request.ability.value, result=9)]
        frame.actions.begin()
        self.assertEqual(request.status, "failed")
        self.assertFalse(frame.units.occupied)
        self.assertFalse(frame.spatial.placement.reserved)
        frame.game.state.action_errors = []
        frame.game.minerals = 0
        building = Unit(10, U.BARRACKS, (25, 25), build_progress=.5)
        frame.game.structures.append(building)
        frame.game.all_units.append(building)
        await frame.run(BuildStructure(), product=U.BARRACKS, near=Point2((25, 25)))
        await frame.actions.resolve()
        self.assertEqual(frame.actions.requests[-1].ability, A.SMART)
        self.assertEqual(frame.actions.requests[-1].status, "submitted")

    async def test_dead_builder_and_timeout_release_reservations(self):
        """Verify builder death and unobserved-construction timeout both release claims."""
        for died in (True, False):
            with self.subTest(died=died):
                frame = make_frame(Unit(1), minerals=150)
                await frame.run(BuildStructure(), product=U.BARRACKS, near=Point2((25, 25)))
                await frame.actions.resolve()
                request = frame.actions.requests[0]
                if died:
                    frame.game.units.clear()
                else:
                    frame.game.state.game_loop = 225
                frame.actions.begin()
                self.assertEqual(request.status, "failed")
                self.assertFalse(frame.units.occupied)
                self.assertFalse(frame.spatial.placement.reserved)

    async def test_duplicate_and_missing_units_are_explicit(self):
        """Verify duplicate orders and dead/missing actors receive explicit rejection reasons."""
        unit = Unit(1)
        unit.orders = [NS(ability=NS(id=A.MOVE), target=Point2((20, 20)))]
        frame = make_frame(unit)
        await frame.run(Request(1, target=Point2((20, 20))))
        await frame.run(Request(999, target=Point2((20, 20))))
        await frame.actions.resolve()
        self.assertEqual([r.reason for r in frame.actions.requests], ["duplicate_order", "actor_missing"])
        self.assertFalse(await frame.run(AttackMove(), tags=(), target=Point2((20, 20))))

    async def test_queue_order_and_no_duplicate_budget_spend(self):
        """Verify a queued sequence stays ordered and excludes competing control of its actor."""
        frame = make_frame(Unit(1), minerals=100)

        class Sequence:
            async def execute(self, frame):
                """Propose a queue head and tail to exercise stable native command ordering."""
                frame.actions.propose(1, A.MOVE, Point2((20, 20)))
                frame.actions.propose(1, A.MOVE, Point2((25, 25)), queue=True)
                return True

        await frame.run(Sequence(), priority=50)
        await frame.run(Request(1, target=Point2((30, 30))), priority=40)
        await frame.actions.resolve()
        self.assertEqual([r.status for r in frame.actions.requests], ["submitted", "submitted", "rejected"])
        self.assertEqual([c.target for c in frame.game.commands], [Point2((20, 20)), Point2((25, 25))])

    async def test_observed_and_unknown_require_order_evidence(self):
        """Verify native orders confirm a request and missing evidence eventually yields unknown."""
        unit = Unit(1)
        frame = make_frame(unit)
        await frame.run(Request(1, target=Point2((20, 20))))
        await frame.actions.resolve()
        request = frame.actions.requests[0]
        unit.orders = [NS(ability=NS(id=A.MOVE), target=Point2((20, 20)))]
        frame.actions.begin()
        self.assertEqual(request.status, "observed")
        unit.orders = []
        await frame.run(Request(1, target=Point2((30, 30))))
        await frame.actions.resolve()
        request = frame.actions.requests[0]
        frame.game.state.game_loop = 33
        frame.actions.begin()
        self.assertEqual(request.status, "unknown")

    async def test_logger_preserves_call_request_links_and_disabled_behavior(self):
        """Verify request/call links survive persistence and disabling logging preserves commands."""
        commands = []
        for enabled in (True, False):
            frame = make_frame(Unit(1))
            with TemporaryDirectory() as directory:
                logger = Logger(directory if enabled else None)
                await frame.run(Request(1, target=Point2((20, 20))))
                await frame.actions.resolve()
                logger.consume(frame)
                logger.close()
                if enabled:
                    rows = [json.loads(line) for line in (Path(directory)/"trace.jsonl").read_text().splitlines()]
                    call = next(row for row in rows if row["event"] == "module_call")
                    self.assertTrue(all(row["call_id"] == call["call_id"] for row in rows if row["event"] == "action"))
            commands.append([(c.ability, c.target) for c in frame.game.commands])
        self.assertEqual(commands[0], commands[1])

    async def test_full_tactics_graph_and_mining_respect_existing_occupancy(self):
        """Verify the three-layer graph executes and mining excludes an accepted builder."""
        worker, base = Unit(1), Unit(2, U.COMMANDCENTER)
        frame = make_frame(worker, base)
        mineral = Unit(20, U.MINERALFIELD, (8, 8), mineral_contents=100)
        frame.game.mineral_field.append(mineral)
        frame.game.all_units.append(mineral)
        await frame.run(BcRush())
        await frame.actions.resolve()
        self.assertTrue(frame.game.commands)
        self.assertTrue(any(row["module_id"] == "TechTask" for row in frame.records))
        frame.actions.begin()
        self.assertIn(1, frame.units.occupied)
        self.assertFalse(await frame.run(Mining()))
        self.assertFalse(frame.actions.requests)


class SpatialTests(unittest.TestCase):
    def test_ground_air_paths_threat_and_safe_point(self):
        """Verify ground barriers, independent air routing and threat-aware local safety."""
        frame = make_frame()
        pathing = frame.spatial.pathing
        frame.game.game_info.pathing_grid.data_numpy[:, 30] = 0
        self.assertFalse(pathing.path(Point2((10, 10)), Point2((40, 10)), max_nodes=5000))
        self.assertTrue(pathing.path(Point2((10, 10)), Point2((40, 10)), flying=True))
        enemy = Unit(90, U.MARINE, (20, 20), can_attack_ground=True, ground_range=5, ground_dps=10)
        frame.game.enemy_units.append(enemy)
        frame.game.state.game_loop = 16
        self.assertGreater(pathing.threat(enemy.position), 0)
        safe = pathing.safe_point(enemy.position)
        self.assertLess(pathing.threat(safe), pathing.threat(enemy.position))

    def test_placement_preserves_addon_space_and_queries_engine(self):
        """Verify addon footprint expansion, reservation release and native placement queries."""
        frame = make_frame()
        placement = frame.spatial.placement
        plain = placement.footprint(U.BARRACKS, Point2((25, 25)))
        extended = placement.footprint(U.BARRACKS, Point2((25, 25)), True)
        self.assertEqual(extended[2]-plain[2], 2)
        placement.reserve("build", extended)
        self.assertTrue(placement.conflicts(plain))
        placement.release("build")
        self.assertFalse(placement.conflicts(plain))
        position = asyncio.run(placement.find(U.BARRACKS, Point2((25, 25)), addon_space=True))
        self.assertIsNotNone(position)
        frame.game.can_place.assert_awaited()
        frame.game.can_place_single.assert_awaited()


if __name__ == "__main__":
    unittest.main()
