"""Compare one candidate batch with the retained parent batch."""
import json
from statistics import mean

from .analyzer import metric_summary


MIN_COVERAGE = 0.95
MIN_METRIC_IMPROVEMENT = 0.05
MIN_IMPROVED_FRACTION = 0.6
METRICS = (
    dict(name="combat_value", statistic="last"),
    dict(name="trade_efficiency", statistic="last"),
    dict(name="army_value", statistic="mean"),
    dict(name="army_investment", statistic="last"),
    dict(name="resource_collection_rate", statistic="last"),
)


def _condition_key(run):
    """Use the same condition ordering for validation and metric comparison."""
    return json.dumps(run["conditions"], sort_keys=True)


def evidence_problem(parent, candidate):
    """Require complete outcomes under the same conditions, independent of run order."""
    parent_runs, candidate_runs = list(parent["runs"].values()), list(candidate["runs"].values())
    if not parent_runs or len(parent_runs) != len(candidate_runs):
        return "Incomplete match set"
    if (len({_condition_key(run) for run in parent_runs}) != len(parent_runs)
            or len({_condition_key(run) for run in candidate_runs}) != len(candidate_runs)):
        return "Duplicate match conditions"
    for parent_run, candidate_run in zip(sorted(parent_runs, key=_condition_key),
                                         sorted(candidate_runs, key=_condition_key)):
        if parent_run["conditions"] != candidate_run["conditions"]:
            return "Parent/candidate conditions differ"
        if parent_run["environment"] != candidate_run["environment"]:
            return "Map data, environment or game-time limit changed between versions"
    for runs, allow_bot_error in ((parent_runs, False), (candidate_runs, True)):
        for run in runs:
            if allow_bot_error and run["outcome"].get("execution_status") == "bot_error":
                continue
            if run["outcome"].get("execution_status") != "normal" or run["coverage"]["integrity"] != "complete":
                return "Execution or log integrity is incomplete"
            if run["outcome"].get("outcome") not in {"win", "loss", "draw"}:
                return "Match outcome is unavailable"
            if (run["coverage"]["sample_ratio"] or 0) < MIN_COVERAGE:
                return "Sampling coverage is below the fixed minimum"
    if any(run["outcome"].get("execution_status") == "bot_error" for run in candidate_runs):
        return "Candidate Bot execution failed"
    return None


def evaluate(parent, candidate):
    """Accept a win-rate gain, or enough percentage improvements at equal win rate."""
    report = dict(decision="reject", reason="No configured improvement", comparable=False,
                  rules=dict(min_coverage=MIN_COVERAGE, min_metric_improvement=MIN_METRIC_IMPROVEMENT,
                             min_improved_fraction=MIN_IMPROVED_FRACTION, metrics=METRICS),
                  parent_batch=parent["batch_id"], candidate_batch=candidate["batch_id"])
    problem = evidence_problem(parent, candidate)
    if problem:
        return dict(report, reason=problem)
    report["comparable"] = True
    parent_runs, candidate_runs = [sorted(batch["runs"].values(), key=_condition_key)
                                   for batch in (parent, candidate)]
    shared_ends = [min(parent_run["coverage"]["duration_seconds"], candidate_run["coverage"]["duration_seconds"])
                   for parent_run, candidate_run in zip(parent_runs, candidate_runs)]
    parent_win_rate, candidate_win_rate = [
        mean(run["outcome"]["outcome"] == "win" and run["outcome"].get("termination") != "time_limit"
             for run in runs)
        for runs in (parent_runs, candidate_runs)]
    report["comparison"] = dict(n_per_version=len(parent_runs), parent_win_fraction=parent_win_rate,
                                candidate_win_fraction=candidate_win_rate,
                                difference=candidate_win_rate - parent_win_rate, draw_policy="nonwin")
    metrics = []
    for rule in METRICS:
        values = [[_metric_value(run, rule, end) for run, end in zip(runs, shared_ends)]
                  for runs in (parent_runs, candidate_runs)]
        parent_value, candidate_value = [mean(samples) if all(value is not None for value in samples) else None
                                         for samples in values]
        gain = None if parent_value is None or candidate_value is None else candidate_value - parent_value
        ratio = gain / abs(parent_value) if gain is not None and parent_value else None
        metrics.append(dict(rule=rule, parent=parent_value, candidate=candidate_value, improvement_ratio=ratio,
            improved=gain is not None and gain > 0 and (parent_value == 0 or ratio + 1e-12 >= MIN_METRIC_IMPROVEMENT)))
    report["metrics"] = metrics
    report["improved_fraction"] = sum(m["improved"] for m in metrics) / len(metrics) if metrics else 0
    if candidate_win_rate > parent_win_rate:
        return dict(report, decision="accept", reason="Win rate improved")
    if candidate_win_rate == parent_win_rate and report["improved_fraction"] + 1e-12 >= MIN_IMPROVED_FRACTION:
        return dict(report, decision="accept", reason="Win rate unchanged; configured core metrics improved")
    return report


def _metric_value(run, rule, end):
    """Read a statistic over the shared game-time window, clipping boundary weights."""
    rows = [dict(row, seconds=min(row.get("seconds", 0), end - row["time"]))
            for row in run["metrics"].get(rule["name"], []) if row["time"] <= end]
    return metric_summary(rule["name"], rows).get(rule["statistic"]) if rows else None
