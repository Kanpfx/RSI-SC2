"""Run built-in AI games in the current experiment environment."""
import hashlib
import os
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path

from runtime.processes import stop_process
from runtime.workspace.storage import path_reference, project_path, read_json, write_json
from runtime.workspace.versions import bot_inventory, hash_files, verify_version


def load_environment(project, conditions):
    """Pin the local engine, API and map used by every built-in opponent."""
    from sc2 import maps
    from sc2.paths import Paths
    try:
        executable = Paths.EXECUTABLE
        map_path = Path(project) / "runtime/matches/local-play-bootstrap/maps" / (conditions["map"] + ".SC2Map")
        if not map_path.is_file():
            map_path = maps.get(conditions["map"]).path
    except SystemExit as exc:
        raise ValueError("Built-in AI matches require a local SC2 installation; configure SC2PATH") from exc
    with executable.open("rb") as stream:
        engine_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    return dict(backend="builtin", engine_sha256=engine_hash, burnysc2=version("burnysc2"),
                map_sha256=hashlib.sha256(map_path.read_bytes()).hexdigest(),
                map_path=str(map_path.resolve()))


def game_arguments(conditions, map_path, output):
    """Use identical opponent parameters and seeds in both execution environments."""
    return ["--map", str(map_path), "--race",
            {"T": "Terran", "Z": "Zerg", "P": "Protoss"}[conditions["opponent_race"]],
            "--difficulty", conditions["difficulty"], "--ai-build", conditions["ai_build"],
            "--seed", str(conditions["trial"]), "--time-limit", str(conditions["game_time_limit"]),
            "--output", str(output)]


def _run_local(source, command, directory, timeout, cancel):
    """Wait for the native worker and always stop its process tree on interruption."""
    process = None
    deadline = time.monotonic() + timeout
    try:
        with (directory / "stdout.txt").open("wb") as stdout, (directory / "stderr.txt").open("wb") as stderr:
            process = subprocess.Popen(command, cwd=source, stdout=stdout, stderr=stderr,
                env=dict(os.environ, PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1",
                         PYTHONIOENCODING="utf-8"),
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                start_new_session=sys.platform != "win32")
            while process.poll() is None:
                if cancel is not None and cancel.is_set():
                    return dict(termination="cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return dict(termination="wall_timeout")
                try:
                    process.wait(timeout=min(0.25, remaining))
                except subprocess.TimeoutExpired:
                    pass
            return dict(termination="process_exit", returncode=process.returncode)
    finally:
        if process is not None and process.poll() is None:
            stop_process(process)


def collect_result(directory, manifest, result, returncode):
    """Normalize the same Bot artifacts regardless of how the worker was launched."""
    summary_path = directory / "bot_result.json"
    if (directory / "result.json").is_file():
        (directory / "result.json").rename(summary_path)
    overview = read_json(directory / "overview.json")
    summary = read_json(summary_path) if summary_path.is_file() else overview
    result.update({key: summary.get(key) for key in ("steps", "phase", "mean_step_ms", "max_step_ms")})
    result["last_frame"] = overview.get("end_frame", overview["last_frame"])
    manifest["actual"] = overview["actual"]
    if summary.get("bot_runtime_error"):
        result.update(execution_status="bot_error", termination="bot_error", error=summary["bot_runtime_error"])
    elif returncode:
        raise RuntimeError("SC2 worker failed; see stderr.txt")
    elif overview["status"] != "completed" or overview.get("error") or summary.get("telemetry_error"):
        result.update(execution_status="logging_error", termination="logging_error", error=overview.get("error"))
    else:
        outcome = {"Result.Victory": "win", "Result.Defeat": "loss", "Result.Tie": "draw"}.get(summary["result"])
        if outcome is None:
            raise ValueError("Unknown game outcome")
        result.update(execution_status="normal", outcome=outcome,
            termination="time_limit" if result["last_frame"] >= manifest["game_time_limit"] * 22.4 else "game_result")


def run_match(project, source, conditions, directory, purpose, batch_id, *, cancel=None, environment=None):
    """Save normalized outcomes; process errors and timeouts never become wins."""
    environment = environment or load_environment(project, conditions)
    if isinstance(source, dict):
        verify_version(source)
    else:
        source = dict(source_root=path_reference(source), version=hash_files(bot_inventory(source)))
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    manifest = dict(conditions, **source, schema_version=3, purpose=purpose, batch_id=batch_id,
                    run_id=directory.name, environment=environment, actual={},
                    recording=dict(schema_version=4, format="numpy-structured"))
    result = dict(schema_version=3, execution_status="unknown", outcome=None,
                  termination="cancelled", last_frame=None, steps=0, error=None)
    try:
        if cancel is not None and cancel.is_set():
            return result
        if hashlib.sha256(Path(environment["map_path"]).read_bytes()).hexdigest() != environment["map_sha256"]:
            raise ValueError("Map changed after environment preparation")
        source_root = project_path(source["source_root"])
        arguments = game_arguments(conditions, environment["map_path"], directory)
        execution = _run_local(source_root, [sys.executable, "-B", "-m", "bot.run_local", *arguments],
                               directory, conditions["wall_timeout"], cancel)
        result.update(execution)
        if execution["termination"] == "process_exit":
            collect_result(directory, manifest, result, execution["returncode"])
    except Exception as exc:
        result.update(termination="infrastructure_error", error=dict(type=type(exc).__name__, message=str(exc)))
    finally:
        write_json(directory / "manifest.json", manifest)
        write_json(directory / "result.json", result)
    return result
