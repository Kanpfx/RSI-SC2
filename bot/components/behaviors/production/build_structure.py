"""One local construction attempt, including interrupted-building recovery."""
from sc2.ids.ability_id import AbilityId as A
from sc2.position import Point2


class BuildStructure:
    def __init__(self):
        """Retain the latest request ID so later calls can maintain the same local attempt."""
        self.request_id = None

    async def execute(self, frame, *, product, near=None, geyser_tag=None, addon_space=False,
                      exact=False, worker_owner="economy", save_if_unaffordable=False):
        """Maintain, resume or propose construction of the supplied product.

        Use near for placement search, exact for a snapped fixed site, or geyser_tag
        for gas. Select an unoccupied worker from worker_owner and delegate placement
        to Spatial. Resume unfinished buildings without paying their cost again.
        Return True for active construction or a new request, not confirmed success;
        Actions occupies the worker and footprint only after accepting the request.
        """
        previous = frame.actions.result(self.request_id)
        if previous is not None and previous.request_id in frame.actions.pending:
            return True
        game = frame.game
        if game.tech_requirement_progress(product) < 1:
            return False
        if frame.actions.active_build(product):
            return True
        geyser = game.vespene_geyser.find_by_tag(geyser_tag) if geyser_tag is not None else None
        if geyser_tag is not None and geyser is None:
            return False
        origin = geyser.position if geyser else Point2(near) if near is not None else None
        if origin is None:
            raise ValueError("BuildStructure needs near or geyser_tag")
        workers = frame.units.workers(game, worker_owner)
        worker = min((w for w in workers if not w.is_carrying_resource),
                     key=lambda w: (w.distance_to(origin), w.tag), default=None)
        if worker is None:
            return False
        # An unfinished structure is resumed, never paid for a second time.
        unfinished = next((b for b in game.structures if b.type_id == product and not b.is_ready
                           and not any(r.structure_tag == b.tag for r in frame.actions.pending.values())), None)
        if unfinished is not None:
            footprint = frame.spatial.placement.footprint(product, unfinished.position, addon_space)
            self.request_id = frame.actions.propose(worker.tag, A.SMART, unfinished.tag,
                product=product, footprint=footprint, construction=True)
            return True
        if geyser:
            if not await game.can_place_single(product, geyser.position):
                return False
            position, target = geyser.position, geyser.tag
        else:
            position = await frame.spatial.placement.find(product, origin,
                addon_space=addon_space, exact=exact)
            if position is None:
                return False
            target = position
        ability = frame.actions.ability_for(product)
        if ability not in await frame.actions.available(worker):
            return False
        footprint = frame.spatial.placement.footprint(product, position, addon_space)
        self.request_id = frame.actions.propose(worker.tag, ability, target, product=product,
            save_if_unaffordable=save_if_unaffordable, footprint=footprint, construction=True)
        return True
