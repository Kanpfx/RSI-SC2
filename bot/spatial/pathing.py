"""Lazy visible-threat grids and bounded A* paths for ground and air units."""
from heapq import heappop, heappush
from math import hypot

import numpy as np
from sc2.position import Point2


class Pathing:
    def __init__(self, game, map_data, refresh=16):
        """Configure lazy threat refresh in game loops and per-refresh path caches."""
        self.game, self.map, self.refresh = game, map_data, refresh
        self.updated = -refresh
        self.grids = {}
        self.paths = {}

    def update(self):
        """Refresh visible-enemy ground/air threat grids when due and invalidate cached paths."""
        loop = self.game.state.game_loop
        if loop - self.updated < self.refresh:
            return
        self.updated = loop
        self.map.pathable = self.map._view(self.game.game_info.pathing_grid)
        shape = self.map.pathable.shape
        self.grids = {False: np.zeros(shape, dtype=np.float32),
                      True: np.zeros(shape, dtype=np.float32)}
        for enemy in self.game.enemy_units + self.game.enemy_structures:
            if not enemy.is_visible:
                continue
            for flying in (False, True):
                if not (enemy.can_attack_air if flying else enemy.can_attack_ground):
                    continue
                reach = (enemy.air_range if flying else enemy.ground_range) + enemy.radius + 2
                x, y = enemy.position
                left, right = max(0, int(x-reach)), min(shape[1], int(x+reach)+2)
                bottom, top = max(0, int(y-reach)), min(shape[0], int(y+reach)+2)
                yy, xx = np.ogrid[bottom:top, left:right]
                d = np.hypot(xx-x, yy-y)
                dps = enemy.air_dps if flying else enemy.ground_dps
                self.grids[flying][bottom:top, left:right] += np.where(
                    d <= reach, max(1, dps) * (1 - d / (reach + 1)), 0)
        self.paths.clear()

    def valid(self, x, y, flying=False):
        """Check grid and playable-area bounds, plus ground walkability unless flying."""
        height, width = self.map.pathable.shape
        area = self.map.playable_area
        return (0 <= x < width and 0 <= y < height
                and area.x <= x < area.x + area.width
                and area.y <= y < area.y + area.height
                and (flying or bool(self.map.pathable[y, x])))

    def threat(self, position, flying=False):
        """Return cached threat at a position, refreshing lazily; invalid cells have infinite cost."""
        self.update()
        x, y = map(int, position)
        return float(self.grids[flying][y, x]) if self.valid(x, y, flying) else float("inf")

    def safe_point(self, position, *, flying=False, radius=8, towards=None):
        """Choose a reachable-grid cell with minimum threat in the local search radius.

        Break ties toward the supplied destination, or toward the current position.
        Return None if no valid cell exists; this is a local safety query, not a full path.
        """
        self.update()
        x, y = map(int, position)
        points = [Point2((px + .5, py + .5))
                  for py in range(y-radius, y+radius+1)
                  for px in range(x-radius, x+radius+1)
                  if hypot(px-x, py-y) <= radius and self.valid(px, py, flying)]
        return min(points, key=lambda p: (self.threat(p, flying),
                   p.distance_to(towards if towards is not None else position), p.x, p.y), default=None)

    def path(self, start, target, *, flying=False, max_nodes=12000):
        """Return a cached bounded A* route using distance plus visible threat costs.

        Ground routes respect walkability and cannot cut blocked corners; air routes
        respect playable bounds. Return an empty tuple when invalid, unreachable or
        the node budget is exhausted; never fall back to a straight line.
        """
        self.update()
        origin, goal = tuple(map(int, start)), tuple(map(int, target))
        key = origin, goal, flying, max_nodes
        if key in self.paths:
            return self.paths[key]
        if not self.valid(*origin, flying) or not self.valid(*goal, flying):
            return ()
        queue, cost, parent = [(0, origin)], {origin: 0}, {}
        visited = set()
        while queue and len(visited) < max_nodes:
            _, current = heappop(queue)
            if current in visited:
                continue
            if current == goal:
                route = [goal]
                while route[-1] != origin:
                    route.append(parent[route[-1]])
                result = tuple(Point2((x+.5, y+.5)) for x, y in reversed(route))
                self.paths[key] = result
                return result
            visited.add(current)
            x, y = current
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                           (1, 1), (1, -1), (-1, 1), (-1, -1)):
                nx, ny = x+dx, y+dy
                if not self.valid(nx, ny, flying):
                    continue
                # Ground paths cannot cut through blocked corners.
                if dx and dy and not (self.valid(x+dx, y, flying) and self.valid(x, y+dy, flying)):
                    continue
                step = hypot(dx, dy) * (1 + float(self.grids[flying][ny, nx]) / 10)
                score = cost[current] + step
                neighbor = nx, ny
                if score < cost.get(neighbor, float("inf")):
                    cost[neighbor], parent[neighbor] = score, current
                    heappush(queue, (score + hypot(goal[0]-nx, goal[1]-ny), neighbor))
        self.paths[key] = ()
        return ()
