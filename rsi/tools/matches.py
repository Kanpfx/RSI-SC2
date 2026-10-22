"""Bounded parallel batches; each frozen Bot runs in its own process."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from uuid import uuid4

from .versions import read_json, write_json, source_hash


def stop_process(process):
    """Terminate only a worker process tree created by this runner."""
    if process.poll() is None:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            process.kill()
        process.wait(timeout=15)


def run_match(project, version, conditions, directory, purpose, batch_id):
    """Execute one match and preserve infrastructure failures separately from outcomes."""
    if source_hash(version["source_root"]) != version["version"]:
        raise ValueError("Bot snapshot changed")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    spec = dict(conditions, **version, purpose=purpose, batch_id=batch_id,
                run_id=directory.name, output_dir=str(directory),
                environment=read_json(Path(project) / "environment.json"))
    write_json(directory / "manifest.json", spec)
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    started = time.monotonic()
    print(f"[{time.strftime('%H:%M:%S')}] [match] Started: purpose={purpose} "
          f"seed={conditions['seed']} difficulty={conditions['difficulty']} run={directory.name}", flush=True)
    with (directory / "stdout.txt").open("w", encoding="utf-8") as out, \
            (directory / "stderr.txt").open("w", encoding="utf-8") as err:
        process = subprocess.Popen([sys.executable, "-B", "-m", "rsi.tools.matches",
                                    "--worker", str(directory / "manifest.json")],
                                   cwd=project, stdout=out, stderr=err, creationflags=flags)
        try:
            process.wait(timeout=conditions["wall_timeout"])
        except subprocess.TimeoutExpired:
            stop_process(process)
            write_json(directory / "result.json", dict(execution_status="unknown", outcome=None,
                       termination="wall_timeout", error="Worker exceeded wall timeout"))
        except BaseException:
            stop_process(process)
            raise
    result_path = directory / "result.json"
    if not result_path.exists():
        write_json(result_path, dict(execution_status="unknown", outcome=None,
                   termination="worker_exited_without_result", exit_code=process.returncode))
    result = dict(read_json(result_path), run_id=directory.name, directory=str(directory))
    print(f"[{time.strftime('%H:%M:%S')}] [match] Finished: purpose={purpose} "
          f"seed={conditions['seed']} run={directory.name} status={result.get('execution_status')} "
          f"outcome={result.get('outcome')} termination={result.get('termination')} "
          f"elapsed={time.monotonic() - started:.1f}s", flush=True)
    return result


def run_batch(project, version, conditions, directory, purpose, parallel=5):
    """Run a fixed batch without retries; retain its manifest before execution."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    jobs = [(condition, directory / uuid4().hex) for condition in conditions]
    batch = dict(batch_id=directory.name, version=version["version"], purpose=purpose,
                 runs={path.name: str(path) for _, path in jobs}, conditions=conditions)
    write_json(directory / "batch.json", batch)
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = [pool.submit(run_match, project, version, condition, path, purpose, directory.name)
                   for condition, path in jobs]
        results = [future.result() for future in futures]
    batch["results"] = results
    write_json(directory / "batch.json", batch)
    return batch


def worker(spec_path):
    """Load only the selected frozen Bot and reuse its native Logger."""
    spec = read_json(spec_path)
    directory = Path(spec["output_dir"])
    os.environ["SC2PATH"] = spec["environment"]["sc2_path"]
    sys.path.insert(0, spec["source_root"])
    bot = logger = None
    stage, error = "import", None
    status, termination = "unknown", "worker_error"
    try:
        from bot.entry import ResearchBot
        from bot.telemetry.logger import Logger
        from sc2 import maps
        from sc2.data import Race, Difficulty
        from sc2.main import run_game
        from sc2.player import Bot, Computer
        logger = Logger(directory)
        bot = ResearchBot(logger=logger, sc2_build=spec["environment"]["sc2_build"])
        stage = "map"
        settings = maps.get(spec["map"])
        spec["map_sha256"] = hashlib.sha256(settings.data).hexdigest()
        write_json(spec_path, spec)
        stage = "game"
        run_game(settings, [Bot(Race.Terran, bot),
                 Computer(getattr(Race, spec["race"]), getattr(Difficulty, spec["difficulty"]))],
                 realtime=False, random_seed=spec["seed"], game_time_limit=spec["game_time_limit"],
                 save_replay_as=str(directory / "match.SC2Replay"))
        status, termination = "normal", "game_result"
    except Exception:
        error = traceback.format_exc()
        status = "bot_error" if stage == "import" or (bot and bot.runtime_error) else "infrastructure_error"
    finally:
        if logger:
            logger.close()
    summary = bot.summary() if bot else {}
    loop = bot.state.game_loop if bot and bot.steps else 0
    outcome = {"Result.Victory": "win", "Result.Defeat": "loss", "Result.Tie": "draw"}.get(
        summary.get("result"))
    if summary.get("bot_runtime_error"):
        status, termination = "bot_error", "bot_error"
    elif status == "normal" and not outcome:
        status, termination = "unknown", "missing_game_result"
    elif status == "normal" and loop / 22.4 >= spec["game_time_limit"]:
        termination = "time_limit"
    write_json(directory / "result.json", dict(run_id=spec["run_id"], version=spec["version"],
               execution_status=status, termination=termination, outcome=outcome,
               duration_loops=loop, error=error, summary=summary,
               replay_saved=(directory / "match.SC2Replay").exists()))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit("Internal worker: --worker MANIFEST")
    worker(Path(sys.argv[2]))
