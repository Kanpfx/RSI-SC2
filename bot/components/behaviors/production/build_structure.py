"""Stateless construction proposal; the core retains accepted jobs by goal key."""

from sc2.position import Point2


async def build_structure(frame, *, product, near=None, geyser_tag=None,
                  addon_space=False, exact=False, save_if_unaffordable=False):
    """Maintain a build goal, resume a nearby unfinished structure or propose a site.

    near anchors placement; geyser_tag selects a gas site. exact tests the snapped
    anchor only, while addon_space reserves room beside the future structure.
    True means the goal is active or proposed. Actions selects and leases the worker.
    """
    construction, game = frame.actions.construction, frame.game
    active = construction.active(frame.current.key)
    if active is not None:
        if active.product != product:
            raise ValueError("An active construction key cannot change product")
        return True
    if game.tech_requirement_progress(product) < 1:
        return False
    geyser = frame.actions.target(geyser_tag) if geyser_tag is not None else None
    if geyser_tag is not None and geyser is None:
        return False
    origin = geyser.position if geyser is not None else Point2(near) if near is not None else None
    if origin is None:
        raise ValueError("Construction requires near or geyser_tag")
    unfinished = min((b for b in game.structures if b.type_id == product and not b.is_ready
                     and not construction.tracked(b)
                     and b.distance_to(origin) <= (1 if exact or geyser else 24)),
                     key=lambda b: (b.distance_to(origin), b.tag), default=None)
    if unfinished is not None:
        frame.actions.build(product, unfinished.tag, resume=True, addon_space=addon_space)
        return True
    if geyser is not None:
        if not await game.can_place_single(product, geyser.position):
            return False
        target = geyser.tag
    else:
        position = await frame.spatial.placement.find(product, origin,
            addon_space=addon_space, exact=exact)
        if position is None:
            return False
        target = position
    frame.actions.build(product, target, addon_space=addon_space,
                        save_if_unaffordable=save_if_unaffordable)
    return True
