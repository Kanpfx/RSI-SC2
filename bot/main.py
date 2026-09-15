from sc2.bot_ai import BotAI

from bot.modules.combat.army_controller import control
from bot.modules.economy.macro_manager import manage
from bot.modules.observation.state_snapshot import snapshot


class SeedBot(BotAI):
    async def on_step(self, iteration: int):
        self.last_snapshot = snapshot(self)
        await manage(self)
        control(self)
