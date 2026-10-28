"""Adapt fixed RSI tool types and budgeted model transport to OpenHands."""
import json
import math
from openhands.sdk import LLM
from openhands.sdk.tool import Action, Observation, ToolAnnotations, ToolDefinition, ToolExecutor
from pydantic import ConfigDict


class BudgetedLLM(LLM):
    token_budget: int

    def check_request(self, messages, tools, security=False):
        """Reserve estimated input and maximum output before each SDK request."""
        usage = self.metrics.accumulated_token_usage
        spent = usage.prompt_tokens + usage.completion_tokens if usage else 0
        estimate = self.get_token_count(messages, tools, security)
        required = math.ceil(estimate * 1.15) + 1024 + self.max_output_tokens
        if estimate <= 0:
            raise ValueError("Token estimation failed; check SDK token counter and tool schemas")
        if spent + required > self.token_budget:
            raise ValueError("Next request cannot fit the token budget")

    def completion(self, messages, tools=None, add_security_risk_prediction=False, **kwargs):
        """Apply the session budget to SDK completion transport."""
        self.check_request(messages, tools, add_security_risk_prediction)
        return super().completion(messages, tools, add_security_risk_prediction, **kwargs)

    def responses(self, messages, tools=None, **kwargs):
        """Apply the same budget to SDK Responses transport."""
        self.check_request(messages, tools, kwargs.get("add_security_risk_prediction", False))
        return super().responses(messages, tools, **kwargs)


class ToolOutput(Observation):
    pass


class FunctionExecutor(ToolExecutor):
    def __init__(self, function):
        self.function = function

    def __call__(self, action, conversation=None):
        """Return input errors as observations so the model can correct its call."""
        try:
            if self.function is None:
                raise ValueError("Tool session is closed")
            value = self.function(**action.model_dump(exclude_none=True, by_alias=True,
                                                     include=set(type(action).model_fields)))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            value = dict(error=dict(code="invalid_input", message=str(exc)))
        return ToolOutput.from_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    def close(self):
        """Drop the bound investigation/reference reader even if the tool is retained."""
        self.function = None


class FunctionTool(ToolDefinition):
    @classmethod
    def create(cls, conv_state=None, *, description, function, input_type):
        """Construct one session-owned executor from a fixed tool type."""
        return [cls(description=description, action_type=input_type, observation_type=ToolOutput,
                    executor=FunctionExecutor(function), annotations=ToolAnnotations(
                        readOnlyHint=True, destructiveHint=False, openWorldHint=False))]


class SelectIssueTool(FunctionTool):
    pass


class QueryLogsTool(FunctionTool):
    pass


class QueryMemoryTool(FunctionTool):
    pass


class ConcludeTool(FunctionTool):
    pass


class ReadReferenceTool(FunctionTool):
    pass


TOOL_TYPES = {tool.name: tool for tool in
              (SelectIssueTool, QueryLogsTool, QueryMemoryTool, ConcludeTool, ReadReferenceTool)}


def make_tool(tool_name, description, function, input_type):
    """Bind a session function without creating classes or registering global instances."""
    return TOOL_TYPES[tool_name].create(description=description, function=function, input_type=input_type)[0]


class ReferenceInput(Action):
    model_config = ConfigDict(extra="forbid")
    path: str
    lines: list[int] | None = None
    key: str | None = None


def reference_tool(function):
    """Share the same read_reference schema and implementation across both agents."""
    description = """Read source, API definitions or Knowledge files.
Paths: source/ is the Bot tree (frozen parent for Diagnosis, current candidate for Editor);
api/ and knowledge/ are frozen. Start from task paths or follow source imports. Files only.
- Text: lines=[start,end], 1-based and inclusive; default [1,120], at most 120 lines per call.
- JSON: key selects dot-separated object keys; omit key to list top-level keys. Do not pass lines.
Returns path with source/start_line/end_line for text, or key/value (keys when listing) for JSON.
next_line continues text reads. If truncated, request fewer lines or a smaller key; reason explains the limit.
Examples: path="source/README.md", lines=[1,80];
path="knowledge/units/terran.json", key="BATTLECRUISER.order_cost"."""
    return make_tool("read_reference", description, function, ReferenceInput)
