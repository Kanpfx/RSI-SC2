"""Short SC2 environment smoke; this is not the project strategy Bot."""
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from runner.environment import load_environment

CONFIG = load_environment(ROOT)
os.environ["SC2PATH"] = CONFIG["sc2_path"]

from sc2 import maps
from sc2.bot_ai import BotAI
from sc2.data import Difficulty, Race
from sc2.ids.unit_typeid import UnitTypeId
from sc2.main import run_game
from sc2.player import Bot, Computer

RUN = ROOT / "runtime/checks/match" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


class SmokeBot(BotAI):
    def __init__(self):
        super().__init__()
        self.unit_command_uses_self_do = False
        self.steps = 0
        self.submitted = 0
        self.created_workers = 0
        self.started_depots = 0
        self.action_errors = []
        self.build_submitted = False
        self.client_info = {}

    async def on_start(self):
        self.client.game_step = 2
        ping = (await self.client.ping()).ping
        self.client_info = {
            "game_version": ping.game_version,
            "data_version": ping.data_version,
            "data_build": ping.data_build,
            "base_build": ping.base_build,
        }
        if ping.base_build != CONFIG["sc2_build"]:
            raise RuntimeError(f"Unexpected client build: {ping.base_build}")

    async def on_step(self, iteration):
        self.steps += 1
        for error in self.state.action_errors:
            self.action_errors.append({
                "loop": self.state.game_loop,
                "unit_tag": error.unit_tag, "ability_id": error.ability_id,
                "result": error.result,
            })
        if self.state.game_loop % 32 == 0:
            await self.distribute_workers()
        if self.workers.amount < 14:
            for townhall in self.townhalls.idle:
                if self.can_afford(UnitTypeId.SCV):
                    self.submitted += bool(townhall.train(UnitTypeId.SCV))
        if not self.build_submitted and self.time >= 3 and self.can_afford(UnitTypeId.SUPPLYDEPOT):
            position = await self.find_placement(
                UnitTypeId.SUPPLYDEPOT,
                near=self.start_location.towards(self.game_info.map_center, 7),
            )
            if position:
                worker = self.select_build_worker(position)
                if worker:
                    self.build_submitted = bool(worker.build(UnitTypeId.SUPPLYDEPOT, position))
                    self.submitted += self.build_submitted

    async def on_unit_created(self, unit):
        if unit.type_id == UnitTypeId.SCV:
            self.created_workers += 1

    async def on_building_construction_started(self, unit):
        if unit.type_id == UnitTypeId.SUPPLYDEPOT:
            self.started_depots += 1


def main():
    RUN.mkdir(parents=True, exist_ok=False)
    bot = SmokeBot()
    replay = RUN / "smoke.SC2Replay"
    result = run_game(
        maps.get(CONFIG["check_map"]),
        [Bot(Race.Terran, bot), Computer(Race.Zerg, Difficulty.Easy)],
        realtime=False, game_time_limit=60,
        save_replay_as=str(replay),
    )
    report = {
        "purpose": "environment_smoke", "game_time_limit_seconds": 60,
        "result": str(result), "client": bot.client_info,
        "on_step_calls": bot.steps, "submitted_train_build": bot.submitted,
        "created_workers": bot.created_workers, "started_depots": bot.started_depots,
        "action_errors": bot.action_errors,
        "replay": str(replay), "replay_bytes": replay.stat().st_size if replay.exists() else 0,
        "llm_calls": 0,
    }
    report["passed"] = (
        bot.steps > 0 and bot.created_workers > 0 and bot.started_depots > 0
        and not bot.action_errors and report["replay_bytes"] > 0
    )
    (RUN / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit("SC2 smoke failed; inspect the archived result and stderr")


if __name__ == "__main__":
    main()
