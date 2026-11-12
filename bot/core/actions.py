"""Per-frame actor, resource and command arbitration; no strategic decisions."""

from dataclasses import dataclass

from sc2.ids.ability_id import AbilityId as A
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2
from sc2.unit_command import UnitCommand

from .construction import ADDONS, Construction

ORDINARY = {A.MOVE, A.ATTACK, A.HARVEST_GATHER, A.HARVEST_RETURN, A.SMART, A.STOP}


def same_target(a, b):
    # Native orders use 0 for a missing target; local requests use None.
    if a in (None, 0) and b in (None, 0):
        return True
    if isinstance(a, (tuple, Point2)) and isinstance(b, (tuple, Point2)):
        return Point2(a).distance_to(Point2(b)) < 0.75
    return a == b


@dataclass(slots=True)
class ActionRequest:
    """One proposed command and its submission/observation state."""

    request_id: str
    call_id: str
    source: str
    group: str
    frame: int
    unit_tag: int | None
    ability: A
    target: object
    priority: int
    product: U | UpgradeId | None = None
    queue: bool = False
    save_if_unaffordable: bool = False
    save_timeout: int = 224
    footprint: tuple | None = None
    construction: bool = False
    takeover: bool = False
    status: str = "proposed"
    reason: str = ""
    structure_tag: int = 0
    expected_orders: int = 0  # Matching native orders required to confirm this request.


class Actions:
    """Validate requests and arbitrate actors, resources, supply and construction space."""

    def __init__(self, frame):
        self.frame, self.game = frame, frame.game
        self.construction = Construction(frame)
        self.requests, self.records = [], []
        self.pending = {}
        self._sequence = 0
        self._abilities = {}
        self._saving_since = {}
        self._targets = {}

    def canonical(self, ability):
        """Normalize native ability aliases before comparison and submission."""
        return self.game.game_data.abilities[ability.value].id

    def ability_for(self, product):
        """Read a unit's creation ability or an upgrade's research ability."""
        data = self.game.game_data
        ability = (data.upgrades[product.value].research_ability if isinstance(product, UpgradeId)
                   else data.units[product.value].creation_ability)
        return ability.id

    def target(self, tag):
        return self._targets.get(tag)

    def propose(self, unit_tag, ability, target=None, *, product=None, queue=False,
                save_if_unaffordable=False, save_timeout=224, takeover=False):
        """Propose a command; takeover permits abandoning this actor's current build."""
        if unit_tag is None:
            raise ValueError("Commands require an actor; use build() for automatic builders")
        if type(takeover) is not bool:
            raise ValueError("takeover must be a bool")
        if queue and takeover:
            raise ValueError("Takeover commands cannot be queued")
        return self._request(unit_tag, ability, target, product=product, queue=queue,
            save_if_unaffordable=save_if_unaffordable, save_timeout=save_timeout, takeover=takeover)

    def build(self, product, target=None, *, actor=None, addon_space=False,
              resume=False, save_if_unaffordable=False):
        """Propose construction with a derived ability and footprint.

        Omit actor for automatic worker selection; addons require their producer.
        Resume targets an unfinished own structure without paying its cost again.
        """
        if not isinstance(product, U):
            raise ValueError("Construction requires a unit product")
        if resume:
            unfinished = self.frame.units.own.get(target) if isinstance(target, int) else None
            if unfinished is None or unfinished.type_id != product or unfinished.is_ready:
                raise ValueError("Resume requires an unfinished own structure of this product")
        if product in ADDONS:
            producer = self.frame.units.own.get(actor)
            if producer is None:
                raise ValueError("Addon construction requires a producer")
            position = producer.add_on_position
        elif isinstance(target, int):
            unit = self.target(target)
            if unit is None:
                raise ValueError("Construction target is missing")
            position = unit.position
        elif target is not None:
            position = Point2(target)
        else:
            raise ValueError("Construction requires a position or target tag")
        return self._request(actor, A.SMART if resume else self.ability_for(product), target,
            product=product, construction=True, save_if_unaffordable=save_if_unaffordable,
            footprint=self.frame.spatial.placement.footprint(product, position, addon_space))

    def _request(self, unit_tag, ability, target, **options):
        call = self.frame.current
        if not call.id or not call.group or not self.frame.units.resolved:
            raise RuntimeError("Actions require a scoped behavior and allocated group")
        if options.get("save_timeout", 224) < 0:
            raise ValueError("save_timeout cannot be negative")
        self._sequence += 1
        source = (f"{call.key}/{unit_tag}"
                  if options.get("construction") and unit_tag is not None else call.key)
        request = ActionRequest(request_id=f"request{self._sequence}", call_id=call.id,
            source=source, group=call.group, frame=self.game.state.game_loop,
            unit_tag=unit_tag, ability=self.canonical(ability), target=target,
            priority=call.priority, **options)
        self.requests.append(request)
        self.record(request)
        return request

    def record(self, request, status=None, reason=""):
        """Append a request-state snapshot for passive telemetry."""
        if status is not None:
            request.status, request.reason = status, reason
        self.records.append(dict(frame=self.game.state.game_loop, request_id=request.request_id,
            call_id=request.call_id, source=request.source, group=request.group,
            unit_tag=request.unit_tag, ability=request.ability.name, target=request.target,
            priority=request.priority, product=request.product.name if request.product is not None else None,
            queue=request.queue, takeover=request.takeover, status=request.status, reason=request.reason))

    async def available_many(self, units):
        """Batch and cache native ability queries per frame, ignoring resource costs."""
        missing = {u.tag: u for u in units if u.tag not in self._abilities}
        if missing:
            answers = await self.game.client.query_available_abilities(
                list(missing.values()), ignore_resource_requirements=True)
            for tag, abilities in zip(missing, answers, strict=True):
                self._abilities[tag] = {self.canonical(a) for a in abilities}

    async def available(self, unit):
        await self.available_many((unit,))
        return self._abilities[unit.tag]

    def maintained(self, unit, ability, target=None):
        """Check native orders and unconfirmed requests for the same command."""
        ability = self.canonical(ability)
        return any(self.canonical(o.ability.id) == ability and same_target(o.target, target)
                   for o in unit.orders) or any(r.unit_tag == unit.tag and r.ability == ability
                   and same_target(r.target, target) for r in self.pending.values())

    def pending_product(self, product):
        """Count submitted products not yet confirmed by native observations."""
        return sum(r.product == product and r.status == "submitted" for r in self.pending.values())

    def begin(self):
        """Reset frame caches, refresh target lookup and reconcile pending requests."""
        self.requests.clear()
        self._abilities.clear()
        self.frame.spatial.placement.begin()
        self._targets = {u.tag: u for collection in (self.game.all_units,
            self.game.mineral_field, self.game.vespene_geyser) for u in collection}
        self._observe()

    def finish(self, request, status, reason):
        """Record a terminal result and release the request's construction leases."""
        self.record(request, status, reason)
        self.pending.pop(request.request_id, None)
        if request.construction:
            self.construction.release(request)

    def _observe(self):
        """Use native errors, orders and construction evidence to reconcile submissions."""
        observations = list(self.pending.values())
        errors = {}
        for error in self.game.state.action_errors:
            ability = self.game.game_data.abilities[error.ability_id].id
            matches = [r for r in observations if r.unit_tag == error.unit_tag and r.ability == ability]
            if len(matches) == 1:
                errors[matches[0].request_id] = f"engine:{error.result}"
            elif matches:
                self.records.append(dict(frame=self.game.state.game_loop, status="ambiguous_error",
                    unit_tag=error.unit_tag, ability=ability.name, reason=f"engine:{error.result}",
                    candidates=[r.request_id for r in matches]))
        for request in observations:
            actor = self.frame.units.own.get(request.unit_tag)
            if request.request_id in errors:
                self.finish(request, "failed", errors[request.request_id])
            elif request.construction:
                self.construction.observe(request)
            elif actor is None:
                self.finish(request, "failed", "actor_died")
            elif (
                sum(self.canonical(o.ability.id) == request.ability
                    and same_target(o.target, request.target) for o in actor.orders)
                >= (request.expected_orders or 1)
                and (request.expected_orders or sum(
                    r.unit_tag == request.unit_tag and r.ability == request.ability
                    and same_target(r.target, request.target) for r in observations) == 1)
            ):
                self.finish(request, "observed", "unit_order")
            elif self.game.state.game_loop - request.frame > 32:
                self.finish(request, "unknown", "no_unique_evidence")

    async def _worker(self, request, taken):
        """Choose a reachable free worker, preferring empty hands and nearby workers."""
        center = self.construction.position(request)
        workers = sorted((w for w in self.frame.units.workers(request.group) if w.tag not in taken),
                         key=lambda w: (w.is_carrying_resource, w.distance_to(center), w.tag))
        if not workers:
            return None
        pathing = self.frame.spatial.pathing
        pathing.update()
        targets = [center]
        if not pathing.valid(*map(int, center)):
            # Occupied centers need approach points outside the building footprint.
            reach = self.game.game_data.units[request.product.value].footprint_radius + 1
            targets = [center + Point2((dx*reach, dy*reach))
                       for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                                      (1, 1), (1, -1), (-1, 1), (-1, -1))]
            targets = [p for p in targets if pathing.valid(*map(int, p))]
        if not targets:
            return None
        queries = [[w, p] for w in workers for p in targets]
        distances = await self.game.client.query_pathings(queries)
        return next((w for (w, p), distance in zip(queries, distances, strict=True)
                     if distance > 0 or w.distance_to(p) < .5), None)

    async def _legal(self, request, actor):
        """Return a rejection reason, or an empty string when validity checks pass."""
        if actor is None:
            return "actor_missing"
        if self.frame.units.groups.get(actor.tag) != request.group:
            return "unit_not_owned"
        if isinstance(request.target, int) and self.target(request.target) is None:
            return "target_missing"
        lease = self.frame.units.leases.get(actor.tag)
        if lease and not request.takeover:
            return "unit_occupied"
        if lease and lease not in self.pending:
            return "invalid_takeover"
        if not request.queue and not (request.takeover and lease) and self.maintained(actor, request.ability, request.target):
            return "duplicate_order"
        if request.construction and self.construction.active(request.source):
            return "duplicate_goal"
        if request.product is not None:
            if actor.is_structure and (not actor.is_ready or (not request.queue and not actor.is_idle)):
                return "producer_unavailable"
            recovering = request.construction and request.ability == A.SMART
            if not recovering and request.ability not in await self.available(actor):
                return "ability_unavailable"
            if request.construction and not recovering:
                product = U.SUPPLYDEPOT if request.product in ADDONS else request.product
                if not await self.game.can_place_single(product, self.construction.position(request)):
                    return "invalid_placement"
        elif request.ability not in ORDINARY and request.ability not in await self.available(actor):
            return "ability_unavailable"
        return ""

    async def resolve(self):
        """Resolve by priority with a frame-local budget and ordered queue tails."""
        game = self.game
        minerals, gas, supply = game.minerals, game.vespene, game.supply_left
        actors, products, saving, demands = {}, set(), False, set()
        groups, tails = [], {}
        # A rejected head also blocks its queue tail; other groups remain independent.
        for request in self.requests:
            key = ((request.unit_tag, request.source) if request.unit_tag is not None
                   else (request.request_id,))
            if not request.queue or key not in tails:
                tails[key] = []
                groups.append(tails[key])
            tails[key].append(request)
        for index, group in sorted(enumerate(groups), key=lambda pair: -pair[1][0].priority):
            blocked = False
            for request in group:
                actor = self.frame.units.own.get(request.unit_tag)
                if request.unit_tag is None:
                    actor = await self._worker(request, actors)
                    if actor is not None:
                        request.unit_tag = actor.tag
                reason = "preceding_action_rejected" if blocked else await self._legal(request, actor)
                if not reason and actor.tag in actors and not (request.queue and actors[actor.tag] == index):
                    reason = "unit_conflict"
                if not reason and isinstance(request.product, UpgradeId) and request.product in products:
                    reason = "duplicate_research"
                if not reason and request.footprint and self.frame.spatial.placement.conflicts(
                        request.footprint, ignore=request.unit_tag if request.product in ADDONS else None):
                    reason = "placement_conflict"
                if reason:
                    self.record(request, "rejected", reason)
                    blocked = True
                    continue
                recovering = request.construction and request.ability == A.SMART
                cost = game.calculate_cost(A.SMART if recovering else request.product or request.ability)
                needed_supply = (max(0, game.calculate_supply_cost(request.product))
                    if isinstance(request.product, U) and not request.construction else 0)
                if needed_supply > supply:
                    self.record(request, "deferred", "insufficient_supply")
                    blocked = True
                    continue
                if minerals < cost.minerals or gas < cost.vespene:
                    reason = "reserved_by_higher_priority" if saving else "insufficient_resources"
                    if request.save_if_unaffordable:
                        key = (request.source, request.ability, request.product,
                               None if request.construction else request.unit_tag)
                        demands.add(key)
                        since = self._saving_since.setdefault(key, game.state.game_loop)
                        if game.state.game_loop - since < request.save_timeout:
                            minerals, gas = max(0, minerals-cost.minerals), max(0, gas-cost.vespene)
                            saving = True
                        else:
                            reason = "saving_timeout"
                    self.record(request, "deferred", reason)
                    blocked = True
                    continue
                target = self.target(request.target) if isinstance(request.target, int) else request.target
                interrupted = (self.pending.get(self.frame.units.leases.get(actor.tag))
                               if request.takeover else None)
                if not game.do(UnitCommand(request.ability, actor, target, request.queue)):
                    self.record(request, "failed", "local_rejected")
                    blocked = True
                    continue
                if interrupted is not None:
                    self.finish(interrupted, "cancelled", "control_taken_over")
                if request.queue or request.product is not None and not request.construction:
                    # Existing orders cannot confirm newly appended identical commands.
                    request.expected_orders = 1 + sum(
                        r.unit_tag == actor.tag and r.ability == request.ability
                        and same_target(r.target, request.target) and r.frame == game.state.game_loop
                        for r in self.pending.values())
                    if request.queue:
                        request.expected_orders += sum(self.canonical(o.ability.id) == request.ability
                            and same_target(o.target, request.target) for o in actor.orders)
                minerals, gas, supply = minerals-cost.minerals, gas-cost.vespene, supply-needed_supply
                actors[actor.tag] = index
                products.add(request.product)
                self.record(request, "submitted", "local_queued")
                self.pending[request.request_id] = request
                if request.construction:
                    self.construction.accept(request)
        self._saving_since = {key: since for key, since in self._saving_since.items() if key in demands}

    def drain(self):
        records, self.records = self.records, []
        return records
