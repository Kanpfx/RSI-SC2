import sys

from rsi.process import run_process


COMPILE = ["python", "-m", "compileall", "-q", "bot"]
TEST = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_bot_smoke.py"]
IMPORT = ["python", "-c", "import bot.main"]


def smoke_test(root, parent, timeout=60):
    from rsi.tools.git import Git

    git = Git(root)
    git.check_bot_only(parent)
    results = []
    for argv in (COMPILE, TEST, IMPORT):
        result = run_process([sys.executable, *argv[1:]], root, timeout)
        results.append({"argv": argv, **result})
        git.check_bot_only(parent)
        if result["exit_code"] != 0 or result["timed_out"]:
            return {"ok": False, "checks": results}
    return {"ok": True, "checks": results}
