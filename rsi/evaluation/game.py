"""Fixed single-game worker, launched by absolute path with Python -I."""
import argparse
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rsi.evolution.state import read_json, save_json


def play(worktree, config):
    from loguru import logger
    from sc2 import maps
    from sc2.bot_ai import BotAI
    from sc2.data import AIBuild, Difficulty, Race, Result
    from sc2.main import run_game
    from sc2.player import Bot, Computer

    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    errors = []
    sink = logger.add(lambda message: errors.append(str(message)), level="ERROR")
    try:
        sys.path.insert(0, str(Path(worktree).resolve()))
        from bot.main import SeedBot

        if not issubclass(SeedBot, BotAI):
            raise TypeError("bot.main.SeedBot must subclass BotAI")
        evaluation = config["evaluation"]
        result = run_game(maps.get(evaluation["map"]), [
            Bot(Race.Terran, SeedBot()),
            Computer(Race.Terran, Difficulty.CheatInsane, ai_build=AIBuild.RandomBuild),
        ], realtime=False)
        if errors:
            raise RuntimeError("\n".join(errors)[-8000:])
        names = {Result.Victory: "win", Result.Defeat: "loss", Result.Tie: "tie"}
        if result not in names:
            raise RuntimeError(f"Unexpected SC2 result: {result}")
        return {"result": names[result], "crashed": False, "error": None}
    finally:
        logger.remove(sink)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    start = time.monotonic()
    try:
        record = play(args.worktree, read_json(Path(args.config)))
    except (Exception, SystemExit):
        record = {"result": "crash", "crashed": True, "error": traceback.format_exc()}
    record["duration"] = time.monotonic() - start
    save_json(Path(args.output), record)


if __name__ == "__main__":
    main()
