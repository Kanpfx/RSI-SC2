"""Generate competing explanations as a structured result."""
from pydantic import TypeAdapter

from rsi.agents.common import RESEARCH_RULES, HYPOTHESIS_FIELDS, Stage, evidence_tools, prepare_stage
from rsi.contracts import HypothesesResult, Unresolved, validate_hypotheses


PROMPT = """You are Hypotheses, responsible for explaining the discovered problem.
Propose competing causal mechanisms grounded in the supplied observations and relevant
code. Each explanation must make distinguishable predictions and have a practical
verification plan. Submit testable hypotheses, or explain why none can yet be justified.
"""

RULES = RESEARCH_RULES + """
Keep observations separate from explanations; evidence motivating a hypothesis does
not confirm it. Preserve IDs only for unchanged mechanisms. Do not modify Bot code
or evidence, design repairs or claim that untested mechanisms are established.
"""

OUTPUT = """
Success format:
{"action":"hypotheses","hypotheses":[
 {"id":"H1","explanation":"causal mechanism",
  "evidence":[{"source":"specific location","observation":"motivating observation"}],
  "predictions":{"support":"supporting observation","refute":"refuting observation"},
  "verification":"what to measure and compare"}]}
""" + HYPOTHESIS_FIELDS

class Hypotheses(Stage):
    result_type = TypeAdapter(HypothesesResult | Unresolved)

    def validate(self, value):
        """Check hypothesis identity and editable source references."""
        if value["action"] == "hypotheses":
            value["hypotheses"] = validate_hypotheses(value["hypotheses"], self.job["parent"])


def prepare_session(job, directory, prompt):
    return prepare_stage(Hypotheses, job, directory, prompt, RULES, OUTPUT)


def create_tools(job, state, hypotheses):
    return evidence_tools(job, hypotheses, conversation_state=state)
