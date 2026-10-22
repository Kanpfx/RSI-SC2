from sc2.ids.unit_typeid import UnitTypeId as U

from ..production.build_structure import BuildStructure


class AutoSupply(BuildStructure):
    async def execute(self, frame, *, margin=8):
        """Request a depot below the supply margin, or recover an interrupted depot.

        Maintain existing pending construction and stop new requests at 200 supply.
        Reuse BuildStructure state without adding another tracked module call.
        """
        game = frame.game
        unfinished = any(b.type_id == U.SUPPLYDEPOT and not b.is_ready for b in game.structures)
        if unfinished and frame.actions.active_build(U.SUPPLYDEPOT) is None:
            return await super().execute(frame, product=U.SUPPLYDEPOT,
                                         near=frame.spatial.map.anchor(-6))
        if game.supply_cap >= 200 or game.supply_left >= margin:
            return False
        if game.already_pending(U.SUPPLYDEPOT) or frame.actions.active_build(U.SUPPLYDEPOT):
            return True
        return await super().execute(frame, product=U.SUPPLYDEPOT,
                                     near=frame.spatial.map.anchor(-6))
