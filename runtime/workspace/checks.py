"""Fixed scope and import checks; validation does not judge strategic value."""
import ast
import copy
import os
import subprocess
import sys
from pathlib import Path

from runtime.workspace.storage import project_path
from runtime.workspace.versions import inventory, write_diff


def editable(name):
    """Allow only Python business components; reject ambiguous relative paths."""
    return (name.startswith(tuple(f"bot/components/{part}/" for part in
                                 ("behaviors", "tactics")))
            and name.endswith(".py") and ".." not in name.split("/") and "\\" not in name)


def validate(parent, candidate, timeout=30, *, patch_path=None):
    """Reject empty/out-of-scope patches, parse modified files and import Bot modules in isolation."""
    checks = []
    try:
        before, after = inventory(project_path(parent["source_root"])), inventory(candidate)
        changed = sorted(n for n in before.keys() | after.keys() if before.get(n) != after.get(n))
        if patch_path is not None:
            write_diff(before, after, changed, patch_path)
        forbidden = [n for n in changed if not editable(n)]
        checks.append(dict(name="scope", passed=bool(changed) and not forbidden, changed=changed, forbidden=forbidden))
        if forbidden or not changed:
            return dict(passed=False, checks=checks)
        for name in changed:
            if name in after:
                ast.parse(after[name].decode("utf-8"), filename=name)
        checks.append(dict(name="syntax", passed=True))
        checks.append(_check_imports(candidate, timeout))
    except (ValueError, SyntaxError, OSError, subprocess.TimeoutExpired) as exc:
        error = dict(name="validation_error", passed=False, error=str(exc), error_type=type(exc).__name__)
        for key, attribute in (("file", "filename"), ("line", "lineno")):
            value = getattr(exc, attribute, None)
            if value is not None:
                error[key] = value
        checks.append(error)
    return dict(passed=all(c["passed"] for c in checks), checks=checks)


def _check_imports(candidate, timeout):
    """Import all candidate business modules in an isolated Python process."""
    # Import every business module, including additions; never import test files here.
    code = ("import importlib,pkgutil,bot.entry,bot.components; "
            "[importlib.import_module(m.name) for m in pkgutil.walk_packages("
            "bot.components.__path__, bot.components.__name__+'.')]")
    process = subprocess.run([sys.executable, "-B", "-c", code], cwd=candidate,
        env=dict(os.environ, PYTHONPATH=str(Path(candidate).resolve()), PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    return dict(name="import", passed=process.returncode == 0,
                       returncode=process.returncode,
                       output=(process.stdout + process.stderr)[-12000:])


def validate_sampling(parent, candidate, timeout=30, *, patch_path=None, allow_unchanged=False):
    """Allow collector edits while preserving strategy, recorder and logger lifecycle."""
    checks = []
    try:
        before, after = inventory(project_path(parent["source_root"])), inventory(candidate)
        changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
        if patch_path is not None:
            write_diff(before, after, changed, patch_path)
        logger = "bot/telemetry/logger.py"
        scope = (changed == [logger] or allow_unchanged and not changed) and logger in after
        checks.append(dict(name="scope", passed=scope, changed=changed))
        if not scope:
            return dict(passed=False, checks=checks)
        original, measured = [ast.parse(files[logger].decode("utf-8-sig")) for files in (before, after)]
        hooks = {"_configure", "_event", "_execution"}
        fixed = {node.name for cls in original.body if isinstance(cls, ast.ClassDef) and cls.name == "Logger"
                 for node in cls.body if isinstance(node, ast.FunctionDef) and node.name not in hooks}

        def framework(tree):
            tree = copy.deepcopy(tree)
            tree.body = [node for node in tree.body
                         if not isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef))]
            for cls in tree.body:
                if isinstance(cls, ast.ClassDef) and cls.name == "Logger":
                    cls.body = [node for node in cls.body
                                if not isinstance(node, ast.FunctionDef) or node.name in fixed]
            return ast.dump(tree, include_attributes=False)

        checks.append(dict(name="framework", passed=framework(original) == framework(measured)))
        if checks[-1]["passed"]:
            checks.append(_check_imports(candidate, timeout))
            process = subprocess.run([sys.executable, "-B", "-c",
                "from bot.telemetry.logger import Logger; "
                "logger=Logger(); "
                "logger.close()"],
                cwd=candidate, env=dict(os.environ, PYTHONPATH=str(Path(candidate).resolve())),
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            checks.append(dict(name="collectors", passed=process.returncode == 0,
                               output=(process.stdout + process.stderr)[-4000:]))
    except (ValueError, SyntaxError, OSError, subprocess.TimeoutExpired) as exc:
        checks.append(dict(name="validation_error", passed=False, error=str(exc)))
    return dict(passed=all(check["passed"] for check in checks), checks=checks)


def validate_targets(targets, parent):
    """Resolve editable source targets and optional qualified symbols."""
    for target in targets:
        path, separator, symbol = target.partition("::")
        if path.startswith("bot."):
            path = path.replace(".", "/") + ".py"
        source = project_path(parent["source_root"]) / path
        if not editable(path) or not source.is_file():
            raise ValueError(f"Invalid code target: {target}; use an editable bot/... file or bot.* module")
        if separator and symbol not in set(_symbols(ast.parse(source.read_text(encoding="utf-8-sig")))):
            raise ValueError(f"Code target symbol unavailable: {target}")


def _symbols(node, prefix=""):
    for child in getattr(node, "body", []):
        if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = prefix + child.name
            yield name
            yield from _symbols(child, name + ".")
