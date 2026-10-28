"""Assemble, persist and load current-format match evidence."""
from collections import Counter
from pathlib import Path

from runner.artifacts import path_reference, project_path, read_json, write_json
from runner.environment import LOOPS_PER_SECOND as LOOPS
from ..experiment.batches import restore_batch


def save_batch_analysis(path, analysis):
    """Save each run once and retain only source/analysis references in the batch."""
    path = Path(path)
    runs = {}
    for run_id, report in analysis["runs"].items():
        source = project_path(report["log_path"]).parent
        run_path = path.parent / run_id / "analysis.json"
        write_json(run_path, report["analysis"], compact=True)
        runs[run_id] = dict(source=path_reference(source), analysis=path_reference(run_path))
    write_json(path, dict(analysis, runs=runs))


def load_batch_analysis(path, *, restore=restore_batch):
    """Restore saved query data without decoding the original logs again."""
    path = project_path(path)
    restore(path.parent)
    analysis = read_json(path)
    runs = {}
    for run_id, reference in analysis["runs"].items():
        if not isinstance(reference, dict) or set(reference) != {"source", "analysis"}:
            raise ValueError("Unsupported batch analysis index; re-analyze a current-format batch")
        restore(project_path(reference["analysis"]).parent.parent)
        restore(project_path(reference["source"]).parent)
        stored = read_json(project_path(reference["analysis"]))
        if stored["schema_version"] != 3 or "view" not in stored:
            raise ValueError("Unsupported analysis format; re-analyze a current-format batch")
        runs[run_id] = run_view(reference["source"], run_id, stored)
    return dict(analysis, runs=runs)


def run_view(directory, run_id, analysis):
    """Combine derived query columns with the authoritative manifest and result."""
    directory = project_path(directory)
    view = analysis["view"]
    if any(len(values) != len(view["frame"]) for values in analysis["series"].values()):
        raise ValueError("State observation count differs from the saved analysis; re-analyze the original batch")
    manifest, result = read_json(directory / "manifest.json"), read_json(directory / "result.json")
    last_frame = result["last_frame"]
    if last_frame is None:
        last_frame = view["last_frame"]
    actions = dict(view["actions"], bot_commands_per_minute=(
        view["actions"]["submitted"] * 60 * LOOPS / last_frame if last_frame else None))
    quality = analysis["quality"]
    problems = [problem for source in ("state", "trace", "events") for problem in quality[source]["problems"]]
    if result["execution_status"] != "normal":
        problems.append("run_not_normal")
    if result["steps"] != len(view["frame"]):
        problems.append("state_step_count_mismatch")
    actual = manifest["actual"]
    coverage = dict(last_frame=last_frame, sampled_frames=quality["state"]["covered_frames"],
        sample_ratio=min(1, quality["state"]["covered_frames"] / last_frame) if last_frame else None,
        integrity="partial" if problems else "complete", problems=problems,
        missing_metrics=quality["missing_metrics"], unattributed_deaths=quality["unattributed_deaths"])
    return dict(run_id=run_id, log_path=path_reference(directory / "trace.jsonl"),
        conditions={key: manifest[key] for key in ("map", "race", "difficulty", "seed")},
        environment=dict(map_sha256=actual.get("map_sha256"), environment=manifest.get("environment"),
                         actual=actual, game_time_limit=manifest.get("game_time_limit")),
        outcome=result, coverage=coverage, frame=view["frame"],
        metrics=dict(view["observations"], **analysis["series"]), counts=view["counts"],
        events=sorted(analysis["events"] + view["events"], key=lambda event: event["frame"]),
        modules=view["modules"], actions=actions, analysis=analysis)


def analyze_batch(analyzer, batch):
    """Analyze source logs and join metadata without changing the source batch."""
    if batch["purpose"] == "holdout":
        raise ValueError("Holdout is unavailable to the development Analyzer")
    reports = {run_id: run_view(path, run_id, analyzer.analyze_run(path, run_id))
               for run_id, path in batch["runs"].items()}
    summary = dict(outcomes=dict(Counter(report["outcome"]["outcome"] or "unknown" for report in reports.values())),
                   event_counts=dict(Counter(event["kind"] for report in reports.values() for event in report["events"])))
    return dict(batch_id=batch["batch_id"], version=batch["version"], purpose=batch["purpose"], runs=reports, summary=summary)


def analyze_evidence(analyzer, batch, batch_path, analysis_path):
    """Persist a new analysis and return the evidence used by one experiment."""
    analysis = analyze_batch(analyzer, batch)
    save_batch_analysis(analysis_path, analysis)
    return dict(batch=batch, analysis=analysis, batch_path=path_reference(batch_path), analysis_path=path_reference(analysis_path))
