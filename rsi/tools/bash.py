import sys

from rsi.process import run_process
from rsi.tools.edit import Editor


COMPILE = ["python", "-m", "compileall", "-q", "bot"]
TEST = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_bot_smoke.py"]
IMPORT = ["python", "-c", "import bot.main"]


class Commands:
    def __init__(self, root, timeout=60):
        self.editor = Editor(root)
        self.timeout = timeout

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


def smoke_test(root, parent, timeout=60):
    from rsi.tools.git import Git

    git = Git(root)
    git.check_bot_only(parent)
    results = []
    commands = Commands(root, timeout)
    for argv in (COMPILE, TEST, IMPORT):
        result = commands.run_command(argv)
        results.append({"argv": argv, **result})
        git.check_bot_only(parent)
        if result["exit_code"] != 0 or result["timed_out"]:
            return {"ok": False, "checks": results}
    return {"ok": True, "checks": results}
