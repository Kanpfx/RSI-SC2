"""Run native built-in AI matches or external Bot matches through bootstrap."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
from threading import Event
import time
from uuid import uuid4

from runtime.console import progress
from runtime.workspace.storage import PROJECT, path_reference, project_path, read_json, write_json
from runtime.workspace.versions import bot_inventory, hash_files, inventory, verify_version

LOOPS_PER_SECOND = 22.4
CONDITION_KEYS = ("map", "opponent", "opponent_race", "trial", "game_time_limit", "difficulty", "ai_build")


def validate_match(conditions):
    """Validate native AI parameters or explicit bootstrap Bot opponents."""
    for key in ("map", "opponent"):
        value = conditions.get(key, "")
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError(f"{key} must be a simple bootstrap entry name")
    if conditions.get("opponent_race") not in {"T", "Z", "P"}:
        raise ValueError("opponent_race must be T, Z or P")
    for key in ("game_time_limit", "wall_timeout"):
        if type(conditions.get(key)) is not int or conditions[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    if type(conditions.get("trial", 1)) is not int or conditions.get("trial", 1) <= 0:
        raise ValueError("trial must be a positive integer")
    if set(conditions) - set(CONDITION_KEYS) - {"wall_timeout"}:
        raise ValueError("Unsupported match conditions")
    if conditions["opponent"] == "builtin":
        from sc2.data import AIBuild, Difficulty
        if conditions.get("difficulty") not in Difficulty.__members__ or conditions.get("ai_build") not in AIBuild.__members__:
            raise ValueError("Built-in opponents require valid difficulty and ai_build")
    elif "difficulty" in conditions or "ai_build" in conditions:
        raise ValueError("AI difficulty and build are only valid for builtin opponents")


def docker(arguments, *, timeout=30):
    """Run a bounded Docker command and preserve its diagnostic output."""
    result = subprocess.run(["docker", *arguments], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    if result.returncode:
        raise RuntimeError("Docker command failed: " + result.stderr[-3000:])
    return result.stdout


def load_environment(project, conditions):
    """Record the actual bootstrap, map, opponent and installed image identities."""
    validate_match(conditions)
    if conditions["opponent"] == "builtin":
        from runtime.matches.builtin import load_environment as builtin_environment
        return builtin_environment(project, conditions)
    bootstrap = Path(project).resolve() / "runtime/matches/local-play-bootstrap"
    docker(["info", "--format", "{{.ServerVersion}}"])
    compose = json.loads(docker(["compose", "-f", str(bootstrap / "docker-compose.yml"),
                                "config", "--format", "json"]))
    services = compose["services"]
    images = {name: service["image"] for name, service in services.items()}
    image_ids = {name: docker(["image", "inspect", image, "--format", "{{.Id}}"]).strip()
                 for name, image in images.items()}
    map_file = bootstrap / "maps" / (conditions["map"] + ".SC2Map")
    opponent = bootstrap / "bots" / conditions["opponent"]
    if not opponent.is_dir() or not (opponent / "run.py").is_file():
        raise ValueError("Opponent must be a bootstrap Bot with run.py")
    return dict(backend="local-play-bootstrap", images=images, image_ids=image_ids,
        bootstrap_sha256=hash_files({name: (bootstrap / name).read_bytes()
            for name in ("docker-compose.yml", "config.toml")}),
        map_sha256=hashlib.sha256(map_file.read_bytes()).hexdigest(),
        opponent_sha256=hash_files(inventory(opponent)))


def prepare_match(project, source, conditions, directory, environment):
    """Create isolated mounts without modifying the Bot or bootstrap source trees."""
    bootstrap = Path(project).resolve() / "runtime/matches/local-play-bootstrap"
    if hash_files(inventory(bootstrap / "bots" / conditions["opponent"])) != environment["opponent_sha256"]:
        raise ValueError("Opponent changed after environment preparation")
    map_file = bootstrap / "maps" / (conditions["map"] + ".SC2Map")
    if hashlib.sha256(map_file.read_bytes()).hexdigest() != environment["map_sha256"]:
        raise ValueError("Map changed after environment preparation")
    deployed = directory / "bots/research_bot"
    shutil.copytree(project_path(source["source_root"]) / "bot", deployed / "bot",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (deployed / "run.py").write_text(
        'import sys\nfrom bot.run import main\n'
        'sys.argv.extend(["--output", "/logs/telemetry"])\n'
        'raise SystemExit(main())\n', encoding="utf-8")
    shutil.copytree(bootstrap / "bots" / conditions["opponent"], directory / "bots/opponent",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (directory / "matches").write_text(
        f'1,research_bot,T,python,2,opponent,{conditions["opponent_race"]},python,{conditions["map"]}\n',
        encoding="utf-8")
    write_json(directory / "arena_results.json", dict(results=[]))
    config = (bootstrap / "config.toml").read_text(encoding="utf-8")
    values = dict(MAX_GAME_TIME=math.ceil(conditions["game_time_limit"] * LOOPS_PER_SECOND),
                  MAX_REAL_TIME=conditions["wall_timeout"], ROUNDS_PER_RUN=1, REALTIME="false")
    for key, value in values.items():
        config = re.sub(rf"(?m)^{key}\s*=.*$", f"{key} = {value}", config)
    (directory / "config.toml").write_text(config, encoding="utf-8")

    def mount(path, target, readonly=False):
        return dict(type="bind", source=str(path.resolve()), target=target, read_only=readonly)

    services = {}
    for name, env, volumes in (
        ("sc2_controller", dict(ACSC2_PORT="8083", ACSC2_PROXY_HOST="proxy_controller"),
         [mount(map_file.parent, "/root/StarCraftII/maps", True)]),
        ("bot_controller1", dict(ACBOT_PORT="8081", ACBOT_PROXY_HOST="proxy_controller"),
         [mount(directory / "bots", "/bots")]),
        ("bot_controller2", dict(ACBOT_PORT="8082", ACBOT_PROXY_HOST="proxy_controller"),
         [mount(directory / "bots", "/bots")]),
        ("proxy_controller", dict(ACPROXY_PORT="8080", ACPROXY_BOT_CONT_1_HOST="bot_controller1",
            ACPROXY_BOT_CONT_2_HOST="bot_controller2", ACPROXY_SC2_CONT_HOST="sc2_controller"),
         [mount(directory / "matches", "/app/matches"), mount(directory / "config.toml", "/app/config.toml"),
          mount(directory / "arena_results.json", "/app/results.json"), mount(directory / "replays", "/replays")]),
    ):
        logs = directory / "logs" / name
        logs.mkdir(parents=True)
        services[name] = dict(image=environment["image_ids"][name], environment=env,
                              volumes=volumes + [mount(logs, "/logs")])
    (directory / "replays").mkdir()
    write_json(directory / "compose.json", dict(services=services))


def read_bot_summary(directory):
    """Read the unchanged ladder entry's JSON summary from controller output."""
    decoder = json.JSONDecoder()
    for path in sorted((directory / "logs/bot_controller1/research_bot").glob("*.log")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"(?m)^\{", text):
            try:
                value, _ = decoder.raw_decode(text[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and "bot_runtime_error" in value and "steps" in value:
                return value
    return None


def collect_result(directory, manifest, raw):
    """Join manager outcomes and Bot facts; crashes never count as research wins."""
    result = dict(schema_version=3, execution_status="unknown", outcome=None,
        termination="infrastructure_error", last_frame=raw.get("game_steps"), steps=0,
        phase=None, mean_step_ms=None, max_step_ms=None, error=None, arena_type=raw.get("type"))
    overviews = list((directory / "logs/bot_controller1/telemetry").glob("*/overview.json"))
    recording_error = None
    if len(overviews) == 1:
        for path in overviews[0].parent.iterdir():
            if path.is_file() and (path.name == "overview.json" or path.suffix == ".bin"):
                shutil.copyfile(path, directory / path.name)
        overview = read_json(overviews[0])
        if (overview.get("schema_version") != 4 or overview.get("format") != "numpy-structured"
                or overview.get("status") != "completed"):
            recording_error = overview.get("error") or dict(stage="telemetry", message="Incomplete recording")
    summary = read_bot_summary(directory)
    if summary:
        result.update({key: summary.get(key) for key in ("steps", "phase", "mean_step_ms", "max_step_ms")})
        manifest["actual"] = {key: summary.get(key) for key in ("game_version", "sc2_build", "game_step")}
        manifest["actual"]["map_sha256"] = manifest["environment"]["map_sha256"]
    kind = raw.get("type", "")
    if kind == "Player1Crash" or summary and summary.get("bot_runtime_error"):
        result.update(execution_status="bot_error", termination="bot_error",
                      error=dict(stage="bot", message=(summary or {}).get("bot_runtime_error") or kind))
    elif kind == "Player2Crash":
        result.update(termination="opponent_error", error=dict(stage="opponent", message=kind))
    elif kind in {"Player1Win", "Player2Win", "Tie", "Draw"} and summary and len(overviews) == 1:
        outcome = {"Player1Win":"win", "Player2Win":"loss", "Tie":"draw", "Draw":"draw"}[kind]
        limit = math.ceil(manifest["game_time_limit"] * LOOPS_PER_SECOND)
        result.update(execution_status="normal", outcome=outcome,
            termination="time_limit" if (result["last_frame"] or 0) >= limit else "game_result")
        if summary.get("telemetry_error") or recording_error:
            result.update(execution_status="logging_error", error=summary.get("telemetry_error") or recording_error)
    else:
        result["error"] = dict(stage="manager", message=f"Incomplete match evidence: {kind}; summary={summary is not None}")
    replays = list((directory / "replays").rglob("*.SC2Replay"))
    if len(replays) == 1:
        shutil.copyfile(replays[0], directory / "match.SC2Replay")
    return result


def run_match(project, source, conditions, directory, purpose="standalone", batch_id=None, *,
              cancel=None, environment=None):
    """Dispatch a native or bootstrap match and retain its result and logs."""
    conditions = dict(conditions)
    validate_match(conditions)
    conditions.setdefault("trial", 1)
    if conditions["opponent"] == "builtin":
        from runtime.matches.builtin import run_match as builtin_match
        environment = environment or load_environment(project, conditions)
        return builtin_match(project, source, conditions, directory, purpose, batch_id,
                             cancel=cancel, environment=environment)
    if isinstance(source, dict):
        verify_version(source)
    else:
        source = dict(source_root=path_reference(source), version=hash_files(bot_inventory(source)))
    environment = environment or load_environment(project, conditions)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    manifest = dict(conditions, **source, schema_version=3, purpose=purpose, batch_id=batch_id,
        run_id=directory.name, environment=environment, actual={},
        recording=dict(schema_version=4, format="numpy-structured"))
    write_json(directory / "manifest.json", manifest)
    result = dict(schema_version=3, execution_status="unknown", outcome=None,
                  termination="cancelled", last_frame=None, steps=0, error=None)
    compose = ["compose", "-p", "rsi-" + uuid4().hex[:12], "-f", str(directory / "compose.json")]
    attempted = False
    progress(purpose, f"Match started: trial={conditions['trial']}; map={conditions['map']}; opponent={conditions['opponent']}")
    try:
        if cancel is not None and cancel.is_set():
            return result
        prepare_match(project, source, conditions, directory, environment)
        deadline = time.monotonic() + conditions["wall_timeout"]
        attempted = True
        docker(compose + ["up", "-d", "--pull", "never"], timeout=min(90, conditions["wall_timeout"]))
        while True:
            if cancel is not None and cancel.is_set():
                break
            try:
                outcomes = read_json(directory / "arena_results.json")["results"]
            except json.JSONDecodeError:
                outcomes = []
            if outcomes:
                result = collect_result(directory, manifest, outcomes[0])
                break
            if time.monotonic() >= deadline:
                result.update(termination="wall_timeout", error=dict(stage="match", message="Match wall deadline exceeded"))
                break
            if cancel is not None:
                cancel.wait(0.5)
            else:
                time.sleep(0.5)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        result.update(termination="infrastructure_error", error=dict(stage="match", type=type(exc).__name__, message=str(exc)))
    finally:
        if attempted:
            try:
                (directory / "compose.log").write_text(docker(compose + ["logs", "--no-color"]), encoding="utf-8")
            except Exception as exc:
                result["log_collection_error"] = str(exc)
            try:
                docker(compose + ["down", "--timeout", "5"], timeout=60)
            except Exception as exc:
                result.update(cleanup_error=str(exc), execution_status="infrastructure_error")
        write_json(directory / "manifest.json", manifest)
        write_json(directory / "result.json", result)
    progress(purpose, f"Match finished: {result['termination']}; outcome={result['outcome']}")
    return result


def run_batch(project, version, conditions, directory, purpose, parallel=3, *, wall_budget=None):
    """Run bounded independent matches with one shared environment identity."""
    if not conditions or type(parallel) is not int or parallel < 1:
        raise ValueError("A batch requires conditions and positive parallelism")
    parallel = min(parallel, len(conditions))
    if any({k:c[k] for k in ("map", "opponent", "opponent_race")} !=
           {k:conditions[0][k] for k in ("map", "opponent", "opponent_race")} for c in conditions):
        raise ValueError("A batch must use one map and opponent")
    started = time.monotonic()
    environment = load_environment(project, conditions[0])
    if wall_budget is not None:
        available = wall_budget - (time.monotonic() - started)
        timeout = int(available / math.ceil(len(conditions) / parallel)) - 30
        if timeout < 1:
            raise ValueError("Insufficient wall budget for match execution and cleanup")
        conditions = [dict(c, wall_timeout=min(c["wall_timeout"], timeout)) for c in conditions]
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    jobs = [(c, directory / f"run_{i:02d}") for i,c in enumerate(conditions, 1)]
    batch = dict(batch_id=directory.name, version=version["version"], purpose=purpose,
                 runs={path.name:path_reference(path) for _,path in jobs}, conditions=conditions, parallel=parallel)
    write_json(directory / "batch.json", batch)
    cancel = Event()
    pool = ThreadPoolExecutor(max_workers=parallel)
    try:
        futures = [pool.submit(run_match, project, version, c, path, purpose, directory.name,
                    cancel=cancel, environment=environment) for c, path in jobs]
        # Poll so Ctrl+C can cancel running matches promptly on Windows.
        while not all(f.done() for f in futures):
            time.sleep(0.2)
        results = [future.result() for future in futures]
        batch["results"] = [dict(run_id=path.name, directory=path_reference(path),
            **{key:r.get(key) for key in ("execution_status", "outcome", "termination")})
            for r,(_,path) in zip(results,jobs,strict=True)]
    except BaseException:
        cancel.set()
        batch["status"] = "interrupted" if sys.exc_info()[0] is KeyboardInterrupt else "error"
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        write_json(directory / "batch.json", batch)
    return batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PROJECT / "rsi/config.json")
    parser.add_argument("--map")
    parser.add_argument("--opponent", default="builtin")
    parser.add_argument("--opponent-race", choices=("T", "Z", "P"))
    parser.add_argument("--ai-build")
    parser.add_argument("--difficulty")
    parser.add_argument("--time-limit", type=int)
    parser.add_argument("--wall-timeout", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true", help="Check the installed match environment without starting a game")
    args = parser.parse_args()
    config = read_json(args.config)
    conditions = dict(config["matches"], opponent=args.opponent, trial=1)
    for field, value in (("map", args.map), ("opponent_race", args.opponent_race),
                         ("game_time_limit", args.time_limit), ("wall_timeout", args.wall_timeout)):
        if value is not None:
            conditions[field] = value
    if args.opponent == "builtin":
        conditions["ai_build"] = args.ai_build or config["evaluation_styles"][0]
        if args.difficulty is not None:
            conditions["difficulty"] = args.difficulty
    else:
        conditions.pop("difficulty", None)
    validate_match(conditions)
    if args.check:
        print(json.dumps(load_environment(PROJECT, conditions), indent=2))
        return 0
    directory = args.output or PROJECT / "outputs/matches" / uuid4().hex[:12]
    result = run_match(PROJECT, PROJECT, conditions, directory)
    print(json.dumps(result, indent=2))
    return 0 if result["execution_status"] == "normal" else 1


if __name__ == "__main__":
    raise SystemExit(main())
