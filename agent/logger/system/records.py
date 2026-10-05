"""Explicit run, decision and model-request correlation."""
from uuid import uuid4


def decision_fields(iteration):
    if iteration is None or iteration < 0:
        return {}
    return {"decision_id": f"d{iteration}", "request_iteration": iteration}


class RequestTrace:
    """Attach immutable request identity without changing the client interface."""

    def __init__(self, telemetry, iteration, phase):
        self.telemetry = telemetry
        self.fields = {
            **decision_fields(iteration),
            "request_id": uuid4().hex,
            "request_phase": phase,
        }

    def _fields(self, fields):
        result = {**fields, **self.fields}
        if "attempt" in result:
            result["attempt_id"] = f"{self.fields['request_id']}:t{result['attempt']}"
        return result

    def event(self, name, **fields):
        self.telemetry.event(name, **self._fields(fields))

    def model_conversation(self, **fields):
        self.telemetry.model_conversation(**self._fields(fields))
