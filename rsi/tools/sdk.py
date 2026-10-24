"""Adapt RSI functions and shared schemas to OpenHands tools."""
import json


def make_tool(tool_name, description, function, input_type):
    """Bind a read-only SDK tool to an ordinary function and its argument schema."""
    from openhands.sdk.tool import Observation, ToolAnnotations, ToolDefinition, ToolExecutor

    class Output(Observation):
        pass

    class Executor(ToolExecutor):
        def __call__(self, action, conversation=None):
            """Return input errors as observations so the model can correct its call."""
            try:
                value = function(**action.model_dump(exclude_none=True, by_alias=True,
                                                     include=set(type(action).model_fields)))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                value = dict(error=dict(code="invalid_input", message=str(exc)))
            return Output.from_text(json.dumps(value, ensure_ascii=False))

    class Definition(ToolDefinition):
        @classmethod
        def create(cls, conv_state=None, **params):
            """Construct the bound SDK-native tool definition."""
            return [cls(description=description, action_type=input_type, observation_type=Output,
                        executor=Executor(), annotations=ToolAnnotations(
                            readOnlyHint=True, destructiveHint=False, openWorldHint=False))]

    tool = type("Rsi" + tool_name.title().replace("_", ""), (Definition,), {"name": tool_name})
    return tool.create()[0]


def reference_tool(function):
    """Share the same read_reference schema and implementation across both agents."""
    from openhands.sdk.tool import Action
    from pydantic import ConfigDict

    class ReferenceInput(Action):
        model_config = ConfigDict(extra="forbid")
        path: str
        lines: list[int] | None = None
        key: str | None = None

    description = """Read source, installed API definitions or Knowledge; read-only.
Paths: source/ is the Bot tree (frozen parent for Diagnosis, current candidate for Editor);
api/ and knowledge/ are frozen references. Task paths and the Bot/Knowledge notes provide starting points;
follow source imports to locate related files. This tool reads files, not directories.
- Text: lines=[start,end], 1-based and inclusive; default [1,120], at most 120 lines per call.
- JSON: key selects dot-separated object keys; omit key to list top-level keys. Do not pass lines.
Returns path with source/start_line/end_line for text, or key/value (keys when listing) for JSON.
next_line continues text reads. truncated/reason appear only when content could not fit; request fewer lines or a smaller key.
Examples: path="source/README.md", lines=[1,80];
path="knowledge/units/terran.json", key="BATTLECRUISER.order_cost"."""
    return make_tool("read_reference", description, function, ReferenceInput)
