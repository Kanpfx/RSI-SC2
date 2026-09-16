import json

from rsi.evolution.state import redact, save_json
from rsi.tools.bash import TOOLS as BASH_TOOLS, Commands, smoke_test
from rsi.tools.edit import TOOLS as EDIT_TOOLS, Editor
from rsi.tools.git import TOOLS as GIT_TOOLS, Git
from rsi.tools.sc2_api import TOOLS as API_TOOLS, api_query, entity_info, tech_tree


TOOLS = EDIT_TOOLS + BASH_TOOLS + GIT_TOOLS + API_TOOLS


class Agent:
    def __init__(self, llm, max_steps=20, timeout=60, smoke=smoke_test):
        self.llm, self.max_steps, self.timeout, self.smoke = llm, max_steps, timeout, smoke

    def run(self, root, prompt, context, log_path, feedback_dir=None):
        editor, git = Editor(root, feedback_dir), Git(root)
        commands = Commands(root, self.timeout, git.current_commit(), self.smoke)
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(
                        {**context, "feedback_files": editor.search("feedback")}, ensure_ascii=False)}]
        handlers = {"read_file": editor.read_file, "search": editor.search,
                    "apply_patch": editor.apply_patch, "run_command": commands.run_command,
                    "finish": commands.finish, "git_view": git.git_view,
                    "api_query": api_query, "entity_info": entity_info, "tech_tree": tech_tree}
        try:
            for step in range(self.max_steps):
                message = self.llm.complete(messages, TOOLS)
                if message.get("role") != "assistant":
                    raise ValueError("Expected an assistant message")
                messages.append(message)
                calls = message.get("tool_calls") or []
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
