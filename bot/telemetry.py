"""Minimal per-game telemetry: tick, minerals and vespene gas."""
import json
from pathlib import Path

FILENAME = "telemetry.json"
INTERVAL = 10
FLUSH_EVERY = 500


class Logger:
    """Sample resources every INTERVAL ticks and write them as JSON next to the game."""

    def __init__(self, path=None, interval=INTERVAL):
        self.path = Path(path or FILENAME)
        self.interval = interval
        self.samples = []
        self.events = []

    def record(self, iteration, bot):
        if iteration % self.interval:
            return
        state = snapshot(bot)
        self.samples.append([iteration, state["minerals"], state["vespene"]])
        # Flush periodically so a crashing game still leaves the samples collected so far.
        if len(self.samples) % FLUSH_EVERY == 0:
            self.save()

    def event(self, time, kind, **fields):
        self.events.append({**fields, "time": time, "type": kind})

    def save(self):
        data = {"interval": self.interval, "samples": self.samples}
        if self.events:
            data["events"] = self.events
        self.path.write_text(json.dumps(data), encoding="utf-8")


def snapshot(bot):
    # TODO: Add enemy, economy or army observations needed to diagnose current behavior.
    return {
        "minerals": bot.minerals,
        "vespene": bot.vespene,
        "supply": {"used": bot.supply_used, "cap": bot.supply_cap},
        "workers": bot.workers.amount,
        "army": bot.supply_army,
        "game_time": bot.time,
    }
