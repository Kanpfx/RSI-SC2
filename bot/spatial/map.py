"""Cache static map data without copying units or game observations."""
from sc2.position import Point2


class Map:
    """Share spawn-relative anchors, resource locations and read-only grid views."""

    def __init__(self, game):
        """Cache spawn locations, resource points and native grid views."""
        info = game.game_info
        self.main = game.start_location
        self.center = info.map_center
        self.enemy_starts = tuple(game.enemy_start_locations)
        self.expansions = tuple(game.expansion_locations_list)
        self.ramp = game.main_base_ramp.top_center
        self.ramps = tuple(info.map_ramps)
        self.height = self._view(info.terrain_height)
        self.pathable = self._view(info.pathing_grid)
        self.buildable = self._view(info.placement_grid)
        self.playable_area = info.playable_area
        self.resources = tuple(r.position for r in game.mineral_field + game.vespene_geyser)

    @staticmethod
    def _view(grid):
        """Expose a read-only NumPy view using native grid[y, x] indexing."""
        result = grid.data_numpy.view()
        result.flags.writeable = False
        return result

    def anchor(self, offset=0):
        """Return a build-search anchor toward map center with a lateral offset."""
        forward = self.main.towards(self.center, 8)
        dx, dy = forward.x - self.main.x, forward.y - self.main.y
        return Point2((forward.x - dy * offset / 8, forward.y + dx * offset / 8))
