"""Load and validate the fixed configuration for one RSI iteration."""
from sc2.data import Difficulty

from runner.artifacts import read_json


def load_config(path):
    """Read the experiment configuration without hidden command-line overrides."""
    return read_json(path)


def role_settings(model, role=None):
    """Merge per-session defaults and role overrides without resolving environment defaults."""
    shared = {k: v for k, v in model.items() if k not in {"diagnosis", "editor"}}
    return dict(shared, **model[role]) if role else shared


def validate_config(config):
    """Reject invalid budgets and research controls before starting external work."""
    execution = config.get("editor_execution", {"mode": "local"})
    if execution.get("mode") not in {"local", "docker"}:
        raise ValueError("editor_execution.mode must be local or docker")
    if execution["mode"] == "docker" and not execution.get("image"):
        raise ValueError("editor_execution.image is required for docker")
    for key in ("max_matches", "max_wall_seconds", "parallel_matches"):
        if not isinstance(config[key], int) or config[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    if not isinstance(config["max_repairs"], int) or config["max_repairs"] < 0:
        raise ValueError("max_repairs must be nonnegative")
    diagnosis = config["diagnosis"]
    if diagnosis["query_policy"] not in {"active", "fixed"}:
        raise ValueError("query_policy must be active or fixed")
    for key in ("max_queries", "max_tasks"):
        if not isinstance(diagnosis[key], int) or diagnosis[key] <= 0:
            raise ValueError(f"diagnosis.{key} must be positive")
    for key in ("competing_hypotheses", "use_memory"):
        if not isinstance(diagnosis[key], bool):
            raise ValueError(f"diagnosis.{key} must be bool")
    model = config["model"]
    for role in (None, "diagnosis", "editor"):
        settings = role_settings(model, role)
        for key in ("max_total_tokens", "max_cost", "max_iterations", "max_output_tokens", "request_timeout", "wall_timeout"):
            if settings[key] <= 0:
                raise ValueError(f"model.{role + '.' if role else ''}{key} must be positive")
    required = config["parallel_matches"] * (config["diagnosis"]["max_tasks"] + (0 if config.get("parent_batch") else 1))
    if config["max_matches"] < required:
        raise ValueError("max_matches must cover the initial parent and all configured candidate tasks")
    match = config["matches"]
    if match["game_time_limit"] <= 0 or match["wall_timeout"] <= 0:
        raise ValueError("Matches require positive timeouts")
    if match["race"] not in {"Terran", "Protoss", "Zerg"} or match["difficulty"] not in Difficulty.__members__:
        raise ValueError("Unsupported match opponent")
