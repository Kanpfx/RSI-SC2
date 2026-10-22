from sc2.ids.ability_id import AbilityId as A

from ..helpers import subjects


class Retreat:
    async def execute(self, frame, *, tags, target):
        """Move supplied units toward a retreat point using threat-aware ground/air paths.

        Use a local safe point when no full path is found. Return True if any unit
        has a safe control destination; this does not confirm engine acceptance.
        """
        handled = False
        for unit in subjects(frame.game, tags):
            safe = frame.spatial.pathing.safe_point(unit.position, flying=unit.is_flying, towards=target)
            if safe is None:
                continue
            path = frame.spatial.pathing.path(unit.position, target, flying=unit.is_flying)
            waypoint = path[min(5, len(path)-1)] if path else safe
            if unit.distance_to(waypoint) > 1 and not frame.actions.maintained(unit, A.MOVE, waypoint):
                frame.actions.propose(unit.tag, A.MOVE, waypoint)
            handled = True
        return handled
