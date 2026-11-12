from sc2.ids.ability_id import AbilityId as A


async def mining(frame, *, gas_workers=3):
    """Assign available group workers to gas and nearby minerals.

    gas_workers is the target per active gas building. Preserve cargo returns and
    stable assignments; rebalance mineral overloads every 224 game loops.
    True means any worker is handled. Workers leased for construction stay excluded.
    """
    game, handled = frame.game, False
    workers = frame.units.workers(frame.group)
    bases = tuple(b for b in game.townhalls if b.is_ready and not b.is_flying)
    if not bases:
        return False
    gas = tuple(g for g in game.gas_buildings if g.is_ready and g.vespene_contents > 0
                and any(g.distance_to(base) < 12 for base in bases))
    allocated, needs = set(), []
    for refinery in gas:
        assigned = sorted((w for w in workers if w.orders and w.orders[0].target == refinery.tag),
                          key=lambda w: (w.is_carrying_resource, w.distance_to(refinery), w.tag))
        # Native counts also include workers returning cargo away from the refinery.
        observed = max(refinery.assigned_harvesters, len(assigned))
        keep = max(0, len(assigned) - max(0, observed - gas_workers))
        allocated.update(w.tag for w in assigned[:keep])
        needs.append((refinery, max(0, gas_workers - observed)))
    for refinery, missing in needs:
        candidates = sorted((w for w in workers if w.tag not in allocated
                             and not w.is_carrying_resource),
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
        if worker.tag not in allocated and worker.orders and worker.orders[0].target in loads:
            loads[worker.orders[0].target] += 1
    rebalance = game.state.game_loop % 224 == 0
    for worker in workers:
        if worker.tag in allocated or worker.is_carrying_resource:
            if worker.is_carrying_resource and worker.is_idle:
                if not frame.actions.maintained(worker, A.HARVEST_RETURN):
                    frame.actions.propose(worker.tag, A.HARVEST_RETURN)
                handled = True
            handled |= bool(worker.orders) or worker.tag in allocated
            continue
        previous = worker.orders[0].target if worker.orders else None
        if previous in loads and (not rebalance or loads[previous] <= 2
                                  or not any(load < 2 for load in loads.values())):
            handled = True
            continue
        mineral = min(minerals, key=lambda m: (loads[m.tag] >= 2,
            worker.distance_to(m) + loads[m.tag]*3, m.tag), default=None)
        if mineral is not None:
            if previous in loads:
                loads[previous] -= 1
            loads[mineral.tag] += 1
            if not frame.actions.maintained(worker, A.HARVEST_GATHER, mineral.tag):
                frame.actions.propose(worker.tag, A.HARVEST_GATHER, mineral.tag)
            handled = True
    return handled
