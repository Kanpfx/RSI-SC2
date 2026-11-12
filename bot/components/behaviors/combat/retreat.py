from sc2.ids.ability_id import AbilityId as A

from ..helpers import subjects


async def retreat(frame, *, tags, target):
    """Retreat along a threat-aware path, falling back to a local safe point.

    Use the supplied retreat target for routing and local safety tie-breaking.
    True means any unit has a valid local safe point; it may already be there.
    """
    handled = False
    for unit in subjects(frame, tags):
        safe = frame.spatial.pathing.safe_point(unit.position, flying=unit.is_flying, towards=target)
        if safe is None:
            continue
        path = frame.spatial.pathing.path(unit.position, target, flying=unit.is_flying)
        waypoint = path[min(5, len(path) - 1)] if path else safe
        if unit.distance_to(waypoint) > 1 and not frame.actions.maintained(unit, A.MOVE, waypoint):
            frame.actions.propose(unit.tag, A.MOVE, waypoint)
        handled = True
    return handled
