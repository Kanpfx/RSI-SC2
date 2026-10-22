"""Run the modular bot independently: python -B -m bot.run."""
import argparse
import json
import os
from pathlib import Path
from uuid import uuid4


def main():
    """Parse standalone match options, configure SC2 and persist artifacts around run_game.

    Always close telemetry and write the Bot summary after a game attempt.
    Return a nonzero status for a recorded Bot runtime error.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", default="AbyssalReefLE")
    parser.add_argument("--race", choices=("Terran", "Zerg", "Protoss"), default="Zerg")
    parser.add_argument("--difficulty", choices=("VeryEasy", "Easy", "Medium", "Hard"), default="Easy")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--time-limit", type=int, default=150)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-log", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "environment.json").read_text(encoding="utf-8"))
    os.environ["SC2PATH"] = config["sc2_path"]
    from sc2 import maps
    from sc2.data import Difficulty, Race
    from sc2.main import run_game
    from sc2.player import Bot, Computer
    from .entry import ResearchBot
    from .telemetry.logger import Logger
    output = args.output or root / "runtime/bot" / uuid4().hex
    output.mkdir(parents=True, exist_ok=True)
    logger = Logger(None if args.no_log else output)
    bot = ResearchBot(logger=logger, sc2_build=config["sc2_build"])
    manifest = dict(map=args.map, race=args.race, difficulty=args.difficulty, seed=args.seed,
                    game_time_limit=args.time_limit, game_step=2, sc2_build=config["sc2_build"])
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2)+"\n", encoding="utf-8")
    try:
        run_game(maps.get(args.map), [Bot(Race.Terran, bot),
            Computer(getattr(Race, args.race), getattr(Difficulty, args.difficulty))],
            realtime=False, random_seed=args.seed, game_time_limit=args.time_limit,
            save_replay_as=str(output / "match.SC2Replay"))
    finally:
        logger.close()
        (output / "result.json").write_text(json.dumps(bot.summary(), indent=2)+"\n", encoding="utf-8")
    print(json.dumps(dict(output=str(output), **bot.summary()), indent=2))
    return 1 if bot.runtime_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
