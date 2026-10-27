"""Run sequential RSI iterations: python -B run_iteration.py --help."""
import argparse
import copy
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .analyzer import Analyzer, analyze_evidence, metric_summary
from .diagnosis.session import make_job as diagnosis_job
from .editor import make_job as editor_job
from .evaluator import evaluate, batch_problem
from .memory import record, search
from .references import freeze_context
from .search import add_node, select_parent
from .workspace import allocate, create_workspace, preflight_storage, remove_workspace
from runner.artifacts import path_reference, project_path, read_json, write_json
from .tools.config import load_config, role_settings, validate_config
from runner.console import error_summary, progress
from .tools.llm import ExperimentRuntime, call_session, load_settings, preflight
from .tools.sandbox import preflight_editor
from runner.matches import run_batch
from .tools.validate import validate
from runner.versions import Versions


def _load_models(project, config):
    """Resolve and preflight both roles once before collecting experiment evidence."""
    preflight_editor(config.get("editor_execution", {"mode": "local"}))
    settings = {role: load_settings(project, role_settings(config["model"], role))
                for role in ("diagnosis", "editor")}
    for model in settings.values():
        preflight(model)
    return settings


@dataclass
class IterationResult:
    """A round report and the evidence needed to continue its version chain."""
    report: dict
    baseline: dict | None = None
    candidate: dict | None = None
    children: list = field(default_factory=list)


class Run:
    def __init__(self, project, config, *, root=None, analyzer=None, model_settings=None, context=None, runtime=None):
        """Bind one iteration's fixed configuration; execution starts only in execute()."""
        self.project = Path(project).resolve()
        self.config = copy.deepcopy(config)
        validate_config(self.config)
        self.root = Path(root) if root is not None else self.project / "runtime/rsi"
        self.versions = Versions(self.project, self.project / "runtime/rsi")
        self.analyzer, self.model_settings = analyzer, model_settings
        self.context = context
        self.runtime = runtime
        self.started = time.monotonic()
        self.directory = allocate(self.root, "iterations")
        self.report = dict(iteration_id=self.directory.name, status="running", stage="prepare",
                           config=self.config, costs=dict(matches=0, sessions=[], wall_seconds=0), artifacts={})
        self.outcome = IterationResult(self.report)

    def save(self, name, value):
        """Persist a stage artifact and update the small iteration report."""
        path = self.directory / f"{name}.json"
        write_json(path, value)
        self.report["artifacts"][name] = path_reference(path)
        self.checkpoint(name)

    def checkpoint(self, stage):
        """Update progress without creating another stage file."""
        self.report["stage"] = stage
        self.report["costs"]["wall_seconds"] = time.monotonic() - self.started
        self._save_summary()

    def _save_summary(self):
        """Write stage summaries while keeping complete results in their own files."""
        summary = dict(self.report)
        summary.pop("config", None)
        if "diagnosis" in summary:
            diagnosis = summary["diagnosis"]
            summary["diagnosis"] = dict(status="repair_ready" if diagnosis.get("conclusion") else "unresolved",
                                        tasks=len((diagnosis.get("conclusion") or {}).get("tasks", [])))
        if "validation" in summary:
            validation = summary["validation"]
            summary["validation"] = dict(passed=validation["passed"], failed_checks=[
                check["name"] for check in validation["checks"] if not check["passed"]])
        if "evaluation" in summary:
            evaluation = summary["evaluation"]
            summary["evaluation"] = {key: evaluation[key] for key in ("decision", "reason")}
        write_json(self.directory / "iteration.json", summary)

    def remaining(self):
        """Enforce the overall wall budget between operations."""
        remaining = self.config["max_wall_seconds"] - (time.monotonic() - self.started)
        if remaining < 1:
            raise RuntimeError("Iteration wall budget exhausted")
        return remaining

    def batch(self, version, purpose):
        """Run the configured seed set in one bounded wave; failures also consume budget."""
        count = self.config["parallel_matches"]
        if self.report["costs"]["matches"] + count > self.config["max_matches"]:
            raise RuntimeError("Match budget exhausted")
        self.report["costs"]["matches"] += count
        remaining = self.remaining()
        conditions = [dict(self.config["matches"], seed=seed) for seed in range(1, count + 1)]
        name = self.runtime.next_name("batch")
        directory = self.directory / "batches" / name
        self.report["artifacts"][name] = path_reference(directory / "batch.json")
        self.checkpoint(name)
        progress(purpose, f"Starting {count} matches; parallel={count}")
        batch = run_batch(self.project, version, conditions, directory,
                          purpose, self.config["parallel_matches"], wall_budget=remaining)
        progress(purpose, "Matches finished; analyzing logs")
        evidence = analyze_evidence(self.analyzer, batch, directory / "batch.json", directory / "analysis.json")
        analysis = evidence["analysis"]
        self.report["artifacts"][name + "_analysis"] = path_reference(directory / "analysis.json")
        self.checkpoint(name + "_analysis")
        summary = analysis["summary"]
        outcomes = ", ".join(f"{name}={count}" for name, count in summary["outcomes"].items())
        events = sorted(summary["event_counts"].items(), key=lambda item: item[1], reverse=True)[:3]
        progress(purpose, f"Outcomes: {outcomes}; main events: " +
                 (", ".join(f"{name}={count}" for name, count in events) or "none"))
        return evidence

    def model(self, name, job):
        """Give each session its role's full limits and record usage for reporting."""
        settings = dict(self.model_settings[job["role"]])
        sessions = self.report["costs"]["sessions"]
        settings["wall_timeout"] = min(settings["wall_timeout"], self.remaining())
        job = dict(job, model=settings, iteration=path_reference(self.directory / "iteration.json"))
        if job["role"] == "editor":
            job["execution"] = self.config.get("editor_execution", {"mode": "local"})
        directory = self.runtime.root / "sessions" / self.runtime.next_name(job["role"])
        self.report["artifacts"][name + "_job"] = path_reference(directory / "job.json")
        self.report["artifacts"][name + "_response"] = path_reference(directory / "response.json")
        if job["role"] == "diagnosis":
            self.report["artifacts"]["diagnosis"] = path_reference(directory / "investigation.json")
        self.checkpoint(name)
        progress(name, f"Session started; timeout={settings['wall_timeout']:.0f}s")
        response = call_session(self.project, job, directory, runtime=self.runtime)
        usage = response.get("usage", {})
        sessions.append(dict(name=name, **usage))
        self.checkpoint(name)
        progress(name, f"Session finished: status={response['status']}; "
                      f"tokens={usage.get('tokens')}; cost={usage.get('cost')}")
        # Unknown usage after a failed worker cannot safely be treated as free.
        if response["status"] != "ok":
            raise RuntimeError(response["reason"])
        if usage.get("tokens") is None:
            raise RuntimeError("Session token usage unavailable; stop before another paid session")
        return response["value"]

    def finish(self, status, reason):
        """Retain all outcomes and write memory only when diagnosis actually ran."""
        if status in {"error", "interrupted"}:
            self.report["failed_stage"] = self.report["stage"]
            for branch in self.report.get("branches", []):
                if branch["status"] in {"editing", "evaluating"}:
                    branch.update(status=status, reason=reason)
        if "champion_before" in self.report:
            self.report["champion_after"] = (self.report.get("candidate") or {}).get(
                "version", self.report["champion_before"])
        self.report.update(status=status, reason=reason)
        if "diagnosis" in self.report:
            record(self.root, self.report)
        self.checkpoint("finished")
        progress("iteration", f"Finished: {status}; {error_summary(reason)}; elapsed={time.monotonic() - self.started:.1f}s")
        progress("iteration", f"Report: {path_reference(self.directory / 'iteration.json')}")
        return self.outcome

    def _overview(self, analyses):
        """Save the same compact task data used by the diagnosis conversation."""
        runs, events = [], []
        for batch in analyses:
            for run_id, report in batch["runs"].items():
                metrics = {name: metric_summary(name, report)
                           for name in ("workers", "minerals", "vespene", "supply_block_ratio", "army_supply")}
                outcome, coverage = report["outcome"], report["coverage"]
                workers, minerals, vespene = metrics["workers"], metrics["minerals"], metrics["vespene"]
                run_metrics = dict(
                    workers_first=workers["first"], workers_last=workers["last"],
                    minerals_mean=minerals["mean"], minerals_max=minerals["max"],
                    vespene_mean=vespene["mean"], vespene_max=vespene["max"],
                    supply_block_ratio=metrics["supply_block_ratio"]["mean"],
                    army_supply_last=metrics["army_supply"]["last"])
                runs.append(dict(
                    batch_id=batch["batch_id"], run_id=run_id, **report["conditions"],
                    outcome=outcome.get("outcome"), termination=outcome.get("termination"),
                    last_frame=coverage["last_frame"], execution_status=outcome.get("execution_status"),
                    integrity=coverage["integrity"], sample_ratio=coverage["sample_ratio"], metrics=run_metrics))
                seen = set()
                for event in report["events"]:
                    if event["kind"] in {"unit_created", "construction_started", "construction_complete"} or event["kind"] in seen:
                        continue
                    seen.add(event["kind"])
                    events.append(dict(batch_id=batch["batch_id"], run_id=run_id,
                        **event))
        options = self.config["diagnosis"]
        overview = dict(runs=runs, events=events,
            paths=dict(strategy="source/components/tactics/bc_rush.py", tasks="source/components/tasks/",
                       api="api/", knowledge="knowledge/"),
            limits={key: options[key] for key in ("query_policy", "max_queries", "max_tasks", "competing_hypotheses", "use_memory")})
        self.save("overview", overview)
        return overview

    def _diagnose(self, parent, batches, overview, analyses):
        """Freeze research inputs and run one diagnosis conversation."""
        conditions = [{k: r["conditions"][k] for k in ("map", "race", "difficulty")}
                      for a in analyses for r in a["runs"].values()]
        self.report["conditions"] = conditions[0] if conditions and all(c == conditions[0] for c in conditions) else {}
        context = self.context
        experiences = search(self.root, self.report["conditions"]) if self.config["diagnosis"]["use_memory"] else []
        progress("diagnosis", f"Preparing inputs; memory={len(experiences)}")
        memory_path = self.directory / "memory.json"
        write_json(memory_path, experiences)
        self.report["artifacts"]["memory"] = path_reference(memory_path)
        diagnosis = self.model("diagnosis", diagnosis_job(self.project, parent, batches, overview, context, self.config,
            analysis_paths=[self.report["artifacts"][b["batch_id"] + "_analysis"] for b in batches],
            memory_path=memory_path))
        self.report["diagnosis"] = diagnosis
        self.checkpoint("diagnosis")
        progress("diagnosis", f"Completed: {'repair_ready' if diagnosis.get('conclusion') else 'unresolved'}; "
                 f"queries={len(diagnosis.get('queries', []))}")
        for hypothesis in diagnosis.get("hypotheses", []):
            progress("diagnosis", f"{hypothesis['id']}: {hypothesis['explanation']}")
        return diagnosis

    def _edit_and_validate(self, parent, diagnosis, task, branch):
        """Edit one independent task, retaining the existing static-repair policy."""
        context = self.context
        slot = branch["slot"]
        candidate = create_workspace(self.versions, parent, self.directory / "iteration.json", task["id"],
                                     root=self.runtime.root, name=self.runtime.next_name("candidate"))
        branch["workspace"] = dict(path=path_reference(candidate.parent), status="retained")
        self.checkpoint("task_" + slot)
        feedback = None
        for attempt in range(self.config["max_repairs"] + 1):
            progress("editor", f"Attempt {attempt + 1}/{self.config['max_repairs'] + 1}")
            self.model(f"editor_{slot}_{attempt}", editor_job(candidate, diagnosis, task, feedback, context=context,
                evidence_path=self.report["artifacts"]["diagnosis"]))
            self.versions.verify(parent)
            feedback = validate(parent, candidate, timeout=min(30, self.remaining()),
                                patch_path=self.directory / f"patch_{slot}.diff")
            changed = next((check["changed"] for check in feedback["checks"] if check["name"] == "scope"), [])
            branch["changed_files"] = changed
            branch["patch"] = path_reference(self.directory / f"patch_{slot}.diff")
            progress("editor", "Changed files: " + (", ".join(changed) or "none"))
            for check in feedback["checks"]:
                progress("validation", f"{check['name']}: {'passed' if check['passed'] else 'failed'}")
            branch["validation_passed"] = feedback["passed"]
            self.save(f"validation_{slot}_{attempt}", feedback)
            if feedback["passed"]:
                child = self.versions.freeze(candidate)
                remove_workspace(self.runtime.root, candidate.parent)
                branch["workspace"]["status"] = "removed"
                return child, feedback
        return None, feedback

    def _branch_budget_problem(self):
        """Skip unstarted branches when the shared round budget is exhausted."""
        if self.report["costs"]["matches"] + self.config["parallel_matches"] > self.config["max_matches"]:
            return "Match budget exhausted"
        if time.monotonic() - self.started >= self.config["max_wall_seconds"]:
            return "Iteration wall budget exhausted"
        return None

    def execute(self, parent=None, baseline=None, *, champion=None, evidence_cache=None):
        """Expand independent tasks under shared budgets, retaining all valid evaluated children."""
        owns_runtime = self.runtime is None
        if owns_runtime:
            self.runtime = ExperimentRuntime(self.project, allocate(self.versions.root, "runtimes"))
        try:
            progress("iteration", f"Started: {path_reference(self.directory)}")
            if self.model_settings is None:
                preflight_storage(self.runtime.root)
                retained = list(self.versions.root.glob("runtimes/*/work/*/workspace.json"))
                retained.extend((self.versions.root / "work").glob("*/workspace.json"))
                if retained:
                    progress("prepare", f"Existing workspaces retained: {len(retained)}; inspect runtime/rsi")
                self.model_settings = _load_models(self.project, self.config)
            if self.context is None:
                self.context = freeze_context(self.project, self.versions.root / "refs", shared=True)
            self.report["context"] = dict(id=self.context.name, source_root=path_reference(self.context))
            if self.analyzer is None:
                self.analyzer = Analyzer(self.context)
            parent = parent or self.versions.freeze(self.project)
            self.versions.verify(parent)
            self.report["parent"] = parent
            self.checkpoint("prepare")
            progress("prepare", f"Parent: {parent['version'][:12]}")
            if baseline is None:
                baseline = self.batch(parent, "parent")
            if baseline["batch"]["version"] != parent["version"]:
                raise ValueError("Parent batch version mismatch")
            self.outcome.baseline = baseline
            batch, analysis = baseline["batch"], baseline["analysis"]
            problem = batch_problem(analysis)
            if problem:
                return self.finish("error", "Parent batch unavailable: " + problem["reason"])
            self.report["parent_batch"] = baseline["batch_path"]
            self.report["artifacts"][batch["batch_id"] + "_analysis"] = baseline["analysis_path"]
            overview = self._overview([analysis])
            diagnosis = self._diagnose(parent, [batch], overview, [analysis])
            if not diagnosis.get("conclusion"):
                return self.finish("unresolved", diagnosis.get("reason", "Diagnosis unresolved"))
            incumbent = champion or baseline
            cache = dict(evidence_cache or {})
            cache[parent["version"]] = baseline
            cache[incumbent["batch"]["version"]] = incumbent
            self.report["champion_before"] = incumbent["batch"]["version"]
            self.report["branches"] = []
            for index, task in enumerate(diagnosis["conclusion"]["tasks"], 1):
                slot = f"t{index:02d}"
                branch = dict(task_id=task["id"], slot=slot, status="editing")
                self.report["branches"].append(branch)
                budget_problem = self._branch_budget_problem()
                if budget_problem:
                    branch.update(status="skipped", reason=budget_problem)
                    self.checkpoint("budget")
                    continue
                self.checkpoint("task_" + task["id"])
                child, validation = self._edit_and_validate(parent, diagnosis, task, branch)
                if not validation["passed"]:
                    branch.update(status="reject", reason="Candidate static validation failed")
                    self.checkpoint("task_" + task["id"])
                    continue
                branch.update(candidate=child, status="evaluating")
                self.checkpoint("task_" + task["id"])
                evidence = cache.get(child["version"])
                branch["reused_evidence"] = evidence is not None
                if evidence is None:
                    evidence = self.batch(child, "candidate_" + slot)
                branch["batch"] = evidence["batch_path"]
                evaluation = evaluate(analysis, evidence["analysis"])
                name = "evaluation_" + slot
                self.save(name, evaluation)
                branch["evaluation"] = self.report["artifacts"][name]
                branch.update(status=evaluation["decision"], reason=evaluation["reason"])
                progress("evaluation", f"Task {task['id']}: {evaluation['decision']}; {evaluation['reason']}")
                if not evaluation["comparable"]:
                    if evaluation.get("code") == "candidate_bot_error":
                        continue
                    branch["status"] = "error"
                    return self.finish("error", evaluation["reason"])
                cache[child["version"]] = evidence
                self.outcome.children.append(dict(version=child, evidence=evidence, task_id=task["id"],
                                                  decision=evaluation["decision"]))
                if incumbent["batch"]["version"] == parent["version"]:
                    promotion = evaluation
                    branch["promotion"] = branch["evaluation"]
                else:
                    promotion = evaluate(incumbent["analysis"], evidence["analysis"])
                    self.save("promotion_" + slot, promotion)
                    branch["promotion"] = self.report["artifacts"]["promotion_" + slot]
                if not promotion["comparable"]:
                    return self.finish("error", promotion["reason"])
                branch["promoted"] = promotion["decision"] == "accept"
                if branch["promoted"]:
                    incumbent = evidence
                    self.outcome.candidate = evidence
                    self.report["candidate"] = child
                    self.report["candidate_batch"] = evidence["batch_path"]
                self.checkpoint("task_" + task["id"])
            if self.outcome.candidate:
                return self.finish("accept", "An evaluated child improved the experiment champion")
            return self.finish("reject", "No child improved the experiment champion")
        except KeyboardInterrupt:
            return self.finish("interrupted", "Cancelled without automatic retry")
        except Exception as exc:
            return self.finish("error", f"{type(exc).__name__}: {exc}")
        finally:
            if owns_runtime:
                self.runtime.close()


def run_experiment(project, config, *, parent=None, rounds=1):
    """Grow an isolated archive while retaining a separately evaluated champion."""
    validate_config(config)
    if not isinstance(rounds, int) or rounds < 1:
        raise ValueError("rounds must be a positive integer")
    project = Path(project).resolve()
    versions = Versions(project, project / "runtime/rsi")
    current = versions.freeze(Path(parent).resolve() if parent is not None else project)
    root = allocate(versions.root, "experiments")
    root.mkdir()
    report = dict(experiment_id=root.name, status="running", config=copy.deepcopy(config),
                  rounds=rounds, initial_parent=current, current_parent=current, iterations=[])
    baseline = analyzer = model_settings = context = None
    archive = dict(nodes={}, edges=[])
    evidence_cache = {}
    report["archive"] = path_reference(root / "archive.json")
    started = time.monotonic()
    runtime = ExperimentRuntime(project, allocate(versions.root, "runtimes", name=root.name))
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
        if config.get("parent_batch"):
            path = project_path(config["parent_batch"])
            batch = read_json(path)
            if batch["version"] != current["version"] or batch["purpose"] == "holdout":
                raise ValueError("parent_batch must belong to the selected parent and not be holdout data")
            expected = [dict(config["matches"], seed=seed) for seed in range(1, config["parallel_matches"] + 1)]
            actual = []
            for run_id, run_path in batch["runs"].items():
                manifest = read_json(project_path(run_path) / "manifest.json")
                if manifest["run_id"] != run_id or manifest["version"] != current["version"] or manifest["purpose"] == "holdout":
                    raise ValueError("Reused run identity/version/purpose mismatch")
                actual.append(manifest)
            keys = ("map", "race", "difficulty", "seed", "game_time_limit")
            signature = lambda items: sorted(json.dumps({k: r[k] for k in keys}, sort_keys=True) for r in items)
            if signature(actual) != signature(expected):
                raise ValueError("parent_batch conditions differ from configured batch")
            context = freeze_context(project, versions.root / "refs", shared=True)
            analyzer = Analyzer(context)
            analysis_path = root / "batches" / batch["batch_id"] / "analysis.json"
            baseline = analyze_evidence(analyzer, batch, path, analysis_path)
        for number in range(1, rounds + 1):
            progress("experiment", f"Round {number}/{rounds}")
            run = Run(project, config, root=root, analyzer=analyzer, model_settings=model_settings, context=context,
                      runtime=runtime)
            parent_key = select_parent(archive, current["version"], number) if archive["nodes"] else current["version"]
            selected = dict(version=parent_key, source_root=archive["nodes"][parent_key]["source_root"]) if archive["nodes"] else current
            selected_evidence = evidence_cache.get(parent_key, baseline)
            outcome = run.execute(selected, selected_evidence, champion=baseline, evidence_cache=evidence_cache)
            result = outcome.report
            analyzer, model_settings = run.analyzer, run.model_settings
            context = run.context
            if context is not None:
                report["context"] = dict(id=context.name, source_root=path_reference(context))
            if outcome.baseline and batch_problem(outcome.baseline["analysis"]) is None:
                add_node(archive, selected, outcome.baseline)
                evidence_cache[parent_key] = outcome.baseline
                archive["nodes"][parent_key]["expansions"] += 1
                baseline = baseline or outcome.baseline
            for child in outcome.children:
                add_node(archive, child["version"], child["evidence"])
                evidence_cache.setdefault(child["version"]["version"], child["evidence"])
                archive["edges"].append(dict(parent=parent_key, child=child["version"]["version"],
                    iteration_id=result["iteration_id"], task_id=child["task_id"], decision=child["decision"],
                    report=path_reference(run.directory / "iteration.json")))
            report["iterations"].append(dict(round=number, iteration_id=result["iteration_id"],
                status=result["status"], reason=result["reason"], parent=result.get("parent"),
                candidate=result.get("candidate"), costs=result["costs"],
                report=path_reference(run.directory / "iteration.json")))
            if outcome.candidate is not None:
                current, baseline = result["candidate"], outcome.candidate
            report["current_parent"] = current
            report["current_batch"] = baseline.get("batch_path") if baseline else None
            report["status"] = result["status"] if result["status"] in {"error", "interrupted"} else "running"
            if report["status"] != "running":
                report["reason"] = result["reason"]
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
        runtime.close()
    save_report()
    progress("experiment", f"Finished: {report['status']}; report={path_reference(root / 'experiment.json')}")
    return report


def main():
    """Expose only the initial source, round count and experiment configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--parent", type=Path, help="Source directory containing bot/; defaults to this repository")
    parser.add_argument("--rounds", type=int, default=1)
    args = parser.parse_args()
    try:
        report = run_experiment(Path(__file__).resolve().parents[1], load_config(args.config),
                                parent=args.parent, rounds=args.rounds)
    except (ValueError, KeyError, OSError) as exc:
        print(json.dumps(dict(status="configuration_error", reason=str(exc)), ensure_ascii=False))
        return 2
    return 130 if report["status"] == "interrupted" else 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
