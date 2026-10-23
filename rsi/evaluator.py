"""Predeclared local predictions plus a fixed, conservative parent/candidate gate."""
import json
from math import sqrt
from statistics import NormalDist, mean

from .analyzer import EVENT_KINDS, metric_statistics, statistics
from .diagnosis.investigate import validate_effect


def specialist_job(diagnosis, diff, max_checks):
    """Request optional supplementary checks before any candidate outcomes are available."""
    return dict(role="evaluation_plan", prompt="""Return ONLY a JSON array of supplementary checks for this patch.
No games have been run. Do not change acceptance thresholds or the original expectations.
Use only existing Analyzer metrics/events; no shell or arbitrary code execution is available.
Each check: {kind:metric/event,name,statistic:mean/min/max/first/last/count/first_time/duration,
time:[start,end] seconds,direction:increase/decrease/stable,role:improvement/regression,
where:{optional event detail keys such as type/facility/reason}}. Return [] if unnecessary.
These checks explain effects; the original fixed gate alone decides promotion.
""" + json.dumps(dict(proposal=diagnosis["conclusion"], diff=diff[:20000], max_checks=max_checks), ensure_ascii=False))


def parse_specialist(value, maximum):
    """Validate optional pre-run checks; never let a model replace the fixed evaluation plan."""
    if value.startswith("```"):
        value = "\n".join(value.splitlines()[1:-1])
    checks = json.loads(value)
    if not isinstance(checks, list) or len(checks) > maximum:
        raise ValueError("Invalid specialist check list")
    for check in checks:
        validate_effect(check)
    return checks


def freeze_plan(config, diagnosis, supplementary=None):
    """Freeze configurations and executable predictions before evaluation matches run."""
    return dict(rules=config, expected_effect=diagnosis["conclusion"]["expected_effect"],
                supplementary=supplementary or [],
                note="Supplementary checks are explanatory; promotion rules remain fixed.")


def effect_value(report, effect):
    """Calculate one declared statistic; insufficient temporal coverage yields null."""
    window = effect["time"]
    coverage = report["coverage"]
    if coverage["integrity"] != "complete" or coverage["duration_seconds"] + 2 < window[1]:
        return None
    if effect["kind"] == "metric":
        if effect["statistic"] not in metric_statistics(effect["name"]):
            return None
        rows = [r for r in report["metrics"].get(effect["name"], []) if window[0] <= r["time"] <= window[1]]
        if not rows and effect["statistic"] == "count" and report["metrics"].get(effect["name"]):
            return 0
        return statistics(rows).get(effect["statistic"]) if rows else None
    if effect["name"] not in EVENT_KINDS:
        return None
    events = [e for e in report["events"] if e["kind"] == effect["name"]
              and e["start"] <= window[1] and e["end"] >= window[0]
              and all(e["details"].get(k) == v for k, v in effect.get("where", {}).items())]
    if effect["statistic"] == "count":
        return len(events)
    if effect["statistic"] == "duration":
        return sum(max(0, min(window[1], e["end"]) - max(window[0], e["start"])) for e in events)
    return min((e["start"] for e in events), default=None)


def check_effects(parent, candidate, effects):
    """Compare declared effects over paired runs; retain missing observations and per-run values."""
    output = []
    for effect in effects:
        p = [effect_value(r, effect) for r in parent["runs"].values()]
        c = [effect_value(r, effect) for r in candidate["runs"].values()]
        delta = mean(c) - mean(p) if p and len(p) == len(c) and all(v is not None for v in p + c) else None
        if delta is None:
            result = "missing"
        elif effect["direction"] == "stable":
            result = "met" if delta == 0 else "not_met"
        else:
            signed = delta if effect["direction"] == "increase" else -delta
            result = "met" if (signed > 0 if effect["role"] == "improvement" else signed >= 0) else "not_met"
        output.append(dict(effect=effect, parent=p, candidate=c, difference=delta, result=result))
    return output


def wilson(successes, total, alpha):
    """Compute a binomial Wilson interval without assuming identical paired trajectories."""
    z = NormalDist().inv_cdf(1 - alpha / 2)
    p, divisor = successes / total, 1 + z * z / total
    center = (p + z * z / (2 * total)) / divisor
    radius = z * sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / divisor
    return max(0, center - radius), min(1, center + radius)


def evaluate(parent, candidate, plan):
    """Decide accept/reject/inconclusive using only the frozen plan and complete fixed data."""
    rules = plan["rules"]
    p, c = list(parent["runs"].values()), list(candidate["runs"].values())
    effects = check_effects(parent, candidate, plan["expected_effect"])
    report = dict(decision="inconclusive", reason="Insufficient fixed evidence", expected_effects=effects,
                  supplementary=check_effects(parent, candidate, plan["supplementary"]),
                  parent_batch=parent["batch_id"], candidate_batch=candidate["batch_id"])
    if any(r["outcome"].get("execution_status") == "bot_error" for r in c):
        return dict(report, decision="reject", reason="Candidate Bot execution failed")
    if len(p) != len(c) or len(p) != len(rules["matches"]) or not p:
        return dict(report, reason="Incomplete fixed match set")
    if any(a["conditions"] != b["conditions"] for a, b in zip(p, c)):
        return dict(report, reason="Parent/candidate conditions are not paired")
    if any(a["environment"] != b["environment"] for a, b in zip(p, c)):
        return dict(report, reason="Map data, environment or game-time limit changed between versions")
    for run in p + c:
        if run["outcome"].get("execution_status") != "normal" or run["coverage"]["integrity"] != "complete":
            return dict(report, reason="Execution or log integrity is incomplete")
        if (run["coverage"]["sample_ratio"] or 0) < rules["min_coverage"]:
            return dict(report, reason="Sampling coverage is below the fixed minimum")
        if rules["time_limit_score"] == "exclude" and run["outcome"]["termination"] == "time_limit":
            return dict(report, reason="Excluded time-limit run leaves the fixed set incomplete")
    n = len(p)
    wins_p = sum(r["outcome"].get("outcome") == "win" for r in p)
    wins_c = sum(r["outcome"].get("outcome") == "win" for r in c)
    pl, pu = wilson(wins_p, n, rules["alpha"] / 2)
    cl, cu = wilson(wins_c, n, rules["alpha"] / 2)
    report["comparison"] = dict(n_per_version=n, parent_win_fraction=wins_p / n,
        candidate_win_fraction=wins_c / n, difference=(wins_c - wins_p) / n,
        difference_interval=[cl - pu, cu - pl], method="Bonferroni-adjusted Wilson difference; conservative approximation",
        draw_policy="nonwin", time_limit_policy=rules["time_limit_score"])
    if not rules["formal"] or n < rules["min_samples"]:
        return dict(report, reason="Functional comparison or fewer than the predeclared minimum samples")
    if cu - pl < 0:
        return dict(report, decision="reject", reason="Fixed comparison indicates overall regression")
    if any(e["result"] != "met" for e in effects):
        return dict(report, reason="A declared effect is missing or unconfirmed")
    if cl - pu > rules["min_improvement"]:
        return dict(report, decision="accept", reason="Overall improvement and declared effects satisfy the fixed gate")
    return report
