import inspect


def test_bot_contract():
    from sc2.bot_ai import BotAI
    from bot.main import SeedBot

    assert issubclass(SeedBot, BotAI)
    assert inspect.iscoroutinefunction(SeedBot.on_step)
    assert isinstance(SeedBot(), BotAI)
