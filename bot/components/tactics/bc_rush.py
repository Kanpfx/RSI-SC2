"""BC rush strategy assembled directly from shared behaviors."""

from sc2.ids.ability_id import AbilityId as A
from sc2.ids.unit_typeid import UnitTypeId as U

from ..behaviors.economy.mining import mining
from ..behaviors.production.build_structure import build_structure
from ..behaviors.production.addon import addon
from ..behaviors.production.train_units import train_units
from ..behaviors.combat.attack_move import attack_move
from ..behaviors.combat.focus_fire import focus_fire
from ..behaviors.combat.kite import kite
from ..behaviors.combat.retreat import retreat
from ..behaviors.combat.tactical_jump import tactical_jump
from ..behaviors.movement.path_to_target import path_to_target
from ..behaviors.helpers import count


class BcRush:
    """Run a basic economy, tech and production plan leading to BC pressure.

    Keep BC-specific goals, priorities and scouting state here; shared behaviors
    propose actions and the core resolves their resource and unit conflicts.
    """

    def __init__(self):
        self.phase = "opening"
        self.scout_tag = None
        self.scout_finished = False
        self.known_structures = {}
        self.search_index = 0
        self.route = ()

    def request_units(self, frame):
        """Claim economy, production and combat groups; scout demand overrides economy."""
        game = frame.game
        economic = {u.tag for u in game.workers + game.townhalls + game.gas_buildings}
        frame.claim("economy", tags=economic, priority=10)
        frame.claim("production", tags=(b.tag for b in game.structures if b.tag not in economic), priority=20)
        frame.claim("defense", tags=(u.tag for u in game.units if u.type_id == U.MARINE), priority=70)
        frame.claim("harass", tags=(u.tag for u in game.units if u.type_id == U.BATTLECRUISER), priority=70)
        if self.scout_tag is not None and self.scout_tag not in frame.units.own:
            self.scout_finished = True
        if game.state.game_loop >= 1344 and not self.scout_finished:
            frame.claim("scout", unit_type=U.SCV, count=1, priority=90,
                        near=frame.spatial.map.main, exclude_carrying=True)

    async def execute(self, frame):
        """Update strategic targets and run each phase using the allocated unit groups."""
        game, map_data = frame.game, frame.spatial.map
        marines = frame.tags("defense")
        bcs = frame.tags("harass")
        self.phase = ("pressure" if bcs else "tech" if any(
            b.type_id == U.FACTORY for b in game.structures) else "opening")
        target = self._target(frame, bcs)
        threats = [e for e in game.enemy_units if e.is_visible and e.distance_to(map_data.main) < 30]
        closest = min(threats, key=lambda e: e.distance_to(map_data.main), default=None)
        defense_target = (closest.position if closest else target if game.state.game_loop >= 8064
            else map_data.main.towards(map_data.ramp, max(0, map_data.main.distance_to(map_data.ramp)-4)))
        await self._economy(frame, bcs)
        await self._technology(frame)
        await self._production(frame)
        await self._defense(frame, marines, defense_target)
        await self._harass(frame, bcs, target)
        await self._scout(frame)

    def _target(self, frame, bcs):
        """Remember enemy structures until disproved; search start and expansion sites."""
        game, map_data = frame.game, frame.spatial.map
        visible = {b.tag for b in game.enemy_structures if b.is_visible}
        for tag, position in list(self.known_structures.items()):
            if game.is_visible(position) and tag not in visible:
                del self.known_structures[tag]
        self.known_structures.update({b.tag: b.position for b in game.enemy_structures if b.is_visible})
        if not self.route:
            self.route = map_data.enemy_starts + tuple(sorted(map_data.expansions,
                key=lambda p: (p.distance_to(map_data.enemy_starts[0]
                    if map_data.enemy_starts else map_data.center), p)))
            self.route = self.route or (map_data.center,)
        search = self.route[self.search_index % len(self.route)]
        if not self.known_structures and any(frame.units.own[tag].distance_to(search) < 5 for tag in bcs):
            self.search_index += 1
            search = self.route[self.search_index % len(self.route)]
        return min(self.known_structures.values(), key=lambda p: p.distance_to(map_data.main), default=search)

    async def _economy(self, frame, bcs):
        """Maintain gathering, worker, supply and gas goals, then allow a second base."""
        game = frame.game
        # Once the Fusion Core is ready, worker production yields spending priority to BCs.
        saving_bc = any(b.type_id == U.FUSIONCORE and b.is_ready for b in game.structures)
        await frame.run(mining, key="mining", group="economy", priority=20)
        await frame.run(train_units, key="workers", group="economy", priority=30 if saving_bc else 60,
                        product=U.SCV, producers=tuple(b.tag for b in game.townhalls), target=28)
        margin = 8 if game.supply_cap >= 31 else 4
        unfinished = next((b for b in game.structures if b.type_id == U.SUPPLYDEPOT and not b.is_ready), None)
        if unfinished or (game.supply_cap < 200 and game.supply_left < margin
                          and not game.already_pending(U.SUPPLYDEPOT)):
            await frame.run(build_structure, key="supply", group="economy", priority=100,
                product=U.SUPPLYDEPOT, near=unfinished.position if unfinished else frame.spatial.map.anchor(-6))
        if count(frame, U.BARRACKS) > 0:
            unfinished_gas = next((b for b in game.gas_buildings if not b.is_ready), None)
            if unfinished_gas is not None:
                await frame.run(build_structure, key="gas", group="economy", priority=90,
                                product=U.REFINERY, near=unfinished_gas.position, exact=True)
            elif count(frame, U.REFINERY) < 2:
                geyser = min((g for g in game.vespene_geyser if g.vespene_contents > 0
                    and any(b.is_ready and b.distance_to(g) < 12 for b in game.townhalls)
                    and not any(b.distance_to(g) < 2 for b in game.gas_buildings)),
                    key=lambda g: (g.distance_to(frame.spatial.map.main), g.tag), default=None)
                if geyser is not None:
                    await frame.run(build_structure, key="gas", group="economy", priority=90,
                                    product=U.REFINERY, geyser_tag=geyser.tag)
        base_target = 2 if bcs and game.state.game_loop > 9408 else 1
        unfinished_base = next((b for b in game.townhalls if not b.is_ready), None)
        if unfinished_base is not None:
            site = unfinished_base.position
        else:
            sites = [p for p in frame.spatial.map.expansions
                     if all(b.distance_to(p) > 12 for b in game.townhalls)]
            site = min(sites, key=lambda p: p.distance_to(frame.spatial.map.main), default=None)
        if site is not None and (unfinished_base or count(frame, U.COMMANDCENTER) < base_target):
            await frame.run(build_structure, key="expansion", group="economy", priority=40,
                            product=U.COMMANDCENTER, near=site, exact=True)

    async def _technology(self, frame):
        """Advance the first missing tech step and request Starport Tech Labs separately."""
        game = frame.game
        for index, product in enumerate((U.BARRACKS, U.FACTORY, U.STARPORT, U.FUSIONCORE)):
            if any(b.type_id == product and b.is_ready for b in game.structures):
                continue
            await frame.run(build_structure, key=f"tech/{product.name}", group="economy", priority=80,
                product=product, near=frame.spatial.map.anchor(index*5),
                addon_space=product != U.FUSIONCORE, save_if_unaffordable=True)
            break
        await frame.run(addon, key="tech/starport_lab", group="production", priority=85,
            product=U.STARPORTTECHLAB, producers=tuple(b.tag for b in game.structures
                if b.type_id == U.STARPORT), save_if_unaffordable=True)

    async def _production(self, frame):
        """Fill Marine and BC targets, giving BC requests higher resource priority."""
        game = frame.game
        barracks = tuple(b.tag for b in game.structures if b.type_id == U.BARRACKS)
        starports = tuple(b.tag for b in game.structures if b.type_id == U.STARPORT
            and (addon := frame.units.own.get(b.add_on_tag)) is not None and addon.is_ready)
        await frame.run(train_units, key="marines", group="production", priority=50,
                        product=U.MARINE, producers=barracks, target=8)
        await frame.run(train_units, key="battlecruisers", group="production", priority=85,
                        product=U.BATTLECRUISER, producers=starports, target=8, save_if_unaffordable=True)

    async def _defense(self, frame, tags, target):
        """For each Marine, try kiting, then in-range fire, then an attack-move fallback."""
        advance = []
        for tag in tags:
            if await frame.run(kite, key="defense/kite", group="defense", priority=80, tags=(tag,)):
                continue
            if not await frame.run(focus_fire, key="defense/focus", group="defense", priority=70, tags=(tag,)):
                advance.append(tag)
        await frame.run(attack_move, key="defense/advance", group="defense", priority=40,
                        tags=advance, target=target)

    async def _harass(self, frame, tags, target):
        """Send damaged BCs home; let the rest focus fire or advance.

        Prefer an available or active jump for withdrawal, otherwise retreat by path.
        Units near home rejoin pressure; this baseline has no repair phase.
        """
        advance, jump, retreating = [], [], []
        home = frame.spatial.map.main
        for tag in tags:
            unit = frame.units.own[tag]
            if unit.health >= unit.health_max * .3 or unit.distance_to(home) < 12:
                advance.append(tag)
            elif (any(o.ability.id == A.EFFECT_TACTICALJUMP for o in unit.orders)
                  or A.EFFECT_TACTICALJUMP in await frame.actions.available(unit)):
                jump.append(tag)
            else:
                retreating.append(tag)
        await frame.run(tactical_jump, key="harass/jump", group="harass", priority=110, tags=jump, target=home)
        await frame.run(retreat, key="harass/retreat", group="harass", priority=105, tags=retreating, target=home)
        moving = []
        for tag in advance:
            if not await frame.run(focus_fire, key="harass/focus", group="harass", priority=70, tags=(tag,)):
                moving.append(tag)
        await frame.run(attack_move, key="harass/advance", group="harass", priority=40, tags=moving, target=target)

    async def _scout(self, frame):
        """Send the claimed worker to enemy start; return on timeout or low health."""
        tags = frame.tags("scout")
        if not tags:
            return
        self.scout_tag = tags[0]
        worker, map_data = frame.units.own[self.scout_tag], frame.spatial.map
        returning = frame.game.state.game_loop >= 4032 or worker.health < 20
        if returning and worker.distance_to(map_data.main) < 8:
            self.scout_finished = True
            return
        await frame.run(path_to_target, key="scout/path", group="scout", priority=35, tags=tags,
            target=map_data.main if returning else map_data.enemy_starts[0]
            if map_data.enemy_starts else map_data.center)
