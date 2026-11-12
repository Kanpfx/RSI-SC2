"""Read indexed NumPy trajectories without domain-specific metrics or events."""
import json
from pathlib import Path

from bot.telemetry.recorder import read_dataset


def read_overview(directory):
    """Read the recording contract; tables retain their own native frame axes."""
    overview = json.loads((Path(directory) / "overview.json").read_text(encoding="utf-8"))
    if overview["schema_version"] != 4 or overview["format"] != "numpy-structured":
        raise ValueError("Unsupported telemetry format")
    return overview


def read_telemetry(directory):
    """Return the overview and named structured arrays for ordinary NumPy analysis."""
    overview = read_overview(directory)
    return overview, {name: read_dataset(directory, name) for name in overview["datasets"]}


from collections import Counter

import numpy as np

from runtime.matches.runner import CONDITION_KEYS
from runtime.workspace.archives import restore_batch
from runtime.workspace.storage import path_reference, project_path, read_json, write_json


def run_view(directory, run_id):
    """Join authoritative match facts with the binary dataset directory."""
    directory = project_path(directory)
    manifest, result = read_json(directory / "manifest.json"), read_json(directory / "result.json")
    problems, ratios = [], []
    overview = {}
    last_frame = result.get("last_frame")
    try:
        overview = read_overview(directory)
        if overview["status"] != "completed" or overview["error"] or overview.get("bot_runtime_error"):
            problems.append("recording_incomplete")
        if overview["steps"] != result["steps"] or not overview["steps"]:
            problems.append("observation_step_mismatch")
        if last_frame is None:
            last_frame = overview["last_frame"]
        if not overview["datasets"]:
            problems.append("no_datasets")
        for name, info in overview["datasets"].items():
            if info["error"]:
                problems.append("collector_error:" + name)
            table = read_dataset(directory, name, mmap=True)
            frames = table["frame"]
            start, end = info["range"]
            if len(frames) and (np.any(frames < start) or np.any(np.diff(frames) < 0)
                    or (end is not None and np.any(frames >= end)) or np.any(frames > last_frame)):
                problems.append("invalid_frames:" + name)
            if info["kind"] == "series":
                if len(frames) > 1 and np.any(np.diff(frames) == 0):
                    problems.append("duplicate_samples:" + name)
                stop = min(last_frame + 1, end) if end is not None else last_frame + 1
                interval = max(info["every_frames"], overview["actual"]["game_step"])
                expected = max(0, (stop - start - 1) // interval + 1)
                ratios.append(min(1, len(frames) / expected) if expected else 1)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        problems.append(str(exc))
    if result["execution_status"] != "normal":
        problems.append("run_not_normal")
    actual = manifest.get("actual", {})
    return dict(run_id=run_id, log_path=path_reference(directory / "overview.json"),
        conditions={key: manifest[key] for key in CONDITION_KEYS if key in manifest},
        environment=dict(environment=manifest.get("environment"), actual=actual,
                         game_time_limit=manifest.get("game_time_limit")),
        outcome=result, datasets=overview.get("datasets", {}),
        coverage=dict(last_frame=last_frame, sample_ratio=min(ratios) if ratios else None,
                      integrity="partial" if problems else "complete", problems=problems))


def analyze_batch(batch):
    """Build a compact batch index; no inferred gameplay metrics or events."""
    if batch["purpose"] == "holdout":
        raise ValueError("Holdout is unavailable to development agents")
    reports = {run_id: run_view(path, run_id) for run_id, path in batch["runs"].items()}
    return dict(schema_version=4, batch_id=batch["batch_id"], version=batch["version"],
                purpose=batch["purpose"], runs=reports,
                summary=dict(outcomes=dict(Counter(r["outcome"]["outcome"] or "unknown" for r in reports.values()))))


def load_batch_analysis(path, *, restore=restore_batch):
    """Restore referenced batch files before exposing their paths to an agent."""
    path = project_path(path)
    restore(path.parent)
    analysis = read_json(path)
    if analysis["schema_version"] != 4:
        raise ValueError("Unsupported evidence format")
    for run in analysis["runs"].values():
        restore(project_path(run["log_path"]).parent.parent)
    return analysis


def analyze_evidence(batch, batch_path, analysis_path):
    """Save a recording index and return the references used by the experiment."""
    analysis = analyze_batch(batch)
    write_json(analysis_path, analysis)
    return dict(batch=batch, analysis=analysis, batch_path=path_reference(batch_path),
                analysis_path=path_reference(analysis_path))
