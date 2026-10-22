"""Prepare one diagnosis conversation; issue selection and investigation share its context."""
import json
from ..tools.retriever import Retriever

PROMPT = """Diagnose ONE actionable inefficiency in a rule-based SC2 Bot. You may select winning games too.
Use select_issue to record a time-bounded phenomenon and competing explanations with support/refutation predictions.
Choose each investigate query by: distinguishes explanations, locates editable code, then cost.
After each valid result, update hypotheses with a short reason before the next query.
support_refs/against_refs and unknowns accumulate; status replaces the current status.
Invalid queries may be corrected directly and are not evidence.
Do not confuse invocation False, a proposal, submission and observed success. Missing/sampled data is not zero.
Analyzer owns match facts; Retriever owns frozen code/API/knowledge/memory. Source paths are relative to bot/.
Do not diagnose causes from event names alone. Do not edit, run games, or change the framework/rules.
Only bot/components/{behaviors,tasks,tactics}/ Python code is editable. Native commands belong to core.
Call conclude with a narrow change OR unresolved when explanations remain indistinguishable, gains are weak,
or the budget is exhausted. Cite only returned references. Preserve uncertainty about competing explanations.
expected_effect is a list of executable metric/event checks, each with a time window, statistic, direction,
and role=improvement/regression. Include both roles. Metric names come from overview.metric_names.
State samples support mean/min/max/first/last; counters support count/first_time; events support count/first_time/duration.
Analyzer defaults to summaries; use detail=raw only for required samples or trace snapshots.
Choose hypothesis_id and describe change, preserve behavior, risks and remaining uncertainty.
The proposal describes target logic, not a complete patch. Include necessary API/knowledge evidence for Editor,
which can read Bot source but cannot query references. Keep tool outputs and reasoning concise.
Leave model budget for Editor. Do not invent evidence or claim that source checks establish causality.
"""


def make_job(project, parent, batches, overview, context, config):
    """Assemble serializable inputs, ablation switches and names-only reference indexes."""
    options = config["diagnosis"]
    prompt = PROMPT
    if not options["competing_hypotheses"]:
        prompt = prompt.replace("competing explanations", "one explanation")
        prompt += "\nAblation: one hypothesis is sufficient; do not require competing hypotheses."
    if options["query_policy"] == "fixed":
        prompt += "\nAblation: use only the predeclared evidence pack; investigate is disabled. Select issue, then conclude with updates."
    return dict(role="diagnosis", project=str(project), parent=parent, batches=batches, context=str(context),
                diagnosis=options, analysis=config["analysis"],
                prompt=prompt + "\n" + json.dumps(dict(overview=overview,
                    references=Retriever(parent, context).index(), research_controls=options), ensure_ascii=False))
