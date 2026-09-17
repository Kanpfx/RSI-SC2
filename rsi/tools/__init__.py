import json


STRING = {"type": "string"}
INTEGER = {"type": "integer"}


def schema(name, description, properties, required, examples):
    description += "\nExample arguments: " + "; ".join(json.dumps(item, ensure_ascii=False) for item in examples)
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False}}}


def toolset(root, feedback_dir, timeout, smoke):
    from rsi.tools.bash import TOOLS as BASH_TOOLS, Commands
    from rsi.tools.edit import TOOLS as EDIT_TOOLS, Editor
    from rsi.tools.git import TOOLS as GIT_TOOLS, Git
    from rsi.tools.sc2_api import TOOLS as API_TOOLS, api_query, entity_info, tech_tree

    editor, git = Editor(root, feedback_dir), Git(root)
    commands = Commands(root, timeout, git.current_commit(), smoke)
    handlers = {"read_file": editor.read_file, "search": editor.search,
                "apply_patch": editor.apply_patch, "run_command": commands.run_command,
                "finish": commands.finish, "git_view": git.git_view,
                "api_query": api_query, "entity_info": entity_info, "tech_tree": tech_tree}
    return EDIT_TOOLS + BASH_TOOLS + GIT_TOOLS + API_TOOLS, handlers, commands
