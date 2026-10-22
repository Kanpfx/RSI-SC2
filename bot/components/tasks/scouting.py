from ..behaviors.movement.path_to_target import PathToTarget


class ScoutingTask:
    def __init__(self):
        """Keep the route capability and completion flag; Tactics owns scout allocation."""
        self.move = PathToTarget()
        self.finished = False

    async def execute(self, frame, *, tags, target, return_target, return_after=4032):
        """Route assigned workers to target, then home when hurt or past return_after.

        return_after uses game loops. Set finished on return; Tactics releases ownership
        on its next call. Return the movement result, or False without active movement.
        """
        workers = [w for w in frame.game.workers if w.tag in tags]
        if not workers:
            return False
        returning = frame.game.state.game_loop >= return_after or any(w.health < 20 for w in workers)
        if returning and all(w.distance_to(return_target) < 8 for w in workers):
            self.finished = True
            return False
        return await frame.run(self.move, tags=tags, target=return_target if returning else target)
