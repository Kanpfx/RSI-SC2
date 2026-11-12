"""Load and validate the fixed configuration for one RSI iteration."""
from runtime.matches.runner import validate_match

from runtime.workspace.storage import read_json
from rsi.evaluation import conditions

ROLES = ("discovery", "hypotheses", "investigation", "resolution")


def load_config(path):
    """Read the experiment configuration without hidden command-line overrides."""
    return read_json(path)


def role_settings(model, role=None):
    """Merge per-session defaults and role overrides without resolving environment defaults."""
    shared = {k: v for k, v in model.items() if k not in ROLES}
    return dict(shared, **model.get(role, {})) if role else shared


def validate_config(config):
    """Reject invalid budgets and research controls before starting external work."""
    for key in ("max_matches", "max_wall_seconds", "trials", "match_parallelism", "max_analyses", "resolution_max_tasks"):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    if not isinstance(config["max_repairs"], int) or config["max_repairs"] < 0:
        raise ValueError("max_repairs must be nonnegative")
    if type(config["max_research_steps"]) is not int or config["max_research_steps"] < 4:
        raise ValueError("max_research_steps must cover the four research stages")
    if not isinstance(config["use_memory"], bool):
        raise ValueError("use_memory must be bool")
    model = config["model"]
    for role in (None, *ROLES):
        settings = role_settings(model, role)
        for key in ("max_total_tokens", "max_cost", "max_iterations", "max_output_tokens", "request_timeout", "wall_timeout"):
            if settings[key] <= 0:
                raise ValueError(f"model.{role + '.' if role else ''}{key} must be positive")
    styles = config["evaluation_styles"]
    if not isinstance(styles, list) or not styles or any(not isinstance(s, str) for s in styles) or len(set(styles)) != len(styles):
        raise ValueError("evaluation_styles must be a nonempty list of distinct AI builds")
    if type(config["selection_seed"]) is not int:
        raise ValueError("selection_seed must be an integer")
    suite = conditions(config)
    for match in suite:
        validate_match(match)
    required = len(suite) * (1 if config.get("parent_batch") else 2)
    if config["max_matches"] < required:
        raise ValueError("max_matches must cover the parent and at least one candidate evaluation")
