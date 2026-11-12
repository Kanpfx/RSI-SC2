from sc2.ids.unit_typeid import UnitTypeId as U

from ..helpers import subjects


async def addon(frame, *, product, producers, save_if_unaffordable=False):
    """Maintain or propose addon construction on the supplied producer tags.

    Require a ready, idle producer with no addon, an available ability and a clear
    addon site. True means at least one producer has an active or proposed build.
    """
    ability, handled = frame.actions.ability_for(product), False
    for producer in subjects(frame, producers):
        if frame.actions.construction.active(f"{frame.current.key}/{producer.tag}"):
            handled = True
            continue
        if not producer.is_ready or not producer.is_idle or producer.add_on_tag:
            continue
        if ability not in await frame.actions.available(producer):
            continue
        # A Supply Depot has the same 2x2 footprint as the addon site.
        if not await frame.game.can_place_single(U.SUPPLYDEPOT, producer.add_on_position):
            continue
        frame.actions.build(product, actor=producer.tag, save_if_unaffordable=save_if_unaffordable)
        handled = True
    return handled
