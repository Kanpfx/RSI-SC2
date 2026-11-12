"""Join a game created by local-play-bootstrap or a ladder manager."""

import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4


if not __package__:
    directory = Path(__file__).resolve().parent
    sys.path.insert(0, str(directory.parent))
    __package__ = directory.name


async def join_game(args, bot):
    from aiohttp import ClientSession
    from sc2.data import Race
    from sc2.main import play_from_websocket
    from sc2.player import Bot
    from sc2.portconfig import Portconfig

    start = args.StartPort
    ports = Portconfig(server_ports=[start + 2, start + 3],
                       player_ports=[[start + 4, start + 5]])
    url = f"ws://{args.LadderServer}:{args.GamePort}/sc2api"
    async with ClientSession() as session:
        async with session.ws_connect(url, timeout=120) as websocket:
            return await play_from_websocket(websocket, Bot(Race.Terran, bot),
                args.RealTime, ports, should_close=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--LadderServer", required=True)
    parser.add_argument("--GamePort", type=int, required=True)
    parser.add_argument("--StartPort", type=int, required=True)
    parser.add_argument("--OpponentId")
    parser.add_argument("--RealTime", action="store_true")
    parser.add_argument("--output", type=Path, help="Telemetry root; each game uses a unique subdirectory")
    args, _ = parser.parse_known_args()

    from .entry import GAME_STEP, ResearchBot
    from .telemetry.logger import Logger

    output = args.output / uuid4().hex if args.output else None
    logger = Logger(output, frame_step=GAME_STEP)
    bot = ResearchBot(logger=logger)
    bot.opponent_id = args.OpponentId
    if output is not None:
        print(f"Output: {output.resolve()}")
    try:
        result = asyncio.run(join_game(args, bot))
    finally:
        logger.close()
    summary = dict(bot.summary(), **logger.actual)
    summary["result"] = str(result)
    print(json.dumps(summary, indent=2))
    return 1 if result is None or bot.runtime_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
