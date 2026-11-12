"""Research outputs with meaningful evidence, predictions, tests and interventions."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from runtime.workspace.checks import validate_targets

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

ROLE_INPUTS = {
    "discovery": ("metrics",),
    "hypotheses": ("problem", "metrics"),
    "investigation": ("problem", "hypotheses", "metrics", "tests"),
    "resolution": ("problem", "hypotheses", "diagnosis", "tests"),
}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(Contract):
    source: Text
    observation: Text


class Metric(Contract):
    name: Text
    definition: Text
    inputs: list[Text] = Field(min_length=1)
    units: Text
    window: Text | None = None
    missing: Text | None = None
    comparison: Text | None = None


class Problem(Contract):
    problem: Text
    conditions: Text
    evidence: list[Evidence] = Field(min_length=1)
    impact: Text
    uncertainty: Text | None = None


class Predictions(Contract):
    support: Text
    refute: Text


class Hypothesis(Contract):
    id: Text
    explanation: Text
    evidence: list[Evidence] = Field(min_length=1)
    predictions: Predictions
    verification: Text
    code_targets: list[Text] | None = Field(default=None, min_length=1)


class Test(Contract):
    hypothesis_id: Text
    method: Text
    evidence: list[Evidence] = Field(min_length=1)
    outcome: Literal["supported", "refuted", "inconclusive"]
    interpretation: Text


class Diagnosis(Contract):
    mechanism: Text
    hypothesis_ids: list[Text] = Field(min_length=1)
    expected_change: Text
    uncertainty: Text | None = None


class Task(Contract):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
    hypothesis_ids: list[Text] = Field(min_length=1)
    change: Text
    preserve: Text
    verification: Text
    code_targets: list[Text] = Field(min_length=1)


class Unresolved(Contract):
    action: Literal["unresolved"]
    reason: Text
    evidence: list[Evidence] | None = Field(default=None, min_length=1)


class DiscoveryStop(Unresolved):
    metrics: list[Metric] | None = Field(default=None, min_length=1)


class ProblemResult(Problem):
    action: Literal["problem"]
    metrics: list[Metric] = Field(min_length=1)


class HypothesesResult(Contract):
    action: Literal["hypotheses"]
    hypotheses: list[Hypothesis] = Field(min_length=1)


class InvestigationNotes(Contract):
    hypotheses: list[Hypothesis] | None = Field(default=None, min_length=1)
    metrics: list[Metric] | None = Field(default=None, min_length=1)


class InvestigationStop(InvestigationNotes):
    action: Literal["discovery", "unresolved"]
    reason: Text
    evidence: list[Evidence] | None = Field(default=None, min_length=1)
    tests: list[Test] | None = Field(default=None, min_length=1)


class DiagnosisResult(InvestigationNotes):
    action: Literal["diagnosis"]
    tests: list[Test] = Field(min_length=1)
    diagnosis: Diagnosis


class ResolutionResult(Contract):
    action: Literal["resolution"]
    tasks: list[Task] = Field(min_length=1)


class ReturnToInvestigation(Unresolved):
    action: Literal["investigation"]


def linked_ids(ids, hypotheses):
    """Reject ambiguous or unknown hypothesis references."""
    if not ids or len(set(ids)) != len(ids) or not set(ids) <= {h["id"] for h in hypotheses}:
        raise ValueError("hypothesis_ids must reference distinct existing hypotheses")


def validate_metrics(metrics):
    """Keep metric definitions uniquely addressable by name."""
    if len({m["name"] for m in metrics}) != len(metrics):
        raise ValueError("Metric names must be unique")
    return metrics


def validate_hypotheses(hypotheses, parent):
    """Validate competing explanations and any known editable targets."""
    result = [Hypothesis.model_validate(h).model_dump(exclude_none=True) for h in hypotheses]
    ids = [h["id"] for h in result]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("Provide nonempty hypotheses with unique IDs")
    for hypothesis in result:
        validate_targets(hypothesis.get("code_targets", []), parent)
    return result


def investigation_context(previous, result, parent):
    """Apply explicit research updates and invalidate tests of changed mechanisms."""
    hypotheses = validate_hypotheses(result.get("hypotheses", previous["hypotheses"]), parent)
    current = {h["id"]: h for h in hypotheses}
    changed = {h["id"] for h in previous["hypotheses"] if h["id"] not in current
        or any(h[key] != current[h["id"]][key] for key in ("explanation", "predictions"))}
    old_tests = previous.get("tests", [])
    tests = [t for t in old_tests if t["hypothesis_id"] not in changed]
    for test in result.get("tests", []):
        linked_ids([test["hypothesis_id"]], hypotheses)
        if test["hypothesis_id"] in changed and test in old_tests:
            raise ValueError("Changed mechanisms cannot inherit previous supporting tests")
        tests.append(test)
    if result["action"] == "diagnosis":
        ids = result["diagnosis"]["hypothesis_ids"]
        linked_ids(ids, hypotheses)
        latest = {t["hypothesis_id"]: t["outcome"] for t in tests}
        if any(latest.get(key) != "supported" for key in ids):
            raise ValueError("Diagnosis requires supporting tests for its current hypotheses")
    metrics = {m["name"]: m for m in previous.get("metrics", [])}
    metrics.update({m["name"]: m for m in validate_metrics(result.get("metrics", []))})
    return dict(hypotheses=hypotheses, tests=tests, metrics=list(metrics.values()))
