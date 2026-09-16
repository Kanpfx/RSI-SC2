def snapshot(bot):
    # TODO: Add enemy, economy or army observations needed by current decisions.
    return {
        "minerals": bot.minerals,
        "supply": {"used": bot.supply_used, "cap": bot.supply_cap},
        "workers": bot.workers.amount,
        "army": bot.supply_army,
        "game_time": bot.time,
    }
