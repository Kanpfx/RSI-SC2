"""Native map data, lazy threat paths and reserved building positions."""
from .map import Map
from .pathing import Pathing
from .placement import Placement


class Spatial:
    def __init__(self, game):
        """Compose map, pathing and placement services around the same native game instance."""
        self.map = Map(game)
        self.pathing = Pathing(game, self.map)
        self.placement = Placement(game, self.map)
