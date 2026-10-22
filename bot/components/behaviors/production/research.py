from ..helpers import subjects


class Research:
    async def execute(self, frame, *, upgrade, producers, save_if_unaffordable=False):
        """Request an unfinished upgrade from the first eligible supplied producer.

        Skip completed or pending research. Return True after proposing a request;
        save_if_unaffordable is a caller policy enforced by Actions.
        """
        if frame.game.already_pending_upgrade(upgrade) > 0:
            return False
        ability = frame.actions.ability_for(upgrade)
        for producer in subjects(frame.game, producers):
            if producer.is_ready and producer.is_idle and ability in await frame.actions.available(producer):
                frame.actions.propose(producer.tag, ability, product=upgrade,
                                      save_if_unaffordable=save_if_unaffordable)
                return True
        return False
