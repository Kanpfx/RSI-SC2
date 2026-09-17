attack_threshold = 12
supply_buffer = 4


def attack_target(bot, army_size):
    # TODO: Choose development, defense or attack goals from current threats.
    if army_size >= attack_threshold:
        return bot.enemy_start_locations[0]
    return None
