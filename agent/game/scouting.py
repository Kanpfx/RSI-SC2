"""One worker loops through enemy starts and expansion locations."""
from __future__ import annotations

from ares.consts import UnitRole


class ScoutController:
    def __init__(self) -> None:
        self.tag: int | None = None
        self.route = []
        self.index = 0
        self.next_update = 60.0
        self.target_since = 0.0

    def run(self, bot) -> None:
        if bot.time < self.next_update:
            return
        self.next_update = bot.time + 1.0
        scout = bot.workers.find_by_tag(self.tag) if self.tag is not None else None
        if self.tag is not None and scout is None:
            self.tag = None
            self.next_update = bot.time + 10.0
            return
        if not self.route:
            self.route = list(bot.enemy_start_locations)
            remaining = [point for point in bot.expansion_locations_list
                         if point not in self.route and point.distance_to(bot.start_location) > 8]
            while remaining:
                origin = self.route[-1] if self.route else bot.start_location
                point = min(remaining, key=lambda location: location.distance_to(origin))
                self.route.append(point)
                remaining.remove(point)
        if not self.route:
            return
        if scout is None:
            if len(bot.workers) < 12:
                return
            workers = bot.mediator.get_units_from_role(role=UnitRole.GATHERING)
            candidates = [worker for worker in workers
                          if not worker.is_carrying_resource and not worker.is_constructing_scv]
            if not candidates:
                return
            scout = min(candidates, key=lambda worker: worker.distance_to(self.route[self.index]))
            self.tag = scout.tag
            bot.mediator.assign_role(tag=scout.tag, role=UnitRole.MAP_CONTROL)
            self.target_since = bot.time
        if scout.distance_to(self.route[self.index]) < 6 or bot.time - self.target_since >= 10:
            self.index = (self.index + 1) % len(self.route)
            self.target_since = bot.time
        scout.move(self.route[self.index])
