import sys

from rsi.process import run_process
from rsi.evaluation.checks import COMPILE, TEST, IMPORT, smoke_test
from rsi.tools.edit import Editor
from rsi.tools import STRING, schema


TOOLS = [
    schema("run_command", "Allowed argv: python -m compileall -q bot; "
           "python -m pytest -q -p no:cacheprovider tests/test_bot_smoke.py; "
           "python -c 'import bot.main'; rg -n -- PATTERN PATH",
           {"argv": {"type": "array", "items": STRING}}, ["argv"],
           [{"argv": ["python", "-m", "compileall", "-q", "bot"]}]),
    schema("finish", "Validate the candidate; fix failures and retry. Call alone",
           {"summary": STRING}, ["summary"],
           [{"summary": "Increase production capacity to spend surplus minerals; expect faster army growth."}]),
]


class Commands:
    def __init__(self, root, timeout=60, parent=None, smoke=None):
        self.editor = Editor(root)
        self.timeout = timeout
        self.parent, self.smoke = parent, smoke or smoke_test
        self.checks = None

    def finish(self, summary):
        from rsi.tools.git import Git

        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("Provide a nonempty change summary")
        git = Git(self.editor.root)
        if not git.check_bot_only(self.parent):
            raise ValueError("Candidate has no changes")
        self.checks = self.smoke(self.editor.root, self.parent, self.timeout)
        git.check_bot_only(self.parent)
        return self.checks

    def run_command(self, argv):
        if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
            raise ValueError("argv must be a list of strings")
        if argv in (COMPILE, TEST, IMPORT):
            command = [sys.executable, *argv[1:]]
        elif len(argv) == 5 and argv[:3] == ["rg", "-n", "--"]:
            self.editor.path(argv[4])
            command = ["rg", "--no-config", *argv[1:]]
        else:
            raise ValueError("Allowed: compileall, fixed smoke pytest, import bot.main, rg -n -- PATTERN PATH")
        return run_process(command, self.editor.root, self.timeout)

