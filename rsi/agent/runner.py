import json

from rsi.evolution.state import redact, save_json
from rsi.tools.bash import Commands
from rsi.tools.edit import Editor
from rsi.tools.git import Git


def schema(name, description, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False}}}


STRING = {"type": "string"}
TOOLS = [
    schema("view_file", "Read a project text file", {"path": STRING}, ["path"]),
    schema("search_text", "Literal text search in project files", {"text": STRING, "path": STRING}, ["text", "path"]),
    schema("replace_text", "Replace exactly one occurrence in bot/", {"path": STRING, "old": STRING, "new": STRING}, ["path", "old", "new"]),
    schema("write_file", "Write a UTF-8 file under bot/", {"path": STRING, "content": STRING}, ["path", "content"]),
    schema("run_command", "Run a fixed compile/import/smoke command or rg -n -- PATTERN PATH",
           {"argv": {"type": "array", "items": STRING}}, ["argv"]),
    schema("git_view", "Inspect Git status or bot diff", {"action": {"type": "string", "enum": ["status", "diff"]}}, ["action"]),
]


class Agent:
    def __init__(self, llm, max_steps=20, timeout=60):
        self.llm, self.max_steps, self.timeout = llm, max_steps, timeout

    def run(self, root, prompt, context, log_path):
        editor, commands, git = Editor(root), Commands(root, self.timeout), Git(root)
        parent = git.current_commit()
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        handlers = {name: getattr(editor, name) for name in ("view_file", "search_text", "replace_text", "write_file")}
        handlers["run_command"] = commands.run_command

        def git_view(action):
            if action not in ("status", "diff"):
                raise ValueError("Only Git status/diff are available")
            return getattr(git, action)()

        handlers["git_view"] = git_view
        tool_failed = False
        try:
            for step in range(self.max_steps):
                message = self.llm.complete(messages, TOOLS)
                if message.get("role") != "assistant":
                    raise ValueError("Expected an assistant message")
                messages.append(message)
                calls = message.get("tool_calls") or []
                if not calls:
                    changes = git.check_bot_only(parent)
                    if tool_failed or not changes:
                        raise ValueError("Tool call failed or candidate made no changes")
                    return {"ok": True, "steps": step + 1, "summary": message.get("content") or ""}
                for call in calls:
                    try:
                        function = call["function"]
                        arguments = json.loads(function["arguments"])
                        if not isinstance(arguments, dict):
                            raise ValueError("Tool arguments must be an object")
                        result = handlers[function["name"]](**arguments)
                    except Exception as exc:
                        tool_failed = True
                        result = {"error": redact(f"{type(exc).__name__}: {exc}")}
                    messages.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": redact(json.dumps(result, ensure_ascii=False))})
                save_json(log_path, messages)
            raise ValueError("Agent exhausted max_steps")
        except Exception as exc:
            return {"ok": False, "error": redact(f"{type(exc).__name__}: {exc}")}
        finally:
            save_json(log_path, messages)
