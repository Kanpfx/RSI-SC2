from sc2.ids.unit_typeid import UnitTypeId

from bot.modules.strategy import strategy


def control(bot):
    # TODO: Use threats and army composition to choose defense, attack or retreat,
    # and coordinate the required unit abilities.
    marines = bot.units(UnitTypeId.MARINE)
    if marines.amount >= strategy.attack_threshold:
        for marine in marines.idle:
            marine.attack(bot.enemy_start_locations[0])
