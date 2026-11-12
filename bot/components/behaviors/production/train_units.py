from ..helpers import count, subjects


async def train_units(frame, *, product, producers, target, save_if_unaffordable=False):
    """Fill available normal/Reactor slots until the total product target is covered.

    producers supplies unit tags; target is the desired total across all owned
    groups, including pending production. True means a request was proposed.
    Actions decides spending; save_if_unaffordable requests temporary saving.
    """
    remaining = max(0, target - count(frame, product))
    handled = False
    ability = frame.actions.ability_for(product)
    eligible = [p for p in subjects(frame, producers) if p.is_ready and not p.is_flying
                and len(p.orders) < 1 + int(p.add_on_tag in frame.game.reactor_tags)]
    if remaining:
        await frame.actions.available_many(eligible)
    for producer in eligible:
        if not remaining:
            break
        if ability not in await frame.actions.available(producer):
            continue
        # Submitted requests occupy slots until native orders confirm them.
        pending = sum(r.unit_tag == producer.tag and r.product is not None and not r.construction
                      for r in frame.actions.pending.values())
        slots = 1 + int(producer.add_on_tag in frame.game.reactor_tags) - len(producer.orders) - pending
        for index in range(min(remaining, slots)):
            frame.actions.propose(producer.tag, ability, product=product,
                queue=bool(producer.orders) or index > 0, save_if_unaffordable=save_if_unaffordable)
            remaining -= 1
            handled = True
    return handled
