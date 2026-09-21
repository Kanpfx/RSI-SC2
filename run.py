"""Single-model local-game entry point."""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import TextIO

from loguru import logger
from sc2 import maps
from sc2.data import AIBuild, Difficulty, Race
from sc2.main import run_game
from sc2.player import Bot, Computer

PROJECT_ROOT = Path(__file__).resolve().parent
# Support direct execution even when the standalone repository is renamed.
if __package__ in (None, ""):
    spec = importlib.util.spec_from_file_location("agent", PROJECT_ROOT / "__init__.py")
    package = importlib.util.module_from_spec(spec)
    sys.modules["agent"] = package
    spec.loader.exec_module(package)
ARES_ROOT = PROJECT_ROOT / "ares-sc2"
if ARES_ROOT.is_dir():
    # Prepend, so the vendored clone wins over any pip-installed ares-sc2:
    # a wheel install ships no sc2_helper, and would silently shadow this one.
    for entry in (str(ARES_ROOT / "src"), str(ARES_ROOT)):
        sys.path.insert(0, entry)

from agent.config import load_environment, require_environment
from agent.runtime.bot import WhyBot
from agent.context.builder import available_tactics


class _TeeStream:
    """Write one console stream to its original destination and a UTF-8 log."""

    def __init__(self, stream: TextIO, mirror: TextIO, lock: Lock):
        self._stream = stream
        self._mirror = mirror
        self._lock = lock

    def write(self, text: str) -> int:
        with self._lock:
            written = self._stream.write(text)
            self._mirror.write(text)
            self._mirror.flush()
        return written

    def flush(self) -> None:
        with self._lock:
            self._stream.flush()
            self._mirror.flush()

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


@contextmanager
def mirror_console(path: Path):
    """Mirror stdout and stderr verbatim while preserving the live console."""
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    lock = Lock()
    with path.open("a", encoding="utf-8", buffering=1) as mirror:
        sys.stdout = _TeeStream(original_stdout, mirror, lock)
        sys.stderr = _TeeStream(original_stderr, mirror, lock)
        try:
            yield
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr


def configure_console_logging() -> None:
    """Keep the live game console readable; detailed traces stay in telemetry."""
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        colorize=False,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} {message}",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the LLM-controlled bot.")
    parser.add_argument("--map_name", required=True, help="Installed SC2 map name.")
    parser.add_argument(
        "--difficulty",
        choices=[difficulty.name for difficulty in Difficulty],
        default="Hard",
        help="Built-in opponent difficulty.",
    )
    parser.add_argument(
        "--build_mode",
        choices=[build.name for build in AIBuild],
        default="RandomBuild",
        help="Built-in opponent build style.",
    )
    parser.add_argument(
        "--tactic",
        choices=available_tactics(),
        default="BattleCruiserRush",
        help="Tactic card used by model.",
    )
    parser.add_argument("--player_name", default="model_player")
    parser.add_argument("--own_race", choices=["Terran"], default="Terran")
    parser.add_argument(
        "--enemy_race", choices=["Terran", "Zerg", "Protoss"], default="Terran"
    )
    return parser.parse_args()


def main() -> None:
    os.chdir(PROJECT_ROOT)  # Ares reads config.yml relative to the working directory.
    load_environment()
    args = parse_args()
    require_environment(["LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"])
    own_race = Race[args.own_race]
    enemy_race = Race[args.enemy_race]
    match_log_directory = PROJECT_ROOT / "logs" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    match_log_directory.mkdir(parents=True, exist_ok=False)
    (match_log_directory / "system").mkdir()
    with mirror_console(match_log_directory / "system" / "console.log"):
        configure_console_logging()
        try:
            bot = Bot(
                own_race,
                WhyBot(
                    tactic_name=args.tactic,
                    run_metadata={
                        "map_name": args.map_name,
                        "difficulty": args.difficulty,
                        "build_mode": args.build_mode,
                        "own_race": args.own_race,
                        "enemy_race": args.enemy_race,
                        "realtime": True,
                    },
                    log_directory=match_log_directory,
                ),
                args.player_name,
            )
            opponent = Computer(
                enemy_race,
                Difficulty[args.difficulty],
                ai_build=AIBuild[args.build_mode],
            )
            run_game(
                maps.get(args.map_name),
                [bot, opponent],
                realtime=True,
                save_replay_as=str(match_log_directory / "replay.SC2Replay"),
            )
        finally:
            logger.remove()


if __name__ == "__main__":
    main()
