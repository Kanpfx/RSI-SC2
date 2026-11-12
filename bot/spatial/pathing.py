"""Lazy visible-threat grids and bounded A* paths for ground and air units."""
from heapq import heappop, heappush
from math import hypot

import numpy as np
from sc2.position import Point2


class Pathing:
    """Route ground and air units over lazily refreshed walkability and threats."""

    def __init__(self, game, map_data, refresh=16):
        """Cache threats and paths for refresh game loops."""
        self.game, self.map, self.refresh = game, map_data, refresh
        self.updated = -refresh
        self.grids = {}
        self.paths = {}

    def update(self):
        """Refresh walkability and visible threats when due, then clear cached paths."""
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
                left, right = max(0, int(x - reach)), min(shape[1], int(x + reach) + 2)
                bottom, top = max(0, int(y - reach)), min(shape[0], int(y + reach) + 2)
                yy, xx = np.ogrid[bottom:top, left:right]
                d = np.hypot(xx - x, yy - y)
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
        """Return local threat, or infinite cost for an invalid cell."""
        self.update()
        x, y = map(int, position)
        return float(self.grids[flying][y, x]) if self.valid(x, y, flying) else float("inf")

    def _neighbors(self, point, flying):
        x, y = point
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                       (1, 1), (1, -1), (-1, 1), (-1, -1)):
            nx, ny = x + dx, y + dy
            # Diagonal movement must also clear both adjacent cardinal cells.
            if self.valid(nx, ny, flying) and (not (dx and dy) or
                    self.valid(x+dx, y, flying) and self.valid(x, y+dy, flying)):
                yield nx, ny

    def safe_point(self, position, *, flying=False, radius=8, towards=None):
        """Choose a connected cell with minimum threat in the local search radius.

        Break ties toward the supplied destination, or toward the current position.
        Return None when the origin is invalid.
        """
        self.update()
        x, y = map(int, position)
        if not self.valid(x, y, flying):
            return None
        reachable, pending = {(x, y)}, [(x, y)]
        while pending:
            for point in self._neighbors(pending.pop(), flying):
                if point not in reachable and hypot(point[0]-x, point[1]-y) <= radius:
                    reachable.add(point)
                    pending.append(point)
        points = (Point2((px + 0.5, py + 0.5)) for px, py in reachable)
        return min(points, key=lambda p: (float(self.grids[flying][int(p.y), int(p.x)]),
                   p.distance_to(towards if towards is not None else position), p.x, p.y), default=None)

    def path(self, start, target, *, flying=False, max_nodes=12000):
        """Return a bounded A* route using distance plus visible threat costs.

        Blocked goals use the nearest walkable ring within three cells. Return ()
        for invalid or unreachable routes, or when the node budget is exhausted.
        """
        self.update()
        origin, goal = tuple(map(int, start)), tuple(map(int, target))
        key = origin, goal, flying, max_nodes
        if key in self.paths:
            return self.paths[key]
        if not self.valid(*origin, flying) or not self.valid(*goal, True):
            return ()
        goals, radius = {goal}, 0
        if not self.valid(*goal, flying):
            for radius in range(1, 4):
                goals = {(x, y) for y in range(goal[1]-radius, goal[1]+radius+1)
                         for x in range(goal[0]-radius, goal[0]+radius+1)
                         if hypot(x-goal[0], y-goal[1]) <= radius and self.valid(x, y, flying)}
                if goals:
                    break
            if not goals:
                return ()
        queue, cost, parent = [(0, origin)], {origin: 0}, {}
        visited = set()
        while queue and len(visited) < max_nodes:
            _, current = heappop(queue)
            if current in visited:
                continue
            if current in goals:
                route = [current]
                while route[-1] != origin:
                    route.append(parent[route[-1]])
                result = tuple(Point2((x + 0.5, y + 0.5)) for x, y in reversed(route))
                self.paths[key] = result
                return result
            visited.add(current)
            for neighbor in self._neighbors(current, flying):
                nx, ny = neighbor
                step = hypot(nx-current[0], ny-current[1]) * (1 + float(self.grids[flying][ny, nx]) / 10)
                score = cost[current] + step
                if score < cost.get(neighbor, float("inf")):
                    cost[neighbor], parent[neighbor] = score, current
                    heappush(queue, (score + max(0, hypot(goal[0]-nx, goal[1]-ny)-radius), neighbor))
        self.paths[key] = ()
        return ()
