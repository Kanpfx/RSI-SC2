"""Console progress lines: ``HH:MM:SS [Stage node] message``, greppable by node id."""
import os
from datetime import datetime

SYMBOLS = {"win": "W", "loss": "L", "tie": "T", "crash": "X"}


def log(stage, message=""):
    print(f"{datetime.now():%H:%M:%S} [{stage}] {message}".rstrip(), flush=True)


def verbose(stage, message=""):
    """Per-step and per-game detail, hidden unless RSI_VERBOSE=1."""
    if os.environ.get("RSI_VERBOSE") == "1":
        log(stage, message)


def blank():
    """Separate one finished node from the next."""
    print(flush=True)


def results(records):
    """Per-game outcomes in game order, e.g. ``WWLXL``."""
    return "".join(SYMBOLS.get(record["result"], "?") for record in records)


def score(counts):
    """Win/loss counts, with ties and crashes appended only when present."""
    parts = [f"{counts['wins']}W", f"{counts['losses']}L"]
    for key, symbol in (("ties", "T"), ("crashes", "X")):
        if counts[key]:
            parts.append(f"{counts[key]}{symbol}")
    return "-".join(parts)


def elapsed(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"
    return f"{int(seconds) // 3600}h{int(seconds) % 3600 // 60:02d}m"
