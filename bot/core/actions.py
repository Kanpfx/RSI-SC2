"""Collect, arbitrate and submit requests; observations confirm effects later."""
from dataclasses import dataclass

from sc2.ids.ability_id import AbilityId as A
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2
from sc2.unit_command import UnitCommand

ADDONS = {U.BARRACKSTECHLAB, U.FACTORYTECHLAB, U.STARPORTTECHLAB,
          U.BARRACKSREACTOR, U.FACTORYREACTOR, U.STARPORTREACTOR}
ORDINARY = {A.MOVE, A.ATTACK, A.HARVEST_GATHER, A.SMART, A.STOP}


@dataclass(slots=True)
class ActionRequest:
    request_id: str
    call_id: str
    source: str
    frame: int
    unit_tag: int
    ability: A
    target: object
    priority: int
    queue: bool = False
    product: U | UpgradeId | None = None
    save_if_unaffordable: bool = False
    save_timeout: int = 224
    footprint: tuple | None = None
    construction: bool = False
    status: str = "proposed"
    reason: str = ""
    structure_tag: int = 0


def same_target(a, b):
    """Compare tag/None targets exactly and point targets with a small positional tolerance."""
    if isinstance(a, (tuple, Point2)) and isinstance(b, (tuple, Point2)):
        return Point2(a).distance_to(Point2(b)) < .75
    return a == b


class Actions:
    def __init__(self, game, units, placement):
        """Bind native execution services and initialize requests, evidence and reservation state."""
        self.game, self.units, self.placement = game, units, placement
        self.source = None
        self.requests = []
        self.history = {}
        self.pending = {}
        self.records = []
        self._sequence = 0
        self._abilities = {}
        self._saving_since = {}
        self._demands = set()

    def canonical(self, ability):
        """Normalize an ability alias through the installed game's ability data."""
        return self.game.game_data.abilities[ability.value].id

    def ability_for(self, product):
        """Resolve a unit/building creation ability or upgrade research ability from native data."""
        data = self.game.game_data
        ability = (data.upgrades[product.value].research_ability if isinstance(product, UpgradeId)
                   else data.units[product.value].creation_ability)
        return ability.id

    def propose(self, unit_tag, ability, target=None, *, queue=False, product=None,
                save_if_unaffordable=False, save_timeout=224, footprint=None, construction=False):
        """Collect a request under the active frame.run call and return its generated ID.

        Priority and source are inherited automatically. Targets are tags, Point2
        positions or None; Macro products identify costs. No command is submitted here.
        """
        call = self.source()
        if not call.id:
            raise RuntimeError("Actions must originate from frame.run")
        self._sequence += 1
        request = ActionRequest(f"request{self._sequence}", call.id, call.path,
            self.game.state.game_loop, unit_tag, self.canonical(ability), target, call.priority,
            queue, product, save_if_unaffordable, save_timeout, footprint, construction)
        self.requests.append(request)
        self.history[request.request_id] = request
        self._record(request)
        return request.request_id

    def result(self, request_id):
        """Return a retained request and its latest status, or None if absent or expired."""
        return self.history.get(request_id)

    def _record(self, request, status=None, reason=""):
        """Append an immutable telemetry snapshot, optionally advancing the request's status."""
        if status:
            request.status, request.reason = status, reason
        self.records.append(dict(frame=self.game.state.game_loop, request_id=request.request_id,
            call_id=request.call_id, source=request.source, unit_tag=request.unit_tag,
            ability=request.ability.name, target=request.target, priority=request.priority,
            product=request.product.name if request.product is not None else None,
            queue=request.queue, status=request.status, reason=request.reason))

    async def available(self, unit):
        """Cache canonical abilities for this frame while ignoring mineral/gas requirements."""
        if unit.tag not in self._abilities:
            answers = await self.game.client.query_available_abilities([unit], ignore_resource_requirements=True)
            self._abilities[unit.tag] = {self.canonical(a) for a in answers[0]}
        return self._abilities[unit.tag]

    def maintained(self, unit, ability, target=None):
        """Check matching native orders or unconfirmed submissions to avoid restarting control."""
        ability = self.canonical(ability)
        return any(self.canonical(o.ability.id) == ability and same_target(o.target, target)
                   for o in unit.orders) or any(
                       r.unit_tag == unit.tag and r.ability == ability and same_target(r.target, target)
                       for r in self.pending.values())

    def pending_product(self, product):
        """Count accepted requests not yet represented by an engine order/building."""
        return sum(r.product == product and r.status == "submitted" for r in self.pending.values())

    def active_build(self, product, source=None):
        """Find an accepted construction still being tracked, optionally scoped to a call path."""
        return next((r for r in self.pending.values() if r.construction and r.product == product
                     and (source is None or r.source == source)), None)

    def begin(self):
        """Start a frame by clearing transient caches, observing submissions and pruning old results."""
        self._abilities.clear()
        self.requests.clear()
        self._demands.clear()
        self._observe()
        loop = self.game.state.game_loop
        for identity, request in list(self.history.items()):
            if identity not in self.pending and loop - request.frame > 448:
                del self.history[identity]

    def _finish(self, request, status, reason):
        """Record a terminal outcome and release the request's worker and footprint reservations."""
        self._record(request, status, reason)
        self.pending.pop(request.request_id, None)
        self.units.release(request.request_id)
        self.placement.release(request.request_id)

    def _observe(self):
        """Advance accepted requests using native errors, orders and building observations.

        Associate engine errors only when unique. Construction remains tracked until
        completion or failure; ordinary requests without unique evidence become unknown.
        """
        game, loop = self.game, self.game.state.game_loop
        own = {u.tag: u for u in game.units + game.structures}
        errors = {}
        observations = list(self.pending.values())
        for error in game.state.action_errors:
            ability = game.game_data.abilities[error.ability_id].id
            candidates = [r for r in self.pending.values()
                          if r.unit_tag == error.unit_tag and r.ability == ability]
            if len(candidates) == 1:
                errors[candidates[0].request_id] = f"engine:{error.result}"
            elif candidates:
                self.records.append(dict(frame=loop, status="ambiguous_error", unit_tag=error.unit_tag,
                    ability=ability.name, reason=f"engine:{error.result}",
                    candidates=[r.request_id for r in candidates]))
        for request in list(self.pending.values()):
            actor = own.get(request.unit_tag)
            if request.request_id in errors:
                self._finish(request, "failed", errors[request.request_id])
                continue
            if request.construction:
                position = self._position(request)
                building = next((u for u in game.structures if u.type_id == request.product
                                 and u.position.distance_to(position) < 1), None)
                if building is not None:
                    request.structure_tag = building.tag
                    if building.is_ready:
                        self._finish(request, "completed", "construction_complete")
                    elif actor is None:
                        self._finish(request, "failed", "builder_died")
                    elif loop - request.frame > 2240:
                        self._finish(request, "failed", "construction_timeout")
                    elif actor.type_id == U.SCV and not actor.is_constructing_scv and actor.is_idle:
                        if loop - request.frame > 32:
                            self._finish(request, "failed", "construction_interrupted")
                    elif request.status != "observed":
                        self._record(request, "observed", "construction_started")
                elif request.structure_tag:
                    self._finish(request, "failed", "structure_destroyed")
                elif actor is None:
                    self._finish(request, "failed", "actor_died")
                elif loop - request.frame > 224:
                    self._finish(request, "failed", "construction_timeout")
            elif actor is None:
                self._finish(request, "failed", "actor_died")
            elif sum(r.unit_tag == request.unit_tag and r.ability == request.ability
                     and same_target(r.target, request.target) for r in observations) == 1 and any(
                     self.canonical(o.ability.id) == request.ability
                     and same_target(o.target, request.target) for o in actor.orders):
                self._finish(request, "observed", "unit_order")
            elif loop - request.frame > 32:
                self._finish(request, "unknown", "no_unique_evidence")

    def _position(self, request):
        """Resolve a construction site's point from its parent, target or retained footprint."""
        if request.product in ADDONS:
            actor = self.game.structures.find_by_tag(request.unit_tag)
            # The footprint is authoritative even if the parent has died.
            if actor:
                return actor.add_on_position
        if isinstance(request.target, int):
            target = self.game.all_units.find_by_tag(request.target)
            if target:
                return target.position
        if request.footprint and (request.target is None or isinstance(request.target, int)):
            left, bottom, right, top = request.footprint
            return Point2(((left+right)/2, (bottom+top)/2))
        return Point2(request.target)

    async def resolve(self):
        """Arbitrate current requests in stable descending priority order and submit winners.

        Preserve each source's queue order. Check feasibility, unit conflicts and
        footprints before spending a local mineral/gas/supply budget. Eligible saving
        demands may protect budget temporarily; deferred demands are not carried over
        as queued work. Accepted construction registers occupancy and spatial claims.
        """
        game = self.game
        own = {u.tag: u for u in game.units + game.structures}
        all_units = {u.tag: u for u in game.all_units}
        minerals, gas, supply = game.minerals, game.vespene, game.supply_left
        actors, accepted_products, reserved_resources = {}, set(), False
        # A queue tail cannot jump ahead of its head through priority sorting.
        groups, tails = [], {}
        for request in self.requests:
            key = request.unit_tag, request.source
            if not request.queue or key not in tails:
                tails[key] = []
                groups.append(tails[key])
            tails[key].append(request)
        for group_index, group in sorted(enumerate(groups), key=lambda pair: -pair[1][0].priority):
            blocked = False
            for request in group:
                actor = own.get(request.unit_tag)
                reason = "preceding_action_rejected" if blocked else ""
                if not reason:
                    reason = await self._legal(request, actor, all_units)
                if not reason and request.unit_tag in actors and not (
                        request.queue and actors[request.unit_tag] == group_index):
                    reason = "unit_conflict"
                if not reason and isinstance(request.product, UpgradeId) and request.product in accepted_products:
                    reason = "duplicate_research"
                if not reason and request.footprint and self.placement.conflicts(request.footprint):
                    reason = "placement_conflict"
                if reason:
                    self._record(request, "rejected", reason)
                    blocked = True
                    continue
                recovering = request.construction and request.ability == A.SMART
                cost = game.calculate_cost(A.SMART if recovering else request.product or request.ability)
                needed_supply = (max(0, game.calculate_supply_cost(request.product))
                                 if isinstance(request.product, U) and not request.construction else 0)
                if needed_supply > supply:
                    self._record(request, "deferred", "insufficient_supply")
                    blocked = True
                    continue
                key = (request.source, request.ability, request.product)
                if minerals < cost.minerals or gas < cost.vespene:
                    reason = "reserved_by_higher_priority" if reserved_resources else "insufficient_resources"
                    if request.save_if_unaffordable:
                        self._demands.add(key)
                        since = self._saving_since.setdefault(key, game.state.game_loop)
                        if game.state.game_loop - since < request.save_timeout:
                            minerals = max(0, minerals - cost.minerals)
                            gas = max(0, gas - cost.vespene)
                            reserved_resources = True
                        else:
                            reason = "saving_timeout"
                    self._record(request, "deferred", reason)
                    blocked = True
                    continue
                target = all_units[request.target] if isinstance(request.target, int) else request.target
                # Construct UnitCommand directly so business modules never invoke Unit commands.
                queued = game.do(UnitCommand(request.ability, actor, target, request.queue))
                if not queued:
                    self._record(request, "failed", "local_rejected")
                    blocked = True
                    continue
                minerals -= cost.minerals
                gas -= cost.vespene
                supply -= needed_supply
                actors[request.unit_tag] = group_index
                accepted_products.add(request.product)
                self._record(request, "submitted", "local_queued")
                self.pending[request.request_id] = request
                if request.construction:
                    if actor.type_id == U.SCV:
                        self.units.occupy(actor.tag, request.request_id)
                    if request.footprint:
                        self.placement.reserve(request.request_id, request.footprint)
        self._saving_since = {key: since for key, since in self._saving_since.items() if key in self._demands}

    async def _legal(self, request, actor, all_units):
        """Return a pre-budget rejection reason, or an empty string for a locally eligible request.

        Validate actors, targets, occupancy, duplicate control, abilities and placement.
        These checks do not guarantee acceptance by the SC2 engine.
        """
        if actor is None:
            return "actor_missing"
        if isinstance(request.target, int) and request.target not in all_units:
            return "target_missing"
        if actor.tag in self.units.occupied:
            return "unit_occupied"
        if not request.queue and self.maintained(actor, request.ability, request.target):
            return "duplicate_order"
        if request.product is not None:
            if actor.is_structure and (not actor.is_ready or (not request.queue and not actor.is_idle)):
                return "producer_unavailable"
            recovering = request.construction and request.ability == A.SMART
            if not recovering and request.ability not in await self.available(actor):
                return "ability_unavailable"
            if request.construction and not recovering:
                position = self._position(request)
                placement_type = U.SUPPLYDEPOT if request.product in ADDONS else request.product
                if not await self.game.can_place_single(placement_type, position):
                    return "invalid_placement"
        elif request.ability not in ORDINARY and request.ability not in await self.available(actor):
            return "ability_unavailable"
        return ""

    def drain(self):
        """Transfer accumulated request-state records to telemetry and clear the record buffer."""
        records, self.records = self.records, []
        return records
