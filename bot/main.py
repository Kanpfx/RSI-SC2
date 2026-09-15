from sc2.bot_ai import BotAI

from bot.modules.combat import control
from bot.modules.economy import manage
from bot.modules.logger import Logger
from bot.modules.observation import snapshot


class SeedBot(BotAI):
    def __init__(self):
        super().__init__()
        self.telemetry = Logger()

    async def on_step(self, iteration: int):
        self.last_snapshot = snapshot(self)
        self.telemetry.record(iteration, self)
        await manage(self)
        control(self)

    async def on_end(self, game_result):
        self.telemetry.save()
