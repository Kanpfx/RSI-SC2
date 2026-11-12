from sc2.ids.ability_id import AbilityId as A
from sc2.position import Point2

from ..helpers import subjects


async def path_to_target(frame, *, tags, target):
    """Move to a nearby waypoint on a threat-aware ground/air path.

    True means any unit has arrived or has a route. Blocked targets use a nearby
    walkable destination; failed searches leave the unit available for a fallback.
    """
    if target is None:
        return False
    handled = False
    for unit in subjects(frame, tags):
        if unit.distance_to(Point2(target)) <= 1.5:
            handled = True
            continue
        path = frame.spatial.pathing.path(unit.position, target, flying=unit.is_flying)
        if not path:
            continue
        waypoint = path[min(4, len(path) - 1)]
        if unit.distance_to(waypoint) > .5 and not frame.actions.maintained(unit, A.MOVE, waypoint):
            frame.actions.propose(unit.tag, A.MOVE, waypoint)
        handled = True
    return handled
