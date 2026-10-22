from ..behaviors.production.train_units import TrainUnits


class ArmyTask:
    def __init__(self):
        """Create the shared training capability; production targets remain explicit call inputs."""
        self.train = TrainUnits()

    async def execute(self, frame, *, goals):
        """Evaluate all production goals without short-circuiting after the first demand.

        Each goal is (product, producer_tags, target, priority, save_if_unaffordable).
        Return the union of child results; Actions resolves competition between goals.
        """
        handled = False
        for product, producer_tags, target, priority, save in goals:
            handled |= await frame.run(self.train, product=product, producers=producer_tags,
                target=target, priority=priority, save_if_unaffordable=save)
        return handled
