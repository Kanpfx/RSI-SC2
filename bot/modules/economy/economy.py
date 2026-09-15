from sc2.ids.unit_typeid import UnitTypeId as U

from bot.modules.strategy import supply_buffer


async def manage(bot):
    await bot.distribute_workers()
    if not bot.townhalls:
        return
    near = bot.townhalls.first.position.towards(bot.game_info.map_center, 8)
    if (bot.supply_left < supply_buffer and bot.supply_cap < 200
            and not bot.already_pending(U.SUPPLYDEPOT) and bot.can_afford(U.SUPPLYDEPOT)):
        await bot.build(U.SUPPLYDEPOT, near=near)
    if (bot.structures(U.SUPPLYDEPOT).ready and not bot.structures(U.BARRACKS)
            and not bot.already_pending(U.BARRACKS) and bot.can_afford(U.BARRACKS)):
        await bot.build(U.BARRACKS, near=near)
    for base in bot.townhalls.ready.idle:
        if bot.can_afford(U.SCV):
            base.train(U.SCV)
    for barracks in bot.structures(U.BARRACKS).ready.idle:
        if bot.can_afford(U.MARINE):
            barracks.train(U.MARINE)
