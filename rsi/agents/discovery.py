"""Discover observed problems and return a structured research result."""
from pydantic import TypeAdapter

from rsi.agents.common import (RESEARCH_RULES, SAMPLING_RULES, METRIC_FIELDS,
                               Stage, evidence_tools, prepare_stage)
from rsi.contracts import ProblemResult, DiscoveryStop, validate_metrics


PROMPT = """You are Discovery, responsible for finding a concrete, consequential problem
in the current rule-based SC2 Bot. Analyze its games and collect missing observations
when needed. Finish with an evidence-backed problem and its impact, or explain why
the evidence is insufficient. Establish the problem before proposing causal explanations.
"""

RULES = RESEARCH_RULES + SAMPLING_RULES + """
Compare conditions and outcomes rather than treating a large number alone as a problem.
Complete at least one successful analysis before submitting a problem.
"""

OUTPUT = """
Success format:
{"action":"problem","problem":"observed problem","conditions":"when it occurs",
 "evidence":[{"source":"specific location","observation":"observed fact or comparison"}],
 "impact":"observed consequence",
 "metrics":[{"name":"metric","definition":"calculation","inputs":["table.field"],"units":"unit"}]}
Include uncertainty only for a specific limitation. When stopping, you may retain
useful evidence and metric definitions.
""" + METRIC_FIELDS

class Discovery(Stage):
    result_type = TypeAdapter(ProblemResult | DiscoveryStop)

    def validate(self, value):
        """Check metric definitions and require a successful recorded analysis."""
        validate_metrics(value.get("metrics", []))
        if value["action"] != "problem":
            return
        if not any(a["returncode"] == 0 for a in self.state["analyses"]):
            raise ValueError("Analyze observed evidence before submitting a problem")


def prepare_session(job, directory, prompt):
    return prepare_stage(Discovery, job, directory, prompt, RULES, OUTPUT)


def create_tools(job, state, discovery):
    return evidence_tools(job, discovery, observations=True, conversation_state=state)
