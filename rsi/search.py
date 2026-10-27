"""Small archive and deterministic parent selection for branching Bot experiments."""


def add_node(archive, version, evidence):
    """Index one evaluated code hash without copying its source, logs or analysis."""
    archive["nodes"].setdefault(version["version"], dict(
        source_root=version["source_root"], batch=evidence["batch_path"],
        analysis=evidence["analysis_path"], expansions=0))


def select_parent(archive, champion, round_number):
    """Alternate champion expansion with the least-expanded archived version."""
    if round_number % 2 == 0:
        return champion
    return min(archive["nodes"], key=lambda version: (
        archive["nodes"][version]["expansions"], version != champion, version))
