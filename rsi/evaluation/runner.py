import importlib.metadata
import importlib.util
import sys
import time
from pathlib import Path

from rsi.evaluation.metadata import summarize
from rsi.evolution.state import read_json, save_json
from rsi.process import run_process


def preflight(root, config):
    from rsi.tools.git import Git

    for module in ("sc2", "openai", "yaml", "pytest", "psutil"):
        if importlib.util.find_spec(module) is None:
            raise RuntimeError(f"Missing {module}; install requirements.txt using {sys.executable}")
    git = Git(root)
    if Path(git.run("rev-parse", "--show-toplevel")).resolve() != Path(root).resolve():
        raise RuntimeError("Project root must be its own Git repository")
    commit = git.current_commit()
    for key in ("user.name", "user.email"):
        try:
            if not git.run("config", "--get", key):
                raise RuntimeError("empty identity")
        except RuntimeError as exc:
            raise RuntimeError(f"Configure Git {key} before running evolution") from exc
    if git.status():
        raise RuntimeError("Commit project changes before running evolution (Git worktree must be clean)")
    try:
        from sc2 import maps
        from sc2.paths import Paths

        executable = Paths.EXECUTABLE
        if not executable.is_file():
            raise RuntimeError(f"SC2 executable missing: {executable}")
        if Paths.CWD is not None and not Paths.CWD.is_dir():
            raise RuntimeError(f"SC2 support directory missing: {Paths.CWD}")
        map_file = maps.get(config["evaluation"]["map"]).path
        if not map_file.is_file() or map_file.stat().st_size == 0:
            raise RuntimeError(f"SC2 map missing or empty: {map_file}")
    except (SystemExit, KeyError, OSError, ValueError) as exc:
        raise RuntimeError(f"SC2 preflight failed; check SC2PATH and map: {exc}") from exc
    return {"python": sys.executable, "commit": commit, "sc2": str(executable), "map": str(map_file),
            "versions": {name: importlib.metadata.version(name)
                         for name in ("burnysc2", "openai", "PyYAML", "pytest", "psutil")}}


class Evaluator:
    def __init__(self, config, config_path):
        self.config = config["evaluation"]
        self.config_path = Path(config_path).resolve()

    def evaluate(self, worktree, node, output):
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        records = []
        worker = Path(__file__).with_name("game.py")
        for number in range(1, self.config["games"] + 1):
            result_path = output / f"game_{number:02d}.json"
            start = time.monotonic()
            result = run_process(
                [sys.executable, "-I", worker, "--worktree", Path(worktree).resolve(),
                 "--config", self.config_path, "--output", result_path.resolve()],
                cwd=worktree, timeout=self.config["game_timeout_sec"],
            )
            duration = time.monotonic() - start
            save_json(output / f"game_{number:02d}.process.json", result)
            try:
                if result["timed_out"]:
                    raise ValueError(f"Game process exceeded {self.config['game_timeout_sec']} seconds")
                if result["exit_code"] != 0:
                    raise ValueError(f"Game process exit {result['exit_code']}: {result['stderr'][-4000:]}")
                record = read_json(result_path)
                summarize([record])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                record = {"result": "crash", "crashed": True, "error": str(exc)}
            record["duration"] = duration
            records.append(record)
            save_json(result_path, record)
            print(f"  {node['id']} game {number}/{self.config['games']}: {record['result']}", flush=True)
        metadata = {"candidate_id": node["id"], "parent_id": node["parent_id"],
                    "commit": node["commit"], **summarize(records)}
        save_json(output / "metadata.json", metadata)
        return metadata
