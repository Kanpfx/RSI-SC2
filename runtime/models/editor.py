"""Resolve evidence and interpreter paths for candidate sessions."""
import os
import sys
from pathlib import Path

from runtime.workspace.storage import project_path


def implementation_paths(candidate, evidence, context=None):
    """Describe evidence, references and interpreter paths for a candidate session."""
    candidate = project_path(candidate)

    def relative(path):
        path = project_path(path)
        try:
            return Path(os.path.relpath(path, candidate)).as_posix()
        except ValueError:
            return path.as_posix()

    return dict(evidence=relative(evidence), references=relative(context) if context else None,
                python=Path(sys.executable).as_posix())
