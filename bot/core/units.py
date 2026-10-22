"""Strategic ownership is separate from temporary construction occupancy."""


class UnitControl:
    def __init__(self):
        """Create independent strategic-owner, role and temporary-occupancy tables."""
        self.owners = {}
        self.roles = {}
        self.occupied = {}

    def update(self, game):
        """Remove entries for units no longer present in native own observations."""
        living = {u.tag for u in game.units + game.structures}
        for table in (self.owners, self.roles, self.occupied):
            for tag in table.keys() - living:
                del table[tag]

    def assign(self, tags, owner, role=""):
        """Explicitly set strategic owner and role for tags without changing temporary occupancy."""
        for tag in tags:
            self.owners[tag], self.roles[tag] = owner, role

    def owned(self, game, owner):
        """Return living native units and structures assigned to the requested owner."""
        return tuple(u for u in game.units + game.structures if self.owners.get(u.tag) == owner)

    def workers(self, game, owner="economy"):
        """Return the owner's workers available for local use, excluding occupied builders."""
        return tuple(w for w in game.workers if self.owners.get(w.tag) == owner
                     and w.tag not in self.occupied and not w.is_constructing_scv)

    def occupy(self, tag, request_id):
        """Bind a free worker to an accepted construction request; reject double occupancy."""
        if tag in self.occupied:
            raise ValueError(f"Unit {tag} is already occupied")
        self.occupied[tag] = request_id

    def release(self, request_id):
        """Release temporary occupancy for a finished request while preserving strategic ownership."""
        for tag, identity in list(self.occupied.items()):
            if identity == request_id:
                del self.occupied[tag]
