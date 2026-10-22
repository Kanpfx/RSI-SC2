from sc2.ids.unit_typeid import UnitTypeId as U

from ..helpers import subjects


class Addon:
    async def execute(self, frame, *, product, producers, save_if_unaffordable=False):
        """Request the supplied addon product from eligible explicit producer tags.

        Check idle readiness, ability availability and addon space. Return whether
        any request was proposed; Actions owns payment, reservation and confirmation.
        """
        ability, handled = frame.actions.ability_for(product), False
        for producer in subjects(frame.game, producers):
            if not producer.is_ready or not producer.is_idle or producer.add_on_tag:
                continue
            if frame.actions.active_build(product) or ability not in await frame.actions.available(producer):
                continue
            position = producer.add_on_position
            if not await frame.game.can_place_single(U.SUPPLYDEPOT, position):
                continue
            footprint = frame.spatial.placement.footprint(product, position)
            frame.actions.propose(producer.tag, ability, product=product,
                construction=True, footprint=footprint, save_if_unaffordable=save_if_unaffordable)
            handled = True
        return handled
