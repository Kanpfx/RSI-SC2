from sc2.ids.ability_id import AbilityId as A


class Mining:
    async def execute(self, frame, *, owner="economy", gas_workers=3):
        """Allocate the owner's available workers to gas and nearby minerals.

        Preserve cargo returns and existing assignments; never reclaim construction
        workers or other Tasks' workers. Return whether mining control is maintained
        or requested. This capability sets no global resource budget.
        """
        game, handled = frame.game, False
        workers = frame.units.workers(game, owner)
        bases = tuple(b for b in game.townhalls if b.is_ready)
        if not bases:
            return False
        gas = tuple(g for g in game.gas_buildings if g.is_ready and g.vespene_contents > 0)
        gas_tags = {g.tag for g in gas}
        allocated = set()
        for refinery in gas:
            assigned = [w for w in workers if w.orders and w.orders[0].target == refinery.tag]
            allocated.update(w.tag for w in assigned)
            missing = max(0, gas_workers-refinery.assigned_harvesters)
            candidates = sorted((w for w in workers if w.tag not in allocated and not w.is_carrying_resource
                                 and not (w.orders and w.orders[0].target in gas_tags)),
                                key=lambda w: (w.distance_to(refinery), w.tag))
            for worker in candidates[:missing]:
                allocated.add(worker.tag)
                if not frame.actions.maintained(worker, A.HARVEST_GATHER, refinery.tag):
                    frame.actions.propose(worker.tag, A.HARVEST_GATHER, refinery.tag)
                handled = True
        minerals = tuple(m for m in game.mineral_field if m.mineral_contents > 0
                         and any(m.distance_to(base) < 12 for base in bases))
        loads = {m.tag: 0 for m in minerals}
        for worker in workers:
            if worker.orders and worker.orders[0].target in loads:
                loads[worker.orders[0].target] += 1
        for worker in workers:
            if worker.tag in allocated or worker.is_carrying_resource:
                handled |= bool(worker.orders) or worker.tag in allocated
                continue
            if worker.orders and worker.orders[0].target in loads:
                handled = True
                continue
            mineral = min(minerals, key=lambda m: (loads[m.tag] >= 2,
                worker.distance_to(m) + loads[m.tag]*3, m.tag), default=None)
            if mineral is not None:
                loads[mineral.tag] += 1
                if not frame.actions.maintained(worker, A.HARVEST_GATHER, mineral.tag):
                    frame.actions.propose(worker.tag, A.HARVEST_GATHER, mineral.tag)
                handled = True
        return handled
