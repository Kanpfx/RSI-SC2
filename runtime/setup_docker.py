"""Install project dependencies once in the mounted Linux Python environment."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tomllib


def main():
    """Reuse a matching environment and record the installed dependency versions."""
    if sys.version_info[:3] != (3, 12, 16):
        raise RuntimeError("Expected standalone Python 3.12.16 in /opt/rsi/python")
    project = Path("/pyproject.toml").read_bytes()
    identity = hashlib.sha256(project).hexdigest()
    marker = Path("/opt/rsi/environment.json")
    previous = json.loads(marker.read_text()) if marker.exists() else {}
    if previous.get("pyproject_sha256") != identity:
        requirements = tomllib.loads(project.decode("utf-8"))["project"]["dependencies"]
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", *requirements], check=True)
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
    # Exercise the exact SDK modules used by RSI, without making a model request.
    from openhands.sdk.conversation import LocalConversation  # noqa: F401
    from openhands.tools.file_editor import FileEditorTool  # noqa: F401
    from openhands.tools.glob import GlobTool  # noqa: F401
    from openhands.tools.grep import GrepTool  # noqa: F401
    from openhands.tools.terminal import TerminalTool  # noqa: F401
    from sc2.bot_ai import BotAI  # noqa: F401
    import numpy  # noqa: F401

    installed = subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True)
    Path("/opt/rsi/requirements.txt").write_text(installed, encoding="utf-8")
    marker.write_text(json.dumps(dict(python=sys.version, pyproject_sha256=identity), indent=2) + "\n",
                      encoding="utf-8")
    print("Python, OpenHands tools and SC2 API imports passed; environment saved in rsi-sc2-python.")


if __name__ == "__main__":
    main()
