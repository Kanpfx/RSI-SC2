from sc2.ids.unit_typeid import UnitTypeId as U

from bot import strategy


async def manage(bot):
    await manage_workers(bot)
    if not bot.townhalls:
        return
    await manage_construction(bot)
    await manage_technology(bot)
    train_units(bot)


async def manage_workers(bot):
    await bot.distribute_workers()


async def manage_construction(bot):
    near = bot.townhalls.first.position.towards(bot.game_info.map_center, 8)
    if (bot.supply_left < strategy.supply_buffer and bot.supply_cap < 200
            and not bot.already_pending(U.SUPPLYDEPOT) and bot.can_afford(U.SUPPLYDEPOT)):
        await bot.build(U.SUPPLYDEPOT, near=near)
    if (bot.structures(U.SUPPLYDEPOT).ready and not bot.structures(U.BARRACKS)
            and not bot.already_pending(U.BARRACKS) and bot.can_afford(U.BARRACKS)):
        await bot.build(U.BARRACKS, near=near)


async def manage_technology(bot):
    # TODO: Add prerequisites and upgrades supporting the chosen army.
    pass


def train_units(bot):
    for base in bot.townhalls.ready.idle:
        if bot.can_afford(U.SCV):
            base.train(U.SCV)
    for barracks in bot.structures(U.BARRACKS).ready.idle:
        if bot.can_afford(U.MARINE):
            barracks.train(U.MARINE)
