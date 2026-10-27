"""Reusable isolated SC2 matches and bounded batches for source trees or snapshots."""
import hashlib
import os
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from .artifacts import path_reference, project_path, read_json, write_json
from .console import error_summary, progress
from .versions import bot_inventory, hash_files, verify_version
from .environment import LOOPS_PER_SECOND, load_environment
from .processes import stop_process, wait_process

MATCH_START_INTERVAL = 3


def match_manifest(project, version, conditions, directory, purpose, batch_id, *, environment=None, log=True):
    """Build the same manifest for launched and cancelled matches."""
    directory = Path(directory)
    return dict(conditions, **version, schema_version=3, purpose=purpose, batch_id=batch_id,
                run_id=directory.name, log=log,
                recording=dict(state_every_iteration=1, state_phase="on_step_start", chunk_size=128),
                environment=environment if environment is not None else load_environment(project), actual={})


def save_result(directory, spec, result):
    """Persist and return the same complete single-match result contract."""
    value = dict(schema_version=3, execution_status="unknown", outcome=None, termination="worker_error",
                 last_frame=None, steps=0, phase=None, mean_step_ms=None, max_step_ms=None, error=None)
    value.update(result)
    for key in ("run_id", "version", "directory", "replay_saved", "summary", "duration_loops"):
        value.pop(key, None)
    write_json(Path(directory) / "result.json", value)
    return value


def result_summary(result, directory):
    """Index a match without duplicating its Bot summary or detailed diagnostics."""
    return dict(run_id=Path(directory).name, directory=path_reference(directory),
                **{key: result[key] for key in ("execution_status", "outcome", "termination") if key in result})


def run_match(project, source, conditions, directory, purpose="standalone", batch_id=None, *,
              cancel=None, environment=None, log=True):
    """Execute one match and preserve infrastructure failures separately from outcomes."""
    if isinstance(source, dict):
        verify_version(source)
        version = source
    else:
        source = Path(source).resolve()
        version = dict(source_root=path_reference(source), version=hash_files(bot_inventory(source)))
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    spec = match_manifest(project, version, conditions, directory, purpose, batch_id,
                          environment=environment, log=log)
    write_json(directory / "manifest.json", spec)
    if cancel is not None and cancel.is_set():
        return save_result(directory, spec, dict(termination="cancelled"))
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    started = time.monotonic()
    progress(purpose, f"Match started: seed={conditions['seed']}; map={conditions['map']}; "
             f"opponent={conditions['race']}/{conditions['difficulty']}")
    with (directory / "stdout.txt").open("w", encoding="utf-8") as out, \
            (directory / "stderr.txt").open("w", encoding="utf-8") as err:
        process = subprocess.Popen([sys.executable, "-B", "-m", "runner.matches",
                                    "--worker", str(directory / "manifest.json")],
                                   cwd=project, stdout=out, stderr=err, creationflags=flags)
        try:
            if not wait_process(process, conditions["wall_timeout"], cancel=cancel):
                stop_process(process)
                if not (directory / "result.json").exists():
                    save_result(directory, spec, dict(termination="cancelled"))
        except subprocess.TimeoutExpired:
            stop_process(process)
            save_result(directory, spec, dict(termination="wall_timeout",
                        error=dict(stage="worker", type="TimeoutExpired", message="Worker exceeded wall timeout")))
        except BaseException:
            stop_process(process)
            raise
    result_path = directory / "result.json"
    if not result_path.exists():
        save_result(directory, spec, dict(termination="worker_exited_without_result", exit_code=process.returncode))
    result = save_result(directory, spec, read_json(result_path))
    progress(purpose, f"Match finished: seed={conditions['seed']}; outcome={result.get('outcome') or 'unknown'}; "
             f"status={result.get('execution_status')}; termination={result.get('termination')}; "
             f"game={(result.get('last_frame') or 0) / LOOPS_PER_SECOND:.1f}s; elapsed={time.monotonic() - started:.1f}s")
    if result.get("error"):
        progress(purpose, "Match error: " + error_summary(result["error"]))
    return result


def run_batch(project, version, conditions, directory, purpose, parallel=3, *, wall_budget=None):
    """Stagger worker submissions to avoid simultaneous client startup; never retry matches."""
    if wall_budget is not None:
        remaining = wall_budget - MATCH_START_INTERVAL * (len(conditions) - 1)
        if remaining < 1:
            raise RuntimeError("Insufficient wall budget for staggered match starts")
        conditions = [dict(condition, wall_timeout=min(condition["wall_timeout"], int(remaining)))
                      for condition in conditions]
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    jobs = [(condition, directory / f"run_{index:02d}") for index, condition in enumerate(conditions, 1)]
    batch = dict(batch_id=directory.name, version=version["version"], purpose=purpose,
                 runs={path.name: path_reference(path) for _, path in jobs}, conditions=conditions)
    write_json(directory / "batch.json", batch)
    cancel = Event()
    futures = []
    try:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            try:
                for index, (condition, path) in enumerate(jobs):
                    if index:
                        time.sleep(MATCH_START_INTERVAL)
                    futures.append(pool.submit(run_match, project, version, condition, path, purpose, directory.name,
                                               cancel=cancel))
                results = [future.result() for future in futures]
            except BaseException:
                cancel.set()
                for future in futures:
                    future.cancel()
                raise
    except BaseException as exc:
        batch["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "error"
        batch["error"] = f"{type(exc).__name__}: {exc}"
        results = []
        for condition, path in jobs:
            if (path / "result.json").exists():
                result = read_json(path / "result.json")
            else:
                path.mkdir(parents=True, exist_ok=True)
                spec = match_manifest(project, version, condition, path, purpose, directory.name)
                if not (path / "manifest.json").exists():
                    write_json(path / "manifest.json", spec)
                result = save_result(path, spec, dict(termination="cancelled"))
            results.append(result_summary(result, path))
        batch["results"] = results
        write_json(directory / "batch.json", batch)
        raise
    batch["results"] = [result_summary(result, path) for result, (_, path) in zip(results, jobs)]
    write_json(directory / "batch.json", batch)
    return batch


def worker(spec_path):
    """Load only the selected Bot source and reuse its native Logger."""
    spec = read_json(spec_path)
    directory = Path(spec_path).resolve().parent
    os.environ["SC2PATH"] = spec["environment"]["sc2_path"]
    sys.path.insert(0, str(project_path(spec["source_root"])))
    bot = logger = None
    stage, error = "import", None
    status, termination = "unknown", "worker_error"
    try:
        from bot.entry import GAME_STEP, ResearchBot
        from bot.telemetry.logger import Logger
        from sc2 import maps
        from sc2.data import Race, Difficulty
        from sc2.main import run_game
        from sc2.player import Bot, Computer
        logger = Logger(directory if spec.get("log", True) else None, frame_step=GAME_STEP)
        bot = ResearchBot(logger=logger, sc2_build=spec["environment"]["sc2_build"])
        stage = "map"
        settings = maps.get(spec["map"])
        spec.setdefault("actual", {}).update(map_sha256=hashlib.sha256(settings.data).hexdigest(), game_step=GAME_STEP)
        write_json(spec_path, spec)
        stage = "game"
        run_game(settings, [Bot(Race.Terran, bot),
                 Computer(getattr(Race, spec["race"]), getattr(Difficulty, spec["difficulty"]))],
                 realtime=False, random_seed=spec["seed"], game_time_limit=spec["game_time_limit"],
                 save_replay_as=str(directory / "match.SC2Replay"))
        status, termination = "normal", "game_result"
    except Exception as exc:
        error = dict(stage=stage, type=type(exc).__name__, message=str(exc))
        if logger and logger.error_ref:
            error["ref"] = logger.error_ref
        else:
            error["traceback"] = traceback.format_exc()
        status = "bot_error" if stage == "import" or (bot and bot.runtime_error) else "infrastructure_error"
    finally:
        if logger:
            try:
                logger.close()
            finally:
                spec.setdefault("actual", {}).update(logger.actual)
                write_json(spec_path, spec)
    summary = bot.summary() if bot else {}
    loop = bot.state.game_loop if bot and bot.steps else None
    outcome = {"Result.Victory": "win", "Result.Defeat": "loss", "Result.Tie": "draw"}.get(
        summary.get("result"))
    if summary.get("bot_runtime_error"):
        status, termination = "bot_error", "bot_error"
    elif status == "normal" and not outcome:
        status, termination = "unknown", "missing_game_result"
    elif status == "normal" and loop is not None and loop / LOOPS_PER_SECOND >= spec["game_time_limit"]:
        termination = "time_limit"
    save_result(directory, spec, dict(execution_status=status, termination=termination, outcome=outcome,
               last_frame=loop, error=error,
               **{key: summary[key] for key in ("steps", "phase", "mean_step_ms", "max_step_ms") if key in summary}))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit("Internal worker: --worker MANIFEST")
    worker(Path(sys.argv[2]))
