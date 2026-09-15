import importlib.metadata
import importlib.util
import sys
import shutil
import threading
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
import time
from pathlib import Path

from rsi.evaluation.metadata import summarize
from rsi.evolution.state import read_json, save_json
from rsi.process import child_env, run_process


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
    return {"python": sys.executable, "commit": commit, **check_sc2(config),
            "versions": {name: importlib.metadata.version(name)
                         for name in ("burnysc2", "openai", "PyYAML", "pytest", "psutil")}}


def check_sc2(config):
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
    return {"sc2": str(executable), "map": str(map_file)}


class Evaluator:
    def __init__(self, config, config_path):
        self.config = config["evaluation"]
        self.config_path = Path(config_path).resolve()

    def _game(self, number, worktree, node, output, cancel_event):
        game_dir = output / f"game_{number:02d}_workdir"
        game_dir.mkdir()
        shutil.copytree(Path(worktree) / "bot", game_dir / "bot",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
        temporary = game_dir / "tmp"
        temporary.mkdir()
        env = child_env()
        env.update({key: str(temporary) for key in ("TMP", "TEMP", "TMPDIR")})
        result_path = output / f"game_{number:02d}.json"
        worker = Path(__file__).with_name("game.py")
        start = time.monotonic()
        try:
            result = run_process(
                [sys.executable, "-I", "-B", worker, "--worktree", game_dir,
                 "--config", self.config_path, "--output", result_path],
                cwd=game_dir, timeout=self.config["game_timeout_sec"],
                env=env, cancel_event=cancel_event,
            )
        except Exception as exc:
            result = {"stdout": "", "stderr": str(exc), "exit_code": -1, "timed_out": False}
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
        save_json(result_path, record)
        print(f"  {node['id']} game {number}/{self.config['games']}: {record['result']}", flush=True)
        return record

    def evaluate(self, worktree, node, output):
        output = Path(output).resolve()
        output.mkdir(parents=True, exist_ok=True)
        cancel_event = threading.Event()
        pool = ThreadPoolExecutor(max_workers=5)
        futures = []
        try:
            for number in range(1, self.config["games"] + 1):
                if number > 1:
                    time.sleep(3)
                futures.append(pool.submit(self._game, number, worktree, node, output, cancel_event))
            pending = set(futures)
            while pending:
                done, pending = wait(pending, timeout=0.2, return_when=FIRST_EXCEPTION)
                for future in done:
                    future.result()
            # Keep metadata in game-number order, regardless of completion order.
            records = [future.result() for future in futures]
        except BaseException:
            cancel_event.set()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        metadata = {"candidate_id": node["id"], "parent_id": node["parent_id"],
                    "commit": node["commit"], **summarize(records)}
        save_json(output / "metadata.json", metadata)
        return metadata
