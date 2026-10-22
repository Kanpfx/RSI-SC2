"""Engine-validated placement with mineral access and addon reservations."""
from sc2.position import Point2
from sc2.ids.unit_typeid import UnitTypeId as U


def overlaps(a, b):
    """Test overlap of (left, bottom, right, top) rectangles; touching edges are allowed."""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


class Placement:
    def __init__(self, game, map_data):
        """Bind native placement queries and initialize accepted-request footprint reservations."""
        self.game, self.map = game, map_data
        self.reserved = {}

    def footprint(self, product, position, addon_space=False):
        """Calculate a building rectangle, optionally extending its right side for Terran addons."""
        half = self.game.game_data.units[product.value].footprint_radius
        x, y = position
        return x-half, y-half, x+half+(2 if addon_space else 0), y+half

    def conflicts(self, footprint):
        """Check whether a proposed footprint overlaps any retained spatial reservation."""
        return any(overlaps(footprint, rectangle) for rectangle in self.reserved.values())

    def reserve(self, request_id, footprint):
        """Retain an accepted construction footprint until Actions releases its request."""
        self.reserved[request_id] = footprint

    def release(self, request_id):
        """Drop a request's footprint reservation; repeated releases are harmless."""
        self.reserved.pop(request_id, None)

    def clear_access(self, product, position, addon_space=False):
        """Filter candidates by build grid, reservations, resource corridors and ramp clearance.

        Townhalls may occupy their own mineral corridor. This geometric heuristic
        only filters candidates; native engine queries remain authoritative.
        """
        rectangle = self.footprint(product, position, addon_space)
        left, bottom, right, top = rectangle
        height, width = self.map.buildable.shape
        if not (0 <= left < right < width and 0 <= bottom < top < height):
            return False
        if not self.map.buildable[int(bottom):int(top+.999), int(left):int(right+.999)].all():
            return False
        if self.conflicts(rectangle):
            return False
        for resource in self.map.resources:
            nearest = Point2((min(max(resource.x, left), right),
                              min(max(resource.y, bottom), top)))
            if nearest.distance_to(resource) < 1.5:
                return False
            # Keep the worker corridor between each mineral patch and its base open.
            base = min(self.map.expansions, key=lambda p: p.distance_to(resource), default=None)
            if product != U.COMMANDCENTER and base is not None and base.distance_to(resource) < 12:
                for fraction in (.25, .5, .75):
                    point = base.towards(resource, base.distance_to(resource) * fraction)
                    if left-1 < point.x < right+1 and bottom-1 < point.y < top+1:
                        return False
        return position.distance_to(self.map.ramp) > 3

    async def find(self, product, near, *, addon_space=False, max_distance=16, exact=False):
        """Find an engine-validated site near an anchor, or None when no candidate is legal.

        Snap positions to the product footprint and batch candidate queries. exact
        restricts search to the snapped anchor; addon_space also validates the addon site.
        This query does not reserve space; only accepted requests create reservations.
        """
        near = Point2(near)
        half = self.game.game_data.units[product.value].footprint_radius
        offset = half % 1
        center = Point2((round(near.x-offset)+offset, round(near.y-offset)+offset))
        candidates = [center] if exact else [
            Point2((center.x+dx, center.y+dy))
            for dy in range(-max_distance, max_distance+1, 2)
            for dx in range(-max_distance, max_distance+1, 2)]
        candidates.sort(key=lambda p: (p.distance_to(near), p.x, p.y))
        # Test a batch through the engine rather than one RPC per candidate.
        candidates = [p for p in candidates if self.clear_access(product, p, addon_space)]
        for index in range(0, len(candidates), 32):
            batch = candidates[index:index+32]
            legal = await self.game.can_place(product, batch)
            for position, valid in zip(batch, legal):
                if valid:
                    if addon_space:
                        if not await self.game.can_place_single(U.SUPPLYDEPOT,
                                position + Point2((2.5, -.5))):
                            continue
                    return position
        return None
