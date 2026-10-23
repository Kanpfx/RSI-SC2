"""One issue, competing hypotheses and free-form evidence in a diagnosis session."""
import ast

from ..retriever import Retriever, symbols
from ..tools.versions import editable, project_path, read_json, write_json


class Investigation:
    def __init__(self, job, directory):
        """Load fixed evidence inputs; the conversation owns its research judgments."""
        self.job, self.directory = job, directory
        self.options = job["diagnosis"]
        self.retriever = Retriever(job["parent"], job["context"], self.options["max_chars"],
            analyses=[read_json(project_path(path)) for path in job["analyses"]], max_items=self.options["max_items"])
        self.state = dict(issue=None, hypotheses=[], queries=[], conclusion=None)

    def update(self, issue=None, hypotheses=None, reason=""):
        """Record the problem and source descriptions; archive hypothesis revisions."""
        if self.state["conclusion"] or self.state.get("reason"):
            raise ValueError("Investigation has already concluded")
        if issue is not None:
            if not issue.get("problem") or not issue.get("evidence"):
                raise ValueError("Issue requires problem and evidence descriptions")
            if self.state["queries"] and self.state["issue"] and issue["problem"] != self.state["issue"]["problem"]:
                raise ValueError("Problem is fixed after the first query")
            self.state["issue"] = issue
        if hypotheses:
            existing = {h["id"]: h for h in self.state["hypotheses"]}
            if existing and not reason:
                raise ValueError("Hypothesis revisions require a reason")
            ids = {h["id"] for h in hypotheses}
            minimum = 2 if self.options["competing_hypotheses"] else 1
            if len(existing.keys() | ids) < minimum or len(ids) != len(hypotheses):
                raise ValueError("Insufficient or duplicate hypotheses")
            for h in hypotheses:
                if not all(h.get(key) for key in ("id", "explanation")) or not all(
                        h.get("predictions", {}).get(key) for key in ("support", "refute")):
                    raise ValueError("Hypotheses require id, explanation and support/refutation predictions")
            for h in hypotheses:
                previous = existing.get(h["id"])
                revised = dict(h)
                if previous:
                    revised["revisions"] = previous.get("revisions", []) + [dict(reason=reason,
                        previous={k: v for k, v in previous.items() if k != "revisions"})]
                elif existing:
                    revised["introduced_reason"] = reason
                existing[h["id"]] = revised
            self.state["hypotheses"] = list(existing.values())
        self.save()
        return dict(status="ok", hypothesis_ids=[h["id"] for h in self.state["hypotheses"]])

    def query(self, tool, **arguments):
        """Apply one shared budget and record every log, reference or memory query."""
        if self.state["conclusion"] or self.state.get("reason"):
            raise ValueError("Investigation has already concluded")
        if not self.state["issue"] or not self.state["hypotheses"]:
            raise ValueError("Select an issue and predictions first")
        if self.options["query_policy"] == "fixed":
            raise ValueError("Fixed policy uses only the predeclared evidence pack")
        if tool == "query_logs" and not arguments.get("purpose"):
            raise ValueError("Explain what the query should distinguish or clarify")
        if len(self.state["queries"]) >= self.options["max_queries"]:
            return dict(items=[], source_refs=[], coverage={}, truncated=False, missing_reason="query_budget_exhausted")
        try:
            if tool == "query_logs":
                params = arguments["params"] = dict(arguments.get("params", {}))
                if "batch_id" not in params:
                    if len(self.job["batches"]) != 1:
                        raise ValueError("Specify batch_id when multiple batches are available")
                    params["batch_id"] = self.job["batches"][0]["batch_id"]
                result = self.retriever.query_logs(arguments["kind"], **params)
            elif tool == "query_memory" and not self.options["use_memory"]:
                raise ValueError("Memory disabled for this experiment")
            else:
                result = getattr(self.retriever, tool)(**arguments)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            result = dict(status="invalid_query", error=dict(code="invalid_query", message=str(exc)),
                          items=[], source_refs=[], coverage={}, truncated=False, missing_reason=None)
        else:
            result["status"] = "ok" if result["items"] else "empty"
        self.state["queries"].append(dict(tool=tool, arguments=arguments, result=result))
        self.save()
        return result

    def conclude(self, conclusion=None, reason=""):
        """Hand off a concrete change or end without a proposal; judgments remain textual."""
        if self.state["conclusion"] or self.state.get("reason"):
            raise ValueError("Investigation has already concluded")
        if conclusion is None:
            self.state["reason"] = reason or "No actionable change proposed"
        else:
            if not self.state["issue"] or not self.state["hypotheses"]:
                raise ValueError("Missing investigation")
            if not all(conclusion.get(key) for key in ("change", "preserve", "verification", "code_targets")):
                raise ValueError("Conclusion requires change, preserve, verification and code_targets")
            validate_targets(conclusion["code_targets"], self.job["parent"])
            self.state["conclusion"] = conclusion
        self.save()
        return {key: self.state[key] for key in ("conclusion", "reason") if key in self.state}

    def fixed_pack(self):
        """Build a hypothesis-independent query schedule for the fixed-policy ablation."""
        schedule = []
        for batch in self.job["batches"]:
            for kind, params in (("overview", {}),
                                 ("metrics", {"names": ["minerals", "vespene", "supply_left", "workers"]}),
                                 ("events", {})):
                schedule.append(("query_logs", dict(kind=kind, purpose="Predeclared fixed schedule",
                                                   params=dict(batch_id=batch["batch_id"], **params))))
        for path in ("components/tactics/bc_rush.py", "components/tasks/economy.py", "components/tasks/tech.py"):
            schedule.append(("read_reference", dict(path="source/" + path)))
        for tool, arguments in schedule[:self.options["max_queries"]]:
            result = (self.retriever.query_logs(arguments["kind"], **arguments["params"]) if tool == "query_logs"
                      else self.retriever.read_reference(**arguments))
            self.state["queries"].append(dict(tool=tool, arguments=arguments, result=result))
        self.save()
        return self.state["queries"]

    def save(self):
        """Persist the investigation after successful state transitions."""
        write_json(self.directory / "investigation.json", self.state)


def validate_targets(targets, parent):
    """Resolve file/module targets and optional symbols within the editable snapshot."""
    for target in targets:
        path, separator, symbol = target.partition("::")
        if path.startswith("bot."):
            path = path.replace(".", "/") + ".py"
        source = project_path(parent["source_root"]) / path
        if not editable(path) or not source.is_file():
            raise ValueError(f"Code target exceeds editable scope or does not exist: {target}")
        if separator and not any(name == symbol for name, _ in symbols(ast.parse(source.read_text(encoding="utf-8")))):
            raise ValueError(f"Code target symbol unavailable: {target}")
