"""Per-frame exclusive groups, separate from accepted construction leases."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Claim:
    """One group's explicit tags or typed unit-count demand."""

    group: str
    priority: int
    tags: tuple
    unit_type: object
    count: int
    near: object
    exclude_carrying: bool


class UnitPool:
    """Allocate exclusive groups per frame while retaining construction worker leases."""

    def __init__(self, game):
        self.game = game
        self.own = {}
        self.groups = {}
        self.leases = {}
        self.claims = {}
        self.records = []
        self.resolved = False
        self._open = False

    def begin(self):
        """Refresh the own-unit index, prune dead tags and reopen unit claims."""
        self.own = {u.tag: u for u in self.game.units + self.game.structures}
        self.groups = {tag: group for tag, group in self.groups.items() if tag in self.own}
        self.leases = {tag: job for tag, job in self.leases.items() if tag in self.own}
        self.claims.clear()
        self.resolved = False
        self._open = True

    def request(self, group, *, tags=None, unit_type=None, count=None,
                priority=0, near=None, exclude_carrying=False):
        """Declare one demand per group, using explicit tags or a typed count."""
        if not self._open or self.resolved:
            raise RuntimeError("Unit claims must precede allocation")
        if not group or group in self.claims:
            raise ValueError("A group must have exactly one declaration per frame")
        if tags is not None:
            if unit_type is not None or count is not None:
                raise ValueError("Use tags or a typed count, not both")
            tags, count = tuple(sorted(set(tags))), 0
        elif unit_type is None or type(count) is not int or count < 0:
            raise ValueError("Count claims require a unit_type and nonnegative integer count")
        self.claims[group] = Claim(group, priority, tags or (), unit_type,
                                   count, near, exclude_carrying)

    def resolve(self):
        """Allocate by descending priority; retain membership when demands permit."""
        if not self._open or self.resolved:
            raise RuntimeError("Units can only be allocated once per frame")
        previous, groups = self.groups, {}
        for claim in sorted(self.claims.values(), key=lambda c: (-c.priority, c.group)):
            # A lease preserves its strategic group even if that group stops claiming.
            eligible = [u for tag, u in self.own.items() if tag not in groups
                        and (tag not in self.leases or previous.get(tag) == claim.group)]
            if claim.unit_type is None:
                chosen = [u.tag for u in eligible if u.tag in claim.tags]
                requested = len(claim.tags)
            else:
                types = (claim.unit_type if isinstance(claim.unit_type, (set, frozenset, tuple))
                         else (claim.unit_type,))
                eligible = [u for u in eligible if u.type_id in types and u.tag not in self.leases
                            and not u.is_constructing_scv
                            and not (claim.exclude_carrying and u.is_carrying_resource)]
                # Preserve group membership before considering proximity and tag order.
                eligible.sort(key=lambda u: (previous.get(u.tag) != claim.group,
                    u.distance_to(claim.near) if claim.near is not None else 0, u.tag))
                chosen, requested = [u.tag for u in eligible[:claim.count]], claim.count
            groups.update(dict.fromkeys(chosen, claim.group))
            self.records.append(dict(frame=self.game.state.game_loop, group=claim.group,
                priority=claim.priority, requested=requested, granted=sorted(chosen)))
        for tag in self.leases:
            if tag in previous:
                groups[tag] = previous[tag]
        self.groups, self.resolved = groups, True

    def tags(self, group):
        if not self.resolved:
            raise RuntimeError("Read allocated groups after resolve()")
        return tuple(sorted(tag for tag, owner in self.groups.items() if owner == group))

    def select(self, group, tags=None):
        """Select living members of a group, optionally restricted to supplied tags."""
        selected = self.tags(group) if tags is None else tags
        return tuple(self.own[tag] for tag in dict.fromkeys(selected)
                     if tag in self.own and self.groups.get(tag) == group)

    def workers(self, group):
        """Return group workers excluding leases and SCVs currently constructing."""
        return tuple(w for w in self.game.workers if self.groups.get(w.tag) == group
                     and w.tag not in self.leases and not w.is_constructing_scv)

    def occupy(self, tag, request_id):
        """Lease a worker to a locally accepted construction request."""
        if tag in self.leases:
            raise RuntimeError("A construction worker already has a lease")
        self.leases[tag] = request_id

    def release(self, request_id):
        """Release worker leases held by a request; repeated releases are harmless."""
        for tag, job in list(self.leases.items()):
            if job == request_id:
                del self.leases[tag]

    def drain(self):
        """Close claims and detach allocation records without clearing group ownership."""
        self._open = False
        records, self.records = self.records, []
        return records
