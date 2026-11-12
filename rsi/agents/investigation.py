"""Report executed tests, supported mechanisms and explicit research revisions."""
from pydantic import TypeAdapter

from rsi.agents.common import (RESEARCH_RULES, SAMPLING_RULES, METRIC_FIELDS,
                               HYPOTHESIS_FIELDS, Stage, evidence_tools, prepare_stage)
from rsi.contracts import DiagnosisResult, InvestigationStop


PROMPT = """You are Investigation, responsible for testing and refining explanations of
the supplied problem. Run checks that distinguish competing predictions, collect more
observations when needed and interpret the results. Finish with a supported mechanism
and its predicted response to intervention, or identify the evidence gap or invalid
problem definition that prevents a conclusion.
"""

RULES = RESEARCH_RULES + SAMPLING_RULES + """
Judge each hypothesis separately. Correlation alone does not establish causation.
Valid previous tests remain available; changed mechanisms cannot inherit their support.
Do not design repair tasks in this stage.
"""

OUTPUT = """
Success format:
{"action":"diagnosis",
 "tests":[{"hypothesis_id":"H1","method":"check actually executed",
  "evidence":[{"source":"specific location","observation":"actual result"}],
  "outcome":"supported","interpretation":"why this result supports the mechanism"}],
 "diagnosis":{"mechanism":"supported explanation","hypothesis_ids":["H1"],
              "expected_change":"predicted observable response to intervention"}}
Test outcomes are supported, refuted or inconclusive. Return this session's tests;
the latest valid test must support each hypothesis cited by diagnosis. uncertainty
is optional when concrete. Do not repeat evidence inside diagnosis.
Return hypotheses only when revised, as a complete replacement collection; use new IDs
for changed mechanisms. Return metrics only for new or revised definitions.
To return an invalid problem, use action="discovery" with reason. Returning or stopping
may retain useful tests, hypotheses, metrics and evidence without inventing a diagnosis.
""" + HYPOTHESIS_FIELDS + "\n" + METRIC_FIELDS

class Investigation(Stage):
    result_type = TypeAdapter(DiagnosisResult | InvestigationStop)


def prepare_session(job, directory, prompt):
    return prepare_stage(Investigation, job, directory, prompt, RULES, OUTPUT)


def create_tools(job, state, investigation):
    return evidence_tools(job, investigation, observations=True, conversation_state=state)
