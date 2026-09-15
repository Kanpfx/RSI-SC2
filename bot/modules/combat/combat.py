from sc2.ids.unit_typeid import UnitTypeId

from bot.modules.strategy import attack_threshold


def control(bot):
    marines = bot.units(UnitTypeId.MARINE)
    if marines.amount >= attack_threshold:
        for marine in marines.idle:
            marine.attack(bot.enemy_start_locations[0])
