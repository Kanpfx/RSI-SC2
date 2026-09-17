from sc2.bot_ai import BotAI

from bot.combat import control
from bot.economy import manage
from bot.telemetry import Logger


class SeedBot(BotAI):
    def __init__(self):
        super().__init__()
        self.telemetry = Logger()

    async def on_step(self, iteration: int):
        self.telemetry.record(iteration, self)
        await manage(self)
        control(self)

    async def on_end(self, game_result):
        self.telemetry.save()
