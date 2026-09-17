from sc2.ids.unit_typeid import UnitTypeId

from bot import strategy


def control(bot):
    # TODO: Coordinate targeting, movement and abilities for the chosen army.
    marines = bot.units(UnitTypeId.MARINE)
    target = strategy.attack_target(bot, marines.amount)
    if target is not None:
        for marine in marines.idle:
            marine.attack(target)
