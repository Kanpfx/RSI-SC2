"""Manage experiment resources, version selection and sequential RSI rounds."""
import copy
import random
import time
from pathlib import Path

from runtime.matches.evidence import analyze_evidence, load_batch_analysis
from rsi.workflow import Run
from runtime.workspace.archives import BatchArchives
from rsi.evaluation import conditions, frontier, score_batch, select_parent
from runtime.workspace.references import freeze_context
from runtime.workspace.candidates import allocate, preflight_storage
from runtime.workspace.storage import path_reference, project_path, read_json, write_json
from rsi.config import ROLES, role_settings, validate_config
from runtime.console import progress
from runtime.models.sessions import ExperimentRuntime, load_settings, preflight
from runtime.workspace.versions import Versions


def _load_models(project, config):
    """Resolve and preflight research and execution roles before collecting evidence."""
    settings = {role: load_settings(project, role_settings(config["model"], role))
                for role in ROLES}
    for model in settings.values():
        preflight(model)
    return settings


class Experiment:
    """Own shared preparation, evidence references and worker cleanup for one experiment."""

    def __init__(self, project, config, *, root=None):
        self.project = Path(project).resolve()
        self.config = copy.deepcopy(config)
        validate_config(self.config)
        self.versions = Versions(self.project / "outputs")
        self.root = Path(root) if root is not None else allocate(self.versions.root, "experiments")
        self.root.mkdir(parents=True, exist_ok=True)
        self.runtime = ExperimentRuntime(self.project, allocate(self.versions.root, "runtimes", name=self.root.name))
        self.context = self.model_settings = None
        self.archive = dict(nodes={}, edges=[])
        self.rng = random.Random(self.config["selection_seed"])
        self.batches = BatchArchives()

    def prepare(self):
        """Prepare fixed inputs once, before collecting or reusing match evidence."""
        preflight_storage(self.runtime.root)
        retained = list(self.versions.root.glob("runtimes/*/work/*/workspace.json"))
        if retained:
            progress("prepare", f"Existing workspaces retained: {len(retained)}; inspect outputs/runtimes")
        from runtime.matches.runner import load_environment
        load_environment(self.project, conditions(self.config)[0])
        self.model_settings = _load_models(self.project, self.config)
        self.context = freeze_context(self.project, self.versions.root / "refs")

    def remember(self, version, evidence, parent=None):
        """Cache valid score vectors and lineage once per distinct source version."""
        key = version["version"]
        if key not in self.archive["nodes"]:
            score = score_batch(evidence["analysis"], conditions(self.config))
            environment = self.archive.setdefault("environment", score["environment"])
            if environment != score["environment"]:
                raise ValueError("Evaluation environment changed between versions")
            depth = self.archive["nodes"][parent]["depth"] + 1 if parent else 0
            self.archive["nodes"][key] = dict(source_root=version["source_root"],
                batch=evidence["batch_path"], analysis=evidence["analysis_path"],
                scores=score["scores"], depth=depth)
        self.archive["frontier"] = frontier(self.archive)
        return self.archive["nodes"][key]

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



def run_experiment(project, config, *, parent=None, rounds=1):
    """Evaluate versions on a fixed suite and evolve per-style leaders."""
    if type(rounds) is not int or rounds < 1:
        raise ValueError("rounds must be a positive integer")
    experiment = Experiment(project, config)
    root, runtime, archive = experiment.root, experiment.runtime, experiment.archive
    initial = experiment.versions.freeze(Path(parent).resolve() if parent is not None else experiment.project)
    report = dict(experiment_id=root.name, status="running", config=experiment.config,
                  rounds=rounds, initial_parent=initial, current_parent=initial, iterations=[],
                  archive=path_reference(root / "archive.json"), runtime=path_reference(runtime.root))
    started = time.monotonic()
    consecutive_errors = 0
    initial_evidence = None

    def save_report():
        report["costs"] = dict(matches=sum(r["costs"]["matches"] for r in report["iterations"]),
                               wall_seconds=time.monotonic() - started)
        report["frontier"] = frontier(archive)
        write_json(root / "archive.json", archive)
        write_json(root / "experiment.json", report)

    try:
        save_report()
        experiment.prepare()
        report["context"] = dict(id=experiment.context.name, source_root=path_reference(experiment.context))
        if config.get("parent_batch"):
            path = project_path(config["parent_batch"])
            experiment.batches.restore(path.parent)
            batch = read_json(path)
            if batch["version"] != initial["version"] or batch["purpose"] == "holdout":
                raise ValueError("parent_batch must belong to the initial parent and not be holdout")
            initial_evidence = analyze_evidence(batch, path, root / "initial_evidence.json")
        for number in range(1, rounds + 1):
            progress("experiment", f"Round {number}/{rounds}")
            if archive["nodes"]:
                key = select_parent(archive, experiment.rng)
                selected = dict(version=key, source_root=archive["nodes"][key]["source_root"])
                evidence = experiment.load_evidence(key)
            else:
                selected, evidence = initial, initial_evidence
            report["current_parent"] = selected
            run = Run(experiment)
            outcome = run.execute(selected, evidence)
            result = outcome.report
            for child in outcome.children:
                archive["edges"].append(dict(parent=selected["version"], child=child["version"]["version"],
                    iteration_id=result["iteration_id"], task_id=child["task_id"], decision="evaluated",
                    report=path_reference(run.directory / "iteration.json")))
            report["iterations"].append(dict(round=number, iteration_id=result["iteration_id"],
                status=result["status"], reason=result["reason"], parent=selected["version"],
                costs=result["costs"], report=path_reference(run.directory / "iteration.json")))
            consecutive_errors = consecutive_errors + 1 if result["status"] == "error" else 0
            report["consecutive_errors"] = consecutive_errors
            stop = result["status"] == "interrupted" or result.get("fatal") or consecutive_errors >= 3
            report["status"] = result["status"] if stop else "running"
            if result["status"] == "error" or stop:
                report["reason"] = result["reason"]
            else:
                report.pop("reason", None)
            save_report()
            archive_errors = experiment.batches.finish(
                run.directory / "batches" if result["status"] not in {"error", "interrupted"} else None)
            if archive_errors:
                report.setdefault("archive_errors", []).extend(archive_errors)
                save_report()
            if stop:
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
        archive_errors = experiment.batches.finish()
        if archive_errors:
            report.setdefault("archive_errors", []).extend(archive_errors)
        save_report()
    progress("experiment", f"Finished: {report['status']}; report={path_reference(root / 'experiment.json')}")
    return report
