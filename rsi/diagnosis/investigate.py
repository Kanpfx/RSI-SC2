"""One issue, competing hypotheses and free-form evidence in a diagnosis session."""
import ast

from ..retriever import Retriever
from ..analyzer import load_batch_analysis
from ..tools.artifacts import project_path, write_json
from ..tools.versions import editable


class Investigation:
    def __init__(self, job, directory):
        """Load fixed evidence inputs; the conversation owns its research judgments."""
        self.job, self.directory = job, directory
        self.options = job["diagnosis"]
        self.retriever = Retriever(job["parent"], job["context"], max_chars=24000,
            analyses=[load_batch_analysis(path) for path in job["analyses"]], max_items=60,
            memory_path=job["memory"])
        self.state = dict(issue=None, hypotheses=[], queries=[], conclusion=None)

    def _ensure_open(self):
        """Reject state changes after a conclusion or explicit abstention."""
        if self.state["conclusion"] or self.state.get("reason"):
            raise ValueError("Investigation has already concluded")

    def update(self, issue=None, hypotheses=None, reason=""):
        """Record the problem and source descriptions; archive hypothesis revisions."""
        self._ensure_open()
        if issue is not None:
            if not issue.get("problem") or not issue.get("evidence"):
                raise ValueError("Issue requires problem and evidence descriptions")
            if self.state["queries"] and self.state["issue"] and issue["problem"] != self.state["issue"]["problem"]:
                raise ValueError("Problem is fixed after the first query")
            self.state["issue"] = issue
        if hypotheses:
            existing = {hypothesis["id"]: hypothesis for hypothesis in self.state["hypotheses"]}
            if existing and not reason:
                raise ValueError("Hypothesis revisions require a reason")
            ids = {hypothesis["id"] for hypothesis in hypotheses}
            minimum = 2 if self.options["competing_hypotheses"] else 1
            if len(existing.keys() | ids) < minimum or len(ids) != len(hypotheses):
                raise ValueError("Insufficient or duplicate hypotheses")
            for hypothesis in hypotheses:
                if not all(hypothesis.get(key) for key in ("id", "explanation")) or not all(
                        hypothesis.get("predictions", {}).get(key) for key in ("support", "refute")):
                    raise ValueError("Hypotheses require id, explanation and support/refutation predictions")
            for hypothesis in hypotheses:
                previous = existing.get(hypothesis["id"])
                revised = dict(hypothesis)
                if previous:
                    revised["revisions"] = previous.get("revisions", []) + [dict(reason=reason,
                        previous={k: v for k, v in previous.items() if k != "revisions"})]
                elif existing:
                    revised["introduced_reason"] = reason
                existing[hypothesis["id"]] = revised
            self.state["hypotheses"] = list(existing.values())
        self.save()
        return dict(issue=self.state["issue"], hypotheses=[
            {key: hypothesis[key] for key in ("id", "explanation", "predictions", "code_targets") if key in hypothesis}
            for hypothesis in self.state["hypotheses"]])

    def query(self, tool, **arguments):
        """Apply one shared budget and record every log, reference or memory query."""
        self._ensure_open()
        if not self.state["issue"] or not self.state["hypotheses"]:
            raise ValueError("Select an issue and predictions first")
        if self.options["query_policy"] == "fixed":
            raise ValueError("Fixed policy uses only the predeclared evidence pack")
        if tool == "query_logs" and not arguments.get("purpose"):
            raise ValueError("Explain what the query should distinguish or clarify")
        if len(self.state["queries"]) >= self.options["max_queries"]:
            return dict(error=dict(code="query_budget_exhausted", message="Shared query budget exhausted; finish with conclude."))
        try:
            if tool == "query_logs":
                params = arguments["params"] = dict(arguments.get("params", {}))
                if "batch_id" not in params:
                    if len(self.job["batches"]) != 1:
                        raise ValueError("Specify batch_id when multiple batches are available")
                    params["batch_id"] = self.job["batches"][0]["batch_id"]
                result = self.retriever.query_logs(arguments["kind"], **params)
            elif tool == "read_reference":
                result = self.retriever.read_reference(**arguments)
            elif tool == "query_memory":
                if not self.options["use_memory"]:
                    raise ValueError("Memory disabled for this experiment")
                result = self.retriever.query_memory(**arguments)
            else:
                raise ValueError(f"Unknown investigation tool: {tool}")
        except (ValueError, KeyError, TypeError, OSError) as exc:
            result = dict(error=dict(code="invalid_query", message=str(exc)))
        self.state["queries"].append(dict(tool=tool, arguments=arguments, result=result))
        self.save()
        return result

    def conclude(self, conclusion=None, reason=""):
        """Hand off a concrete change or end without a proposal; judgments remain textual."""
        self._ensure_open()
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


def symbols(node, prefix=""):
    """Yield qualified Python class/function names without importing candidate code."""
    for child in getattr(node, "body", []):
        if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = prefix + child.name
            yield name, child
            yield from symbols(child, name + ".")
