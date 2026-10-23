"""Prepare one diagnosis conversation; issue selection and investigation share its context."""
import json
from ..retriever import Retriever, make_tool, reference_tool
from ..tools.versions import path_reference

PROMPT = """Diagnose ONE actionable inefficiency in a rule-based SC2 Bot. You may select winning games too.
Use select_issue to record issue={problem,evidence} and competing hypotheses.
Evidence is a list of natural-language source descriptions: runs, metrics and time ranges, events,
source locations or reference entries. Be specific enough to check; no citation-ID format is required.
Each hypothesis has id, explanation, predictions={support,refute}, code_targets.
Explain the causal mechanism and what observations would support or weaken it.
Use query_logs to distinguish explanations, read_reference to inspect code/API/knowledge,
and query_memory to consult prior experience. Collect related evidence
freely, then revise hypotheses with select_issue and a short reason when your understanding changes.
Keep the problem fixed after querying; update its evidence list as needed. Revisions are archived automatically.
Log queries need a short purpose and default to the only available batch, all its runs and all times;
set batch_id for multiple batches and use runs/time explicitly to narrow scope. Times are game seconds.
Reference paths start with source/ (the frozen bot/ tree), api/ or knowledge/; copy paths from the index.
Read text by lines=[start,end] (1-based, at most 120 lines per read); JSON files use key with dot-separated keys.
Omit key to list JSON entry names. These tools are read-only and share one query budget.
Do not confuse invocation False, a proposal, submission and observed success. Missing data is not zero.
Do not diagnose causes from event names alone. Preserve uncertainty in your reasoning.
Do not edit, run games, or change the framework/rules. Only bot/components/{behaviors,tasks,tactics}/
Python code is editable; native commands belong to core. Code targets use project-relative file paths or
Python module names, optionally followed by ::Class.method for an exact symbol.
Conclude with {change,preserve,verification,code_targets}: a narrow modification idea, behavior to retain,
natural-language advice for judging results, and concrete editable code locations. Do not write a full patch.
Verification can suggest checks and observations without executable metric definitions or acceptance thresholds.
When evidence cannot distinguish explanations, gains are weak, or budget is exhausted, call conclude with
reason only and no conclusion. Do not force a modification to complete the workflow.
Retriever defaults to summaries; detail=raw retrieves samples or trace snapshots, offset retrieves pages.
Editor receives the issue, hypotheses, conclusion and complete query records for selective evidence checks.
Keep tool outputs and reasoning concise and leave model budget for Editor.
"""


def make_job(project, parent, batches, overview, context, config, *, analysis_paths):
    """Assemble serializable inputs, ablation switches and names-only reference indexes."""
    options = config["diagnosis"]
    prompt = PROMPT
    if not options["competing_hypotheses"]:
        prompt = prompt.replace("competing hypotheses", "one hypothesis")
        prompt += "\nAblation: one hypothesis is sufficient; do not require competing hypotheses."
    if options["query_policy"] == "fixed":
        prompt += "\nAblation: use only the predeclared evidence pack; all three query tools are disabled. Select issue, then conclude."
    return dict(role="diagnosis", project=path_reference(project), parent=parent, batches=batches, context=path_reference(context),
                diagnosis=options, analyses=analysis_paths,
                prompt=prompt + "\n" + json.dumps(dict(overview=overview,
                    references=Retriever(parent, context).index(), research_controls=options), ensure_ascii=False))


def investigation_tools(investigation):
    """Expose ordinary investigation functions as tools within one SDK conversation."""
    from functools import partial
    from typing import Any, Literal
    from pydantic import BaseModel, ConfigDict, Field
    from openhands.sdk.tool import Action

    class Issue(BaseModel):
        model_config = ConfigDict(extra="forbid")
        problem: str
        evidence: list[str]

    class Predictions(BaseModel):
        model_config = ConfigDict(extra="forbid")
        support: str
        refute: str

    class Hypothesis(BaseModel):
        model_config = ConfigDict(extra="forbid")
        id: str
        explanation: str
        predictions: Predictions
        code_targets: list[str] = Field(default_factory=list)

    class Conclusion(BaseModel):
        model_config = ConfigDict(extra="forbid")
        change: str
        preserve: str
        verification: str
        code_targets: list[str]

    class SelectInput(Action):
        model_config = ConfigDict(extra="forbid")
        issue: Issue | None = None
        hypotheses: list[Hypothesis] = Field(default_factory=list)
        reason: str = ""

    class LogInput(Action):
        model_config = ConfigDict(extra="forbid")
        query_kind: Literal["overview", "metrics", "events", "trace", "compare"] = Field(alias="kind")
        params: dict[str, Any] = Field(default_factory=dict)
        purpose: str

    class MemoryInput(Action):
        model_config = ConfigDict(extra="forbid")
        text: str = ""

    class ConcludeInput(Action):
        model_config = ConfigDict(extra="forbid")
        conclusion: Conclusion | None = None
        reason: str = ""

    descriptions = {
        "select_issue": "Select issue={problem,evidence} and hypotheses={id,explanation,predictions:{support,refute},code_targets}. Evidence is a list of natural-language source descriptions. The problem is fixed after querying, but evidence can be refined. Later submit new/revised hypotheses plus reason; omitted hypotheses remain and previous versions are archived. Code targets use project-relative paths or Python modules, optionally ::Class.method.",
        "query_logs": "Query analyzed logs; purpose states what to distinguish or clarify. Kinds: overview,metrics,events,trace,compare. params: batch_id (defaults to the only available batch), runs (list or all), time=[start,end] game seconds. Defaults: all runs/times, detail=summary. metrics: names; events: types; trace: module/call_id/request_id/status; compare: reference_batch,names. detail=raw adds metric samples or trace state snapshots; offset retrieves the next page. Related queries may run consecutively.",
        "query_memory": "Read the experiences frozen for this investigation; text filters by case-insensitive substring. Omit text to read available experiences. Unavailable when the memory experiment switch is off. Shares the log/reference query budget.",
        "conclude": "Submit conclusion={change,preserve,verification,code_targets}. Verification is natural-language guidance, not executable metric checks. Code targets must identify existing editable Python files/modules, optionally ::Class.method. If no actionable change is justified, omit conclusion and give reason. Do not invent evidence or assert that source inspection proves causality.",
    }
    functions = dict(select_issue=investigation.update, conclude=investigation.conclude,
        **{name: partial(investigation.query, name) for name in ("query_logs", "query_memory")})
    inputs = dict(select_issue=SelectInput, query_logs=LogInput, query_memory=MemoryInput, conclude=ConcludeInput)
    tools = [make_tool(name, description, functions[name], inputs[name]) for name, description in descriptions.items()]
    tools.insert(2, reference_tool(partial(investigation.query, "read_reference")))
    return tools
