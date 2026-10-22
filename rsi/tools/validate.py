"""Fixed scope and import checks; validation does not judge strategic value."""
import ast
import os
from pathlib import Path
import subprocess
import sys

from .versions import editable, inventory


def validate(parent, candidate, timeout=30):
    """Reject empty/out-of-scope patches, parse modified files and import Bot modules in isolation."""
    checks = []
    try:
        before, after = inventory(parent["source_root"]), inventory(candidate)
        changed = sorted(n for n in before.keys() | after.keys() if before.get(n) != after.get(n))
        forbidden = [n for n in changed if not editable(n)]
        checks.append(dict(name="scope", passed=bool(changed) and not forbidden, changed=changed, forbidden=forbidden))
        if forbidden or not changed:
            return dict(passed=False, checks=checks)
        for name in changed:
            if name in after:
                ast.parse(after[name].decode("utf-8"), filename=name)
        checks.append(dict(name="syntax", passed=True))
        # Import every business module, including additions; never import test files here.
        code = ("import importlib,pkgutil,bot.entry,bot.components; "
                "[importlib.import_module(m.name) for m in pkgutil.walk_packages("
                "bot.components.__path__, bot.components.__name__+'.')]")
        process = subprocess.run([sys.executable, "-B", "-c", code], cwd=candidate,
            env=dict(os.environ, PYTHONPATH=str(Path(candidate).resolve()), PYTHONDONTWRITEBYTECODE="1"),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        checks.append(dict(name="import", passed=process.returncode == 0,
                           returncode=process.returncode,
                           output=(process.stdout + process.stderr)[-12000:]))
    except (ValueError, SyntaxError, OSError, subprocess.TimeoutExpired) as exc:
        checks.append(dict(name="validation_error", passed=False, error=str(exc), error_type=type(exc).__name__,
                           file=getattr(exc, "filename", None), line=getattr(exc, "lineno", None)))
    return dict(passed=all(c["passed"] for c in checks), checks=checks)
