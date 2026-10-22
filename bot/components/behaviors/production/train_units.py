from ..helpers import count, subjects


class TrainUnits:
    async def execute(self, frame, *, product, producers, target, save_if_unaffordable=False):
        """Request product from explicit producer tags until the total target is covered.

        Count pending production, check nonresource ability availability, and propose
        demands even when unaffordable. Return True if any request is produced;
        only Actions may approve spending or reserve resources.
        """
        remaining = max(0, target - count(frame, product))
        handled = False
        ability = frame.actions.ability_for(product)
        for producer in subjects(frame.game, producers):
            if not remaining:
                break
            if not producer.is_ready or not producer.is_idle:
                continue
            if ability not in await frame.actions.available(producer):
                continue
            frame.actions.propose(producer.tag, ability, product=product,
                                  save_if_unaffordable=save_if_unaffordable)
            remaining -= 1
            handled = True
        return handled
