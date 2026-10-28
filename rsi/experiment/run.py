"""Manage experiment resources, version selection and sequential RSI rounds."""
import copy
import json
import time
from pathlib import Path

from ..analysis.analyzer import Analyzer
from ..analysis.evidence import analyze_evidence, load_batch_analysis
from .iteration import Run
from .batches import BatchArchives
from ..evaluator import batch_problem
from ..references import freeze_context
from .workspace import allocate, preflight_storage
from runner.artifacts import path_reference, project_path, read_json, write_json
from .config import role_settings, validate_config
from runner.console import progress
from ..tools.llm import ExperimentRuntime, load_settings, preflight
from ..tools.sandbox import preflight_editor
from runner.versions import Versions


def _load_models(project, config):
    """Resolve and preflight both roles once before collecting experiment evidence."""
    preflight_editor(config["editor_execution"])
    settings = {role: load_settings(project, role_settings(config["model"], role))
                for role in ("diagnosis", "editor")}
    for model in settings.values():
        preflight(model)
    return settings


class Experiment:
    """Own shared preparation, evidence references and worker cleanup for one experiment."""

    def __init__(self, project, config, *, root=None):
        self.project = Path(project).resolve()
        self.config = copy.deepcopy(config)
        self.config.setdefault("editor_execution", {"mode": "local"})
        validate_config(self.config)
        self.versions = Versions(self.project, self.project / "runtime/rsi")
        self.root = Path(root) if root is not None else allocate(self.versions.root, "experiments")
        self.root.mkdir(parents=True, exist_ok=True)
        self.runtime = ExperimentRuntime(self.project, allocate(self.versions.root, "runtimes", name=self.root.name))
        self.analyzer = self.context = self.model_settings = None
        self.archive = dict(nodes={}, edges=[])
        self.batches = BatchArchives()

    def prepare(self):
        """Prepare fixed inputs once, before collecting or reusing match evidence."""
        preflight_storage(self.runtime.root)
        retained = list(self.versions.root.glob("runtimes/*/work/*/workspace.json"))
        if retained:
            progress("prepare", f"Existing workspaces retained: {len(retained)}; inspect runtime/rsi")
        self.model_settings = _load_models(self.project, self.config)
        self.context = freeze_context(self.project, self.versions.root / "refs")
        self.analyzer = Analyzer(self.context)

    def remember(self, version, evidence):
        """Register valid evidence once in the archive, retaining only paths."""
        self.archive["nodes"].setdefault(version["version"], dict(
            source_root=version["source_root"], batch=evidence["batch_path"],
            analysis=evidence["analysis_path"], expansions=0))

    def load_evidence(self, version):
        """Load an archived version only when it is selected or reused."""
        node = self.archive["nodes"].get(version)
        if node is None:
            return None
        self.batches.restore(project_path(node["batch"]).parent)
        return dict(batch_path=node["batch"], analysis_path=node["analysis"],
                    batch=read_json(project_path(node["batch"])),
                    analysis=load_batch_analysis(node["analysis"], restore=self.batches.restore))

    def restore_evidence(self, evidence):
        """Restore files needed by diagnosis even when the analysis is already in memory."""
        self.batches.restore(project_path(evidence["batch_path"]).parent)
        self.batches.restore(project_path(evidence["analysis_path"]).parent)
        for run in evidence["analysis"]["runs"].values():
            self.batches.restore(project_path(run["log_path"]).parent.parent)


def select_parent(archive, champion, round_number):
    """Alternate champion expansion with the least-expanded archived version."""
    if round_number % 2 == 0:
        return champion
    return min(archive["nodes"], key=lambda version: (
        archive["nodes"][version]["expansions"], version != champion, version))


def run_experiment(project, config, *, parent=None, rounds=1):
    """Grow an isolated archive while retaining a separately evaluated champion."""
    if not isinstance(rounds, int) or rounds < 1:
        raise ValueError("rounds must be a positive integer")
    experiment = Experiment(project, config)
    project, config, versions, root = experiment.project, experiment.config, experiment.versions, experiment.root
    champion_version = versions.freeze(Path(parent).resolve() if parent is not None else project)
    report = dict(experiment_id=root.name, status="running", config=copy.deepcopy(config),
                  rounds=rounds, initial_parent=champion_version, current_parent=champion_version, iterations=[])
    champion_evidence = None
    consecutive_errors = 0
    archive = experiment.archive
    report["archive"] = path_reference(root / "archive.json")
    started = time.monotonic()
    runtime = experiment.runtime
    report["runtime"] = path_reference(runtime.root)

    def save_report():
        """Keep a compact round index; complete round details live in iteration.json."""
        iterations = [dict(round=r["round"], iteration_id=r["iteration_id"], status=r["status"],
                           parent=(r["parent"] or {}).get("version"),
                           candidate=(r["candidate"] or {}).get("version"), report=r["report"])
                      for r in report["iterations"]]
        report["costs"] = dict(matches=sum(r["costs"]["matches"] for r in report["iterations"]),
                              wall_seconds=time.monotonic() - started)
        write_json(root / "archive.json", archive)
        write_json(root / "experiment.json", dict(report, iterations=iterations))

    try:
        save_report()
        parent_batch = config.get("parent_batch")
        if parent_batch:
            path = project_path(parent_batch)
            experiment.batches.restore(path.parent)
            batch = read_json(path)
            if batch["version"] != champion_version["version"] or batch["purpose"] == "holdout":
                raise ValueError("parent_batch must belong to the selected parent and not be holdout data")
            expected = [dict(config["matches"], seed=seed) for seed in range(1, config["parallel_matches"] + 1)]
            actual = []
            for run_id, run_path in batch["runs"].items():
                experiment.batches.restore(project_path(run_path).parent)
                manifest = read_json(project_path(run_path) / "manifest.json")
                if manifest["run_id"] != run_id or manifest["version"] != champion_version["version"] or manifest["purpose"] == "holdout":
                    raise ValueError("Reused run identity/version/purpose mismatch")
                actual.append(manifest)
            keys = ("map", "race", "difficulty", "seed", "game_time_limit")
            signature = lambda items: sorted(json.dumps({k: r[k] for k in keys}, sort_keys=True) for r in items)
            if signature(actual) != signature(expected):
                raise ValueError("parent_batch conditions differ from configured batch")
        experiment.prepare()
        report["context"] = dict(id=experiment.context.name, source_root=path_reference(experiment.context))
        if parent_batch:
            analysis_path = root / "batches" / batch["batch_id"] / "analysis.json"
            champion_evidence = analyze_evidence(experiment.analyzer, batch, path, analysis_path)
        for number in range(1, rounds + 1):
            progress("experiment", f"Round {number}/{rounds}")
            run = Run(experiment)
            parent_key = select_parent(archive, champion_version["version"], number) if archive["nodes"] else champion_version["version"]
            selected = dict(version=parent_key, source_root=archive["nodes"][parent_key]["source_root"]) if archive["nodes"] else champion_version
            selected_evidence = (champion_evidence if champion_evidence and champion_evidence["batch"]["version"] == parent_key
                                 else experiment.load_evidence(parent_key))
            outcome = run.execute(selected, selected_evidence, champion_evidence=champion_evidence)
            result = outcome.report
            if outcome.baseline and batch_problem(outcome.baseline["analysis"]) is None:
                archive["nodes"][parent_key]["expansions"] += 1
                champion_evidence = champion_evidence or outcome.baseline
            for child in outcome.children:
                archive["edges"].append(dict(parent=parent_key, child=child["version"]["version"],
                    iteration_id=result["iteration_id"], task_id=child["task_id"], decision=child["decision"],
                    report=path_reference(run.directory / "iteration.json")))
            report["iterations"].append(dict(round=number, iteration_id=result["iteration_id"],
                status=result["status"], reason=result["reason"], parent=result.get("parent"),
                candidate=result.get("candidate"), costs=result["costs"],
                report=path_reference(run.directory / "iteration.json")))
            if outcome.candidate is not None:
                champion_version, champion_evidence = result["candidate"], outcome.candidate
            report["current_parent"] = champion_version
            report["current_batch"] = champion_evidence.get("batch_path") if champion_evidence else None
            consecutive_errors = consecutive_errors + 1 if result["status"] == "error" else 0
            report["consecutive_errors"] = consecutive_errors
            stop = result["status"] == "interrupted" or result.get("fatal") or consecutive_errors >= 3
            report["status"] = result["status"] if stop else "running"
            if result["status"] == "error" or report["status"] != "running":
                report["reason"] = result["reason"]
            else:
                report.pop("reason", None)
            if result["status"] == "error" and not stop:
                progress("experiment", f"Skipping failed round; consecutive errors={consecutive_errors}/3")
            save_report()
            archive_errors = experiment.batches.finish(
                run.directory / "batches" if result["status"] not in {"error", "interrupted"} else None)
            if archive_errors:
                report.setdefault("archive_errors", []).extend(archive_errors)
                progress("archive", "Batch archival incomplete; see experiment.json")
                save_report()
            if report["status"] != "running":
                break
        else:
            report["status"] = "completed"
    except KeyboardInterrupt:
        report.update(status="interrupted", reason="Cancelled without automatic retry")
    except Exception as exc:
        report.update(status="error", reason=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            runtime.close()
        except Exception as exc:
            report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            if report["status"] not in {"error", "interrupted"}:
                report["status"] = "error"
                report.setdefault("reason", "Experiment resource cleanup failed")
        finally:
            archive_errors = experiment.batches.finish()
            if archive_errors:
                report.setdefault("archive_errors", []).extend(archive_errors)
            save_report()
    progress("experiment", f"Finished: {report['status']}; report={path_reference(root / 'experiment.json')}")
    return report
