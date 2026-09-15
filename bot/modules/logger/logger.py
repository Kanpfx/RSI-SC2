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

    def record(self, iteration, bot):
        if iteration % self.interval:
            return
        self.samples.append([iteration, bot.minerals, bot.vespene])
        # Flush periodically so a crashing game still leaves the samples collected so far.
        if len(self.samples) % FLUSH_EVERY == 0:
            self.save()

    def save(self):
        self.path.write_text(json.dumps({"interval": self.interval, "samples": self.samples}),
                             encoding="utf-8")
