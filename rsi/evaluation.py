"""Win-rate evaluation and per-style frontier selection, without gameplay heuristics."""
import json
from collections import Counter, defaultdict

from runtime.matches.runner import CONDITION_KEYS


def conditions(config):
    """Expand the fixed evaluation suite in style and trial order."""
    base = dict(config["matches"], opponent="builtin")
    styles = config["evaluation_styles"]
    count = len(styles) * config["trials"]
    return [dict(base, ai_build=styles[i % len(styles)], trial=i // len(styles) + 1)
            for i in range(count)]


def sampling_conditions(config, request, used=()):
    """Replay conditions for new fields or add repetitions beyond the fixed suite."""
    mode, style = request["mode"], request["ai_build"]
    styles = config["evaluation_styles"]
    if mode not in {"reobserve", "repeat"} or style is not None and style not in styles:
        raise ValueError("Sampling requires a valid mode and configured AI build")
    styles = [style] if style is not None else styles
    seen = {(c["ai_build"], c["trial"]) for c in used}
    trial = 1 if mode == "reobserve" else config["trials"] + 1
    matches = []
    while len(matches) < request["matches"]:
        for style in styles:
            if (style, trial) not in seen:
                matches.append(dict(config["matches"], opponent="builtin", ai_build=style, trial=trial))
                if len(matches) == request["matches"]:
                    break
        trial += 1
    return matches


def _key(condition):
    return json.dumps({k: condition[k] for k in CONDITION_KEYS if k in condition}, sort_keys=True)


def batch_problem(batch):
    """Reject failed or incomplete executions without imposing gameplay metrics."""
    runs = list(batch["runs"].values())
    if not runs or len({_key(r["conditions"]) for r in runs}) != len(runs):
        return dict(code="incomplete_match_set", reason="Missing or duplicate match conditions")
    for run in runs:
        outcome = run["outcome"]
        if outcome.get("execution_status") == "bot_error":
            return dict(code="candidate_bot_error", reason="Bot execution failed")
        if outcome.get("execution_status") != "normal" or run["coverage"]["integrity"] != "complete":
            return dict(code="incomplete_execution", reason="Execution or log integrity is incomplete")
        if outcome.get("outcome") not in {"win", "loss", "draw"}:
            return dict(code="missing_outcome", reason="Match outcome is unavailable")
    return None


def score_batch(batch, expected):
    """Require every configured repetition and return one win rate per style."""
    problem = batch_problem(batch)
    if problem:
        raise ValueError(problem["reason"])
    runs = sorted(batch["runs"].values(), key=lambda r: _key(r["conditions"]))
    if sorted(_key(r["conditions"]) for r in runs) != sorted(_key(c) for c in expected):
        raise ValueError("Evaluation batch does not match the fixed opponent suite")
    games, wins = Counter(), Counter()
    for run in runs:
        style = run["conditions"]["ai_build"]
        games[style] += 1
        wins[style] += (run["outcome"]["outcome"] == "win"
                        and run["outcome"].get("termination") != "time_limit")
    return dict(scores={style: wins[style] / games[style] for style in sorted(games)},
                environment=[dict(conditions=r["conditions"], environment=r["environment"]) for r in runs])


def frontier(archive):
    """Count per-style bests, keeping the deeper representative of identical vectors."""
    nodes = archive["nodes"]
    if not nodes:
        return {}
    equivalent = defaultdict(list)
    styles = sorted(next(iter(nodes.values()))["scores"])
    for version, node in nodes.items():
        equivalent[tuple(node["scores"][s] for s in styles)].append(version)
    representatives = [max(group, key=lambda v: (nodes[v]["depth"], v))
                       for group in equivalent.values()]
    best = {s: max(nodes[v]["scores"][s] for v in representatives) for s in styles}
    return {v: count for v in sorted(representatives)
            if (count := sum(nodes[v]["scores"][s] == best[s] for s in styles))}


def select_parent(archive, rng):
    """Sample proportionally to the number of styles where a version is best."""
    weights = frontier(archive)
    if not weights:
        raise ValueError("No evaluated version is available")
    return rng.choices(list(weights), weights=list(weights.values()), k=1)[0]
