from ..helpers import subjects


async def research(frame, *, upgrade, producers, save_if_unaffordable=False):
    """Propose an upgrade from the first eligible supplied producer.

    Skip completed or pending research; require an idle, ready producer with the
    ability available. True means a request was proposed; Actions decides spending.
    """
    if frame.game.already_pending_upgrade(upgrade) > 0:
        return False
    ability = frame.actions.ability_for(upgrade)
    for producer in subjects(frame, producers):
        if producer.is_ready and producer.is_idle and ability in await frame.actions.available(producer):
            frame.actions.propose(producer.tag, ability, product=upgrade,
                                  save_if_unaffordable=save_if_unaffordable)
            return True
    return False
