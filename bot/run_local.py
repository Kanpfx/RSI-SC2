"""Run a local game against the built-in AI: python -B -m bot.run_local."""

import argparse
import json
from pathlib import Path
from uuid import uuid4


def main():
    from sc2 import maps
    from sc2.data import AIBuild, Difficulty, Race
    from sc2.main import run_game
    from sc2.player import Bot, Computer

    from .entry import GAME_STEP, ResearchBot
    from .telemetry.logger import Logger

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", default="AbyssalReefLE")
    parser.add_argument("--race", choices=("Terran", "Zerg", "Protoss"), default="Zerg")
    parser.add_argument("--difficulty", choices=tuple(Difficulty.__members__), default="Easy")
    parser.add_argument("--ai-build", choices=tuple(AIBuild.__members__), default="RandomBuild")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--time-limit", type=int, default=150)
    parser.add_argument("--realtime", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-log", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = (args.output or root / "runtime/bot" / uuid4().hex).resolve()
    map_settings = maps.Map(Path(args.map)) if Path(args.map).is_file() else maps.get(args.map)
    output.mkdir(parents=True, exist_ok=True)
    logger = Logger(None if args.no_log else output, frame_step=GAME_STEP)
    bot = ResearchBot(logger=logger)
    print(f"Output: {output}")
    try:
        result = run_game(map_settings,
            [Bot(Race.Terran, bot), Computer(Race[args.race], Difficulty[args.difficulty], AIBuild[args.ai_build])],
            realtime=args.realtime, random_seed=args.seed, game_time_limit=args.time_limit,
            save_replay_as=str(output / "match.SC2Replay"))
    finally:
        logger.close()
    summary = dict(bot.summary(), **logger.actual)
    summary["result"] = str(result)
    text = json.dumps(summary, indent=2)
    (output / "result.json").write_text(text + "\n", encoding="utf-8")
    print(text)
    return 1 if bot.runtime_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
