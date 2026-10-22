from sc2.ids.ability_id import AbilityId as A
from sc2.position import Point2

from ..helpers import subjects


class PathToTarget:
    async def execute(self, frame, *, tags, target):
        """Move supplied units to a nearby waypoint on their threat-aware ground/air path.

        Return True if any unit has a path or has arrived. Unreachable units receive
        no request; never substitute an unsafe straight-line route.
        """
        if target is None:
            return False
        handled = False
        for unit in subjects(frame.game, tags):
            if unit.distance_to(Point2(target)) <= 1.5:
                handled = True
                continue
            path = frame.spatial.pathing.path(unit.position, target, flying=unit.is_flying)
            if not path:
                continue
            waypoint = path[min(4, len(path)-1)]
            if not frame.actions.maintained(unit, A.MOVE, waypoint):
                frame.actions.propose(unit.tag, A.MOVE, waypoint)
            handled = True
        return handled
