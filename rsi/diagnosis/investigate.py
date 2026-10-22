"""Evidence bookkeeping and research controls for a single diagnosis session."""
from ..analyzer import Analyzer, EVENT_KINDS, metric_statistics
from ..tools.retriever import Retriever
from ..tools.versions import editable, write_json


class Investigation:
    def __init__(self, job, directory):
        """Restore deterministic query inputs; no secondary model or agent is created."""
        self.job, self.directory = job, directory
        self.options = job["diagnosis"]
        self.analyzer = Analyzer(job["project"], job["analysis"], self.options["max_items"], self.options["max_chars"])
        # Use the frozen knowledge for all numeric analysis as well as model retrieval.
        self.analyzer.catalog = Analyzer(job["context"], job["analysis"]).catalog
        for batch in job["batches"]:
            self.analyzer.add_batch(batch)
        self.retriever = Retriever(job["parent"], job["context"], self.options["max_chars"])
        self.state = dict(issue=None, hypotheses=[], queries=[], conclusion=None)
        self.issued_refs = set()
        self.calls = 0

    def update(self, issue=None, hypotheses=None):
        """Record issue selection and competing predictions before any investigation query."""
        if self.calls and self.state["issue"] is not None:
            raise ValueError("Issue and hypotheses are fixed after the first query")
        if issue is not None:
            if not all(issue.get(k) for k in ("symptom", "runs", "window", "why_selected")):
                raise ValueError("Issue requires symptom, runs, window and why_selected")
            allowed = {rid for batch in self.job["batches"] for rid in batch["runs"]}
            if not set(issue["runs"]) <= allowed:
                raise ValueError("Issue refers to unavailable runs")
            self.state["issue"] = issue
        if hypotheses is not None:
            minimum = 2 if self.options["competing_hypotheses"] else 1
            if len(hypotheses) < minimum or len({h["id"] for h in hypotheses}) != len(hypotheses):
                raise ValueError("Insufficient or duplicate hypotheses")
            for h in hypotheses:
                if not all(h.get(k) for k in ("id", "explanation", "predictions")):
                    raise ValueError("Hypotheses require id, explanation and support/refutation predictions")
                h.update(support_refs=[], against_refs=[], unknowns=[], status="open")
            self.state["hypotheses"] = hypotheses
        self.save()
        return {"issue": self.state["issue"], "hypotheses": self.state["hypotheses"]}

    def query(self, channel, kind, params, purpose, distinguishes, updates=None):
        """Apply prior evidence updates, then execute one purposeful bounded query."""
        if self.state["conclusion"]:
            raise ValueError("Investigation has already concluded")
        if not self.state["issue"] or not self.state["hypotheses"]:
            raise ValueError("Select an issue and predictions first")
        if self.options["query_policy"] == "fixed":
            raise ValueError("Fixed policy uses only the predeclared evidence pack")
        self.apply_updates(updates or [])
        if self.state["queries"] and not self.state["queries"][-1].get("updated"):
            raise ValueError("Update hypotheses from the previous result before querying again")
        if not purpose or not distinguishes:
            raise ValueError("Explain query purpose and predicted differences")
        if self.calls >= self.options["max_queries"]:
            return dict(items=[], source_refs=[], coverage={}, truncated=False, missing_reason="query_budget_exhausted")
        self.calls += 1
        try:
            if channel == "analyzer":
                result = self.analyzer.query(kind, **params)
            elif channel == "retriever":
                if kind == "memory" and not self.options["use_memory"]:
                    raise ValueError("Memory disabled for this experiment")
                result = self.retriever.query(kind, **params)
            else:
                raise ValueError("channel must be analyzer or retriever")
        except (ValueError, KeyError, TypeError, OSError) as exc:
            result = dict(status="invalid_query", error=dict(code="invalid_query", message=str(exc)),
                          items=[], source_refs=[], coverage={}, truncated=False, missing_reason=None)
        else:
            result["status"] = "ok" if result["items"] else "empty"
        self.issued_refs.update(result["source_refs"])
        self.state["queries"].append(dict(channel=channel, kind=kind, filters=params, purpose=purpose,
                                         distinguishes=distinguishes, result=result,
                                         updated=result["status"] == "invalid_query"))
        self.save()
        return result

    def apply_updates(self, updates):
        """Accumulate evidence and unknowns; retain a short reason for each update."""
        hypotheses = {h["id"]: h for h in self.state["hypotheses"]}
        for update in updates:
            h = hypotheses[update["id"]]
            cited = set(update.get("support_refs", []) + update.get("against_refs", []))
            if not cited <= self.issued_refs:
                raise ValueError("Hypothesis update cites evidence that was not returned")
            if not update.get("reason"):
                raise ValueError("Hypothesis updates require a short reason")
            for key in ("support_refs", "against_refs", "unknowns"):
                if key in update:
                    h[key] = list(dict.fromkeys(h[key] + update[key]))
            if "status" in update:
                h["status"] = update["status"]
            h.setdefault("updates", []).append(update)
        if updates and self.state["queries"]:
            self.state["queries"][-1]["update"] = updates
            self.state["queries"][-1]["updated"] = True

    def conclude(self, conclusion, updates=None):
        """Validate source/scope and measurable expectations, without certifying causality."""
        if self.state["conclusion"]:
            raise ValueError("Investigation has already concluded")
        self.apply_updates(updates or [])
        if conclusion.get("status") not in {"repair_ready", "unresolved"}:
            raise ValueError("Conclusion status must be repair_ready or unresolved")
        if conclusion["status"] == "repair_ready":
            if not self.state["issue"] or not self.state["hypotheses"]:
                raise ValueError("Missing investigation")
            if conclusion.get("hypothesis_id") not in {h["id"] for h in self.state["hypotheses"]}:
                raise ValueError("Choose an existing hypothesis_id")
            if not self.state["queries"] or not self.state["queries"][-1].get("updated"):
                raise ValueError("Review returned evidence before proposing a change")
            if not conclusion.get("evidence_refs") or not set(conclusion["evidence_refs"]) <= self.issued_refs:
                raise ValueError("Conclusion requires returned evidence")
            if not conclusion.get("code_targets") or not all(editable(p) for p in conclusion["code_targets"]):
                raise ValueError("Code targets exceed editable scope")
            effects = conclusion.get("expected_effect", [])
            if not effects or not {"regression", "improvement"} <= {e.get("role") for e in effects}:
                raise ValueError("Expected effects must include improvement and regression checks")
            from ..evaluator import effect_value
            runs = [r for b in self.analyzer.batches.values() for rid, r in b["runs"].items()
                    if rid in self.state["issue"]["runs"]]
            for effect in effects:
                validate_effect(effect)
                if not any(effect_value(run, effect) is not None for run in runs):
                    raise ValueError(f"Effect {effect['name']} has no usable observations in its window")
            if not all(conclusion.get(key) for key in ("change", "preserve", "uncertainty", "risks")):
                raise ValueError("Describe the change, preserved behavior, risks and remaining uncertainty")
        self.state["conclusion"] = conclusion
        self.save()
        return {"conclusion": conclusion, "hypotheses": self.state["hypotheses"]}

    def fixed_pack(self):
        """Build a hypothesis-independent query schedule for the fixed-policy ablation."""
        schedule = []
        for batch in self.job["batches"]:
            for kind, params in (("overview", {}),
                                 ("metrics", {"names": ["minerals", "vespene", "supply_left", "workers"]}),
                                 ("events", {})):
                schedule.append(("analyzer", kind, dict(batch_id=batch["batch_id"], **params)))
        for path in ("components/tactics/bc_rush.py", "components/tasks/economy.py", "components/tasks/tech.py"):
            schedule.append(("retriever", "source", dict(path=path)))
        for channel, kind, params in schedule[:self.options["max_queries"]]:
            if channel == "analyzer":
                result = self.analyzer.query(kind, **params)
            else:
                result = self.retriever.query(kind, **params)
            self.calls += 1
            self.issued_refs.update(result["source_refs"])
            self.state["queries"].append(dict(channel=channel, kind=kind, filters=params,
                                             purpose="Predeclared fixed schedule", distinguishes="not adaptive",
                                             result=result, updated=False))
        self.save()
        return self.state["queries"]

    def save(self):
        """Persist the inspectable investigation after each successful state transition."""
        write_json(self.directory / "investigation.json", self.state)


def validate_effect(effect):
    """Require an executable metric/event expectation and an explicit time window."""
    if effect.get("kind") not in {"metric", "event"} or not effect.get("name"):
        raise ValueError("Effect requires kind=metric/event and name")
    if effect["kind"] == "event" and effect["name"] not in EVENT_KINDS:
        raise ValueError("Unsupported effect event name")
    if effect.get("statistic") not in {"mean", "min", "max", "first", "last", "count", "first_time", "duration"}:
        raise ValueError("Unsupported effect statistic")
    if effect["kind"] == "event" and effect["statistic"] not in {"count", "first_time", "duration"}:
        raise ValueError("Event effects support count, first_time and duration")
    if effect["kind"] == "metric" and effect["statistic"] == "duration":
        raise ValueError("Metric effects do not support duration")
    if effect["kind"] == "metric" and effect["statistic"] not in metric_statistics(effect["name"]):
        raise ValueError(f"Unsupported statistic for {effect['name']}; allowed: {metric_statistics(effect['name'])}")
    window = effect.get("time", [])
    if len(window) != 2 or not all(isinstance(t, (int, float)) for t in window) or not 0 <= window[0] < window[1]:
        raise ValueError("Effect time must be [start, end] in seconds")
    if effect.get("direction") not in {"increase", "decrease", "stable"}:
        raise ValueError("Effect direction must be increase/decrease/stable")
    if effect.get("role") not in {"improvement", "regression"}:
        raise ValueError("Effect role must be improvement/regression")
    if not isinstance(effect.get("where", {}), dict):
        raise ValueError("Effect where must be a dictionary of event details")
