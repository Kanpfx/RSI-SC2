"""BC baseline: explicit global goals, concurrent tasks and disjoint army groups."""
from sc2.ids.unit_typeid import UnitTypeId as U

from ..tasks.economy import EconomyTask
from ..tasks.tech import TechTask
from ..tasks.army import ArmyTask
from ..tasks.defense import DefenseTask
from ..tasks.harass import HarassTask
from ..tasks.scouting import ScoutingTask


class BcRush:
    def __init__(self):
        """Create persistent Tasks and the minimal phase, scout and enemy-search state."""
        self.economy, self.tech, self.army = EconomyTask(), TechTask(), ArmyTask()
        self.defense, self.harass, self.scouting = DefenseTask(), HarassTask(), ScoutingTask()
        self.phase = "opening"
        self.scout_tag = None
        self.search_index = 0
        self.known_structures = {}

    async def execute(self, frame):
        """Assign strategic unit groups, set BC goals and run concurrent Tasks.

        Allocate the scout before Tasks select builders, keep defense and harassment
        groups disjoint, and maintain enemy structure locations for search. Phases
        inform goals without disabling economy or defense. Return combined Task
        control; never issue native commands or resolve resource competition here.
        """
        game, control, map_data = frame.game, frame.units, frame.spatial.map
        marines = tuple(u.tag for u in game.units if u.type_id == U.MARINE)
        bcs = tuple(u.tag for u in game.units if u.type_id == U.BATTLECRUISER)
        self.phase = "pressure" if bcs else "tech" if any(
            b.type_id == U.FACTORY for b in game.structures) else "opening"
        for worker in game.workers:
            if worker.tag not in control.owners:
                control.assign((worker.tag,), "economy", "miner")
        control.assign((b.tag for b in game.townhalls), "economy", "producer")
        control.assign(marines, "defense", "army")
        control.assign(bcs, "harass", "army")
        control.assign((b.tag for b in game.structures if b.type_id in
                        {U.BARRACKS, U.FACTORY, U.STARPORT, U.FUSIONCORE}), "army", "producer")
        # Allocate the scout before any task selects a construction worker.
        if self.scout_tag is None and not self.scouting.finished and game.state.game_loop >= 1344:
            workers = control.workers(game)
            scout = min((w for w in workers if not w.is_carrying_resource), key=lambda w: w.tag, default=None)
            if scout:
                self.scout_tag = scout.tag
                control.assign((scout.tag,), "scouting", "scout")
        if self.scout_tag is not None:
            if game.workers.find_by_tag(self.scout_tag) is None:
                self.scouting.finished = True
            if self.scouting.finished:
                if game.workers.find_by_tag(self.scout_tag):
                    control.assign((self.scout_tag,), "economy", "miner")
                self.scout_tag = None
        visible_tags = {b.tag for b in game.enemy_structures if b.is_visible}
        for tag, position in list(self.known_structures.items()):
            if game.is_visible(position) and tag not in visible_tags:
                del self.known_structures[tag]
        self.known_structures.update({b.tag: b.position for b in game.enemy_structures if b.is_visible})
        route = map_data.enemy_starts + tuple(sorted(map_data.expansions,
            key=lambda p: (p.distance_to(map_data.enemy_starts[0] if map_data.enemy_starts else map_data.center), p)))
        route = route or (map_data.center,)
        search = route[self.search_index % len(route)]
        if not self.known_structures and any(u.type_id == U.BATTLECRUISER and u.distance_to(search) < 5
                                             for u in game.units):
            self.search_index += 1
            search = route[self.search_index % len(route)]
        target = min(self.known_structures.values(), key=lambda p: p.distance_to(map_data.main), default=search)
        threats = [e for e in game.enemy_units if e.is_visible and e.distance_to(map_data.main) < 30]
        defense = min(threats, key=lambda e: e.distance_to(map_data.main), default=None)
        defense_target = (defense.position if defense else target if game.state.game_loop >= 8064
                          else map_data.main.towards(map_data.ramp, max(0, map_data.main.distance_to(map_data.ramp)-4)))
        saving_bc = any(b.type_id == U.FUSIONCORE and b.is_ready for b in game.structures)
        handled = await frame.run(self.economy, priority=50, worker_target=28, gas_target=2,
            base_target=2 if bcs and game.state.game_loop > 9408 else 1, saving_bc=saving_bc)
        handled |= await frame.run(self.tech, priority=80)
        barracks = tuple(b.tag for b in game.structures if b.type_id == U.BARRACKS)
        starports = tuple(b.tag for b in game.structures if b.type_id == U.STARPORT
                         and (addon := game.structures.find_by_tag(b.add_on_tag)) is not None and addon.is_ready)
        handled |= await frame.run(self.army, goals=[
            (U.MARINE, barracks, 8, 50, False), (U.BATTLECRUISER, starports, 8, 85, True)])
        handled |= await frame.run(self.defense, priority=40, tags=marines, target=defense_target)
        handled |= await frame.run(self.harass, priority=40, tags=bcs, target=target, retreat_target=map_data.main)
        handled |= await frame.run(self.scouting, priority=35,
            enabled=self.scout_tag is not None, skip_reason="no_active_scout",
            tags=(self.scout_tag,) if self.scout_tag is not None else (),
            target=map_data.enemy_starts[0] if map_data.enemy_starts else map_data.center,
            return_target=map_data.main)
        return handled
