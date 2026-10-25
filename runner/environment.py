"""Shared SC2 environment inputs and game-time units."""
from pathlib import Path

from .artifacts import PROJECT, read_json

LOOPS_PER_SECOND = 22.4


def load_environment(project=PROJECT):
    """Read the project's SC2 installation and expected build without changing process state."""
    return read_json(Path(project) / "environment.json")
