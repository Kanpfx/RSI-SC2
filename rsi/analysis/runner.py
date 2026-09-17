import json
from pathlib import Path

from rsi.console import verbose
from rsi.evolution.state import redact, save_json
from rsi.evaluation.checks import smoke_test
from rsi.tools import toolset
from rsi.context.window import working_messages


class Agent:
    def __init__(self, llm, max_steps=20, timeout=60, smoke=smoke_test):
        self.llm, self.max_steps, self.timeout, self.smoke = llm, max_steps, timeout, smoke

    def run(self, root, prompt, context, log_path, feedback_dir=None):
        schemas, handlers, commands = toolset(root, feedback_dir, self.timeout, self.smoke)
        stage = f"Agent {Path(log_path).parent.name}"
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(
                        {**context, "feedback_files": handlers["search"]("feedback", limit=200)}, ensure_ascii=False)}]
        try:
            for step in range(self.max_steps):
                verbose(stage, f"step {step + 1}/{self.max_steps} model")
                message = self.llm.complete(working_messages(messages), schemas)
                if message.get("role") != "assistant":
                    raise ValueError("Expected an assistant message")
                messages.append(message)
                calls = message.get("tool_calls") or []
                if calls:
                    verbose(stage, f"step {step + 1}/{self.max_steps}: "
                           + ", ".join(call["function"]["name"] for call in calls))
                if not calls:
                    messages.append({"role": "user", "content": "Continue using tools, or call finish with your change summary."})
                for call in calls:
                    name = call["function"]["name"]
                    try:
                        arguments = json.loads(call["function"]["arguments"])
                        if not isinstance(arguments, dict):
                            raise ValueError("Tool arguments must be an object")
                        if name == "finish" and len(calls) != 1:
                            raise ValueError("Call finish alone, after all edits")
                        result = handlers[name](**arguments)
                    except Exception as exc:
                        result = {"error": redact(f"{type(exc).__name__}: {exc}")}
                    messages.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": redact(json.dumps(result, ensure_ascii=False))})
                    if name == "finish" and result.get("ok"):
                        return {"ok": True, "steps": step + 1, "summary": arguments["summary"], "smoke": commands.checks}
                save_json(log_path, messages)
            raise ValueError("Agent exhausted max_steps")
        except Exception as exc:
            return {"ok": False, "error": redact(f"{type(exc).__name__}: {exc}"), "smoke": commands.checks}
        finally:
            save_json(log_path, messages)
