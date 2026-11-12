"""Accepted construction jobs and their worker/site leases, keyed by goal."""

from dataclasses import dataclass

from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

ADDONS = {U.BARRACKSTECHLAB, U.FACTORYTECHLAB, U.STARPORTTECHLAB,
          U.BARRACKSREACTOR, U.FACTORYREACTOR, U.STARPORTREACTOR}


@dataclass(slots=True)
class Job:
    """Accepted build, its site and the latest evidence of forward progress."""

    request: object
    position: Point2
    builder_type: U
    updated: int  # Last game loop with measurable movement or construction progress.
    distance: float
    progress: float = 0


class Construction:
    """Track accepted build goals and release their worker/site leases on completion or failure."""

    def __init__(self, frame):
        self.frame = frame
        self.jobs = {}

    def active(self, key):
        job = self.jobs.get(key)
        return job.request if job else None

    def position(self, request):
        """Resolve a build site from its actor, target or reserved footprint."""
        if request.product in ADDONS:
            actor = self.frame.units.own.get(request.unit_tag)
            if actor is not None:
                return actor.add_on_position
        if isinstance(request.target, int):
            target = self.frame.actions.target(request.target)
            if target is not None:
                return target.position
        elif request.target is not None:
            return Point2(request.target)
        left, bottom, right, top = request.footprint
        return Point2(((left + right) / 2, (bottom + top) / 2))

    def accept(self, request):
        """Track a locally queued build and reserve its worker and footprint."""
        actor = self.frame.units.own[request.unit_tag]
        position = self.position(request)
        self.jobs[request.source] = Job(request, position, actor.type_id,
            self.frame.game.state.game_loop, actor.distance_to(position))
        if any(w.tag == actor.tag for w in self.frame.game.workers):
            self.frame.units.occupy(actor.tag, request.request_id)
        self.frame.spatial.placement.reserve(request.request_id, request.footprint)

    def release(self, request):
        """Remove the matching job and release its worker and footprint reservations."""
        job = self.jobs.get(request.source)
        if job is not None and job.request is request:
            del self.jobs[request.source]
        self.frame.units.release(request.request_id)
        self.frame.spatial.placement.release(request.request_id)

    def tracked(self, structure):
        """Check whether a structure already belongs to an active build goal."""
        return any(job.request.product == structure.type_id
                   and structure.distance_to(job.position) < 1 for job in self.jobs.values())

    def observe(self, request):
        """Reconcile one accepted job; failures release leases via Actions.finish."""
        frame, game = self.frame, self.frame.game
        job = self.jobs[request.source]
        loop = game.state.game_loop
        age = loop - request.frame
        actor = frame.units.own.get(request.unit_tag)
        if actor is not None:
            distance = actor.distance_to(job.position)
            if distance < job.distance - .5:
                job.distance, job.updated = distance, loop
        building = next((b for b in game.structures if b.type_id == request.product
                         and b.distance_to(job.position) < 1), None)
        if building is not None:
            request.structure_tag = building.tag
            if building.build_progress > job.progress:
                job.progress, job.updated = building.build_progress, loop
            if job.builder_type in {U.PROBE, U.DRONE}:
                # These builds continue independently after the structure appears.
                frame.units.release(request.request_id)
            if building.is_ready:
                placement = frame.spatial.placement
                body = placement.footprint(request.product, job.position)
                if request.footprint[2] > body[2]:
                    # Keep addon space with the structure after releasing the job.
                    placement.retain(building, (body[2], body[1], request.footprint[2], body[3]))
                frame.actions.finish(request, "completed", "construction_complete")
            elif actor is None and job.builder_type not in {U.PROBE, U.DRONE}:
                frame.actions.finish(request, "failed", "builder_died")
            elif (job.builder_type == U.SCV and age > 32 and not actor.is_constructing_scv
                  and not any(o.target == building.tag or isinstance(o.target, (tuple, Point2))
                      and Point2(o.target).distance_to(job.position) < 1 for o in actor.orders)):
                frame.actions.finish(request, "failed", "construction_interrupted")
            elif loop - job.updated > 224:
                frame.actions.finish(request, "failed", "construction_stalled")
            elif request.status != "observed":
                frame.actions.record(request, "observed", "construction_started")
        elif request.structure_tag:
            frame.actions.finish(request, "failed", "structure_destroyed")
        elif actor is None:
            frame.actions.finish(request, "failed", "actor_died")
        elif loop - job.updated > 224:
            frame.actions.finish(request, "failed", "construction_timeout")
