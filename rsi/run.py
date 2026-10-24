"""Run sequential RSI iterations: python -B run_iteration.py --help."""
import argparse
import copy
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .analyzer import Analyzer, metric_summary, save_batch_analysis
from .diagnosis.session import make_job as diagnosis_job
from .editor import make_job as editor_job
from .evaluator import evaluate, evidence_problem
from .memory import record, search
from .retriever import freeze_context
from .tools.artifacts import path_reference, project_path, read_json, write_json
from .tools.config import load_config, validate_config
from .tools.console import error_summary, progress
from .tools.llm import call_session, load_settings, preflight
from .tools.matches import MATCH_START_INTERVAL, run_batch
from .tools.validate import validate
from .tools.versions import Versions


def _load_models(project, config):
    """Resolve and preflight both roles once before collecting experiment evidence."""
    shared = {k: v for k, v in config["model"].items() if k not in {"diagnosis", "editor"}}
    settings = {role: load_settings(project, dict(shared, **config["model"][role]))
                for role in ("diagnosis", "editor")}
    for model in settings.values():
        preflight(model)
    return settings


class Run:
    def __init__(self, project, config, *, root=None, analyzer=None, model_settings=None):
        """Bind one iteration's fixed configuration; execution starts only in execute()."""
        self.project = Path(project).resolve()
        self.config = copy.deepcopy(config)
        validate_config(self.config)
        self.root = Path(root) if root is not None else self.project / "runtime/rsi"
        self.versions = Versions(self.project, self.project / "runtime/rsi")
        self.parent_data = self.candidate_data = None
        self.analyzer, self.model_settings = analyzer, model_settings
        self.started = time.monotonic()
        self.directory = self.root / "iterations" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:8])
        self.report = dict(iteration_id=self.directory.name, status="running", stage="prepare",
                           config=self.config, costs=dict(matches=0, sessions=[], wall_seconds=0), artifacts={})

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
            summary["diagnosis"] = dict(status="repair_ready" if diagnosis.get("conclusion") else "unresolved")
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
        remaining -= MATCH_START_INTERVAL * (count - 1)
        if remaining < 1:
            raise RuntimeError("Insufficient wall budget for staggered match starts")
        conditions = [dict(self.config["matches"], seed=seed,
                           wall_timeout=min(self.config["matches"]["wall_timeout"], int(remaining)))
                      for seed in range(1, count + 1)]
        name = purpose + "_" + uuid4().hex[:8]
        directory = self.directory / "batches" / name
        self.report["artifacts"][name] = path_reference(directory / "batch.json")
        self.checkpoint(name)
        progress(purpose, f"Starting {count} matches; parallel={count}")
        batch = run_batch(self.project, version, conditions, directory,
                          purpose, self.config["parallel_matches"])
        progress(purpose, "Matches finished; analyzing logs")
        analysis = self.analyzer.add_batch(batch, persist=False)
        save_batch_analysis(directory / "analysis.json", analysis)
        self.report["artifacts"][name + "_analysis"] = path_reference(directory / "analysis.json")
        self.checkpoint(name + "_analysis")
        summary = analysis["summary"]
        outcomes = ", ".join(f"{name}={count}" for name, count in summary["outcomes"].items())
        events = sorted(summary["event_counts"].items(), key=lambda item: item[1], reverse=True)[:3]
        progress(purpose, f"Outcomes: {outcomes}; main events: " +
                 (", ".join(f"{name}={count}" for name, count in events) or "none"))
        return batch, analysis

    def model(self, name, job):
        """Allocate remaining shared model budget and charge every completed/failed session."""
        settings = dict(self.model_settings[job["role"]])
        sessions = self.report["costs"]["sessions"]
        used_tokens = sum(session.get("tokens") or 0 for session in sessions)
        settings["max_total_tokens"] = min(settings["max_total_tokens"],
            self.config["model"]["max_total_tokens"] - used_tokens)
        known_cost = sum(session.get("cost") or 0 for session in sessions)
        settings["max_cost"] = min(settings["max_cost"],
            self.config["model"]["max_cost"] - known_cost)
        if settings["max_total_tokens"] <= 0 or settings["max_cost"] <= 0:
            raise RuntimeError("Iteration model budget exhausted")
        settings["wall_timeout"] = min(settings["wall_timeout"], self.remaining())
        job = dict(job, model=settings)
        directory = self.directory / "sessions" / name
        self.report["artifacts"][name + "_job"] = path_reference(directory / "job.json")
        self.report["artifacts"][name + "_response"] = path_reference(directory / "response.json")
        if job["role"] == "diagnosis":
            self.report["artifacts"]["diagnosis"] = path_reference(directory / "investigation.json")
        self.checkpoint(name)
        progress(name, f"Model session started; timeout={settings['wall_timeout']:.0f}s")
        response = call_session(self.project, job, directory)
        usage = response.get("usage", {})
        sessions.append(dict(name=name, **usage))
        self.checkpoint(name)
        progress(name, f"Model session finished: status={response['status']} "
                      f"tokens={usage.get('tokens')} cost={usage.get('cost')}")
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
        self.report.update(status=status, reason=reason)
        if "diagnosis" in self.report:
            record(self.root, self.report)
        self.checkpoint("finished")
        progress("iteration", f"Finished: {status}; {error_summary(reason)}; elapsed={time.monotonic() - self.started:.1f}s")
        progress("iteration", f"Report: {path_reference(self.directory / 'iteration.json')}")
        return self.report

    def _overview(self, analyses):
        """Save the same compact task data used by the diagnosis conversation."""
        runs, events = [], []
        for batch in analyses:
            for run_id, report in batch["runs"].items():
                metrics = {name: metric_summary(name, report["metrics"].get(name, []))
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
                    duration_seconds=coverage["duration_seconds"], execution_status=outcome.get("execution_status"),
                    integrity=coverage["integrity"], sample_ratio=coverage["sample_ratio"], metrics=run_metrics))
                seen = set()
                for event in report["events"]:
                    if event["kind"] in {"unit_created", "construction_started", "construction_complete"} or event["kind"] in seen:
                        continue
                    seen.add(event["kind"])
                    events.append(dict(batch_id=batch["batch_id"], run_id=run_id,
                        **{key: event[key] for key in ("kind", "start", "end", "details")}))
        options = self.config["diagnosis"]
        overview = dict(runs=runs, events=events,
            paths=dict(strategy="source/components/tactics/bc_rush.py", tasks="source/components/tasks/",
                       api="api/", knowledge="knowledge/"),
            limits={key: options[key] for key in ("query_policy", "max_queries", "competing_hypotheses", "use_memory")})
        self.save("overview", overview)
        return overview

    def _diagnose(self, parent, batches, overview, analyses):
        """Freeze research inputs and run one diagnosis conversation."""
        conditions = [{k: r["conditions"][k] for k in ("map", "race", "difficulty")}
                      for a in analyses for r in a["runs"].values()]
        self.report["conditions"] = conditions[0] if conditions and all(c == conditions[0] for c in conditions) else {}
        context = self.root / "context"
        experiences = search(self.root, self.report["conditions"]) if self.config["diagnosis"]["use_memory"] else []
        progress("diagnosis", f"Preparing inputs; memory_items={len(experiences)}")
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

    def _edit_and_validate(self, parent, diagnosis):
        """Edit and validate a candidate, retrying only repairable Bot failures."""
        context = self.root / "context"
        candidate = self.versions.candidate(parent, self.directory / "candidate")
        feedback = None
        for attempt in range(self.config["max_repairs"] + 1):
            progress("editor", f"Attempt {attempt + 1}/{self.config['max_repairs'] + 1}")
            self.model(f"editor_{attempt}", editor_job(candidate, diagnosis, feedback, context=context))
            self.versions.verify(parent)
            feedback = validate(parent, candidate, timeout=min(30, self.remaining()),
                                patch_path=self.directory / "patch.diff")
            changed = next((check["changed"] for check in feedback["checks"] if check["name"] == "scope"), [])
            self.report["changed_files"] = changed
            self.report["artifacts"]["patch"] = path_reference(self.directory / "patch.diff")
            progress("editor", "Changed files: " + (", ".join(changed) or "none"))
            for check in feedback["checks"]:
                progress("validation", f"{check['name']}: {'passed' if check['passed'] else 'failed'}")
            self.report["validation"] = feedback
            self.save(f"validation_{attempt}", feedback)
            if feedback["passed"]:
                return self.versions.freeze(candidate), feedback
        return None, feedback

    def execute(self, parent=None, baseline=None):
        """Reuse the parent batch; run only one candidate batch after static checks."""
        try:
            progress("iteration", f"Started: {path_reference(self.directory)}")
            if self.model_settings is None:
                self.model_settings = _load_models(self.project, self.config)
            context = self.root / "context"
            if not context.exists():
                freeze_context(self.project, context)
            if self.analyzer is None:
                self.analyzer = Analyzer(context)
            parent = parent or self.versions.freeze(self.project)
            self.versions.verify(parent)
            self.report["parent"] = parent
            self.checkpoint("prepare")
            progress("prepare", f"Parent: {parent['version'][:12]}")
            if baseline is None:
                batch, analysis = self.batch(parent, "parent")
                baseline = dict(batch=batch, analysis=analysis,
                                analysis_path=self.report["artifacts"][batch["batch_id"] + "_analysis"],
                                batch_path=path_reference(self.directory / "batches" / batch["batch_id"] / "batch.json"))
            if baseline["batch"]["version"] != parent["version"]:
                raise ValueError("Parent batch version mismatch")
            self.parent_data = baseline
            batch, analysis = baseline["batch"], baseline["analysis"]
            problem = evidence_problem(analysis, analysis)
            if problem:
                return self.finish("error", "Parent batch unavailable: " + problem)
            self.report["parent_batch"] = baseline["batch_path"]
            self.report["artifacts"][batch["batch_id"] + "_analysis"] = baseline["analysis_path"]
            overview = self._overview([analysis])
            diagnosis = self._diagnose(parent, [batch], overview, [analysis])
            if not diagnosis.get("conclusion"):
                return self.finish("unresolved", diagnosis.get("reason", "Diagnosis unresolved"))
            child, validation = self._edit_and_validate(parent, diagnosis)
            if not validation["passed"]:
                return self.finish("reject", "Candidate static validation failed")
            self.report["candidate"] = child
            candidate_batch, candidate_analysis = self.batch(child, "candidate")
            self.candidate_data = dict(batch=candidate_batch, analysis=candidate_analysis,
                analysis_path=self.report["artifacts"][candidate_batch["batch_id"] + "_analysis"],
                batch_path=path_reference(self.directory / "batches" / candidate_batch["batch_id"] / "batch.json"))
            self.report["candidate_batch"] = path_reference(self.directory / "batches" / candidate_batch["batch_id"] / "batch.json")
            evaluation = evaluate(analysis, candidate_analysis)
            self.report["evaluation"] = evaluation
            self.save("evaluation", evaluation)
            progress("evaluation", f"Decision: {evaluation['decision']}; reason={evaluation['reason']}")
            if not evaluation["comparable"] and evaluation["reason"] != "Candidate Bot execution failed":
                return self.finish("error", evaluation["reason"])
            return self.finish(evaluation["decision"], evaluation["reason"])
        except KeyboardInterrupt:
            return self.finish("interrupted", "Cancelled without automatic retry")
        except Exception as exc:
            return self.finish("error", f"{type(exc).__name__}: {exc}")


def run_experiment(project, config, *, parent=None, rounds=1):
    """Run one isolated chain, retaining accepted code and its already collected evidence."""
    validate_config(config)
    if not isinstance(rounds, int) or rounds < 1:
        raise ValueError("rounds must be a positive integer")
    project = Path(project).resolve()
    versions = Versions(project, project / "runtime/rsi")
    current = versions.freeze(Path(parent).resolve() if parent is not None else project)
    root = project / "runtime/rsi/experiments" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:8])
    report = dict(experiment_id=root.name, status="running", config=copy.deepcopy(config),
                  rounds=rounds, initial_parent=current, current_parent=current, iterations=[])
    baseline = analyzer = model_settings = None
    started = time.monotonic()

    def save_report():
        """Keep a compact round index; complete round details live in iteration.json."""
        iterations = [dict(round=r["round"], iteration_id=r["iteration_id"], status=r["status"],
                           parent=(r["parent"] or {}).get("version"),
                           candidate=(r["candidate"] or {}).get("version"), report=r["report"])
                      for r in report["iterations"]]
        report["costs"] = dict(matches=sum(r["costs"]["matches"] for r in report["iterations"]),
                              wall_seconds=time.monotonic() - started)
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
            context = root / "context"
            freeze_context(project, context)
            analyzer = Analyzer(context)
            analysis = analyzer.add_batch(batch, persist=False)
            analysis_path = root / "batches" / batch["batch_id"] / "analysis.json"
            save_batch_analysis(analysis_path, analysis)
            baseline = dict(batch=batch, analysis=analysis, analysis_path=path_reference(analysis_path),
                            batch_path=path_reference(path))
        for number in range(1, rounds + 1):
            progress("experiment", f"Round {number}/{rounds}")
            run = Run(project, config, root=root, analyzer=analyzer, model_settings=model_settings)
            result = run.execute(current, baseline)
            analyzer, model_settings = run.analyzer, run.model_settings
            baseline = run.parent_data or baseline
            report["iterations"].append(dict(round=number, iteration_id=result["iteration_id"],
                status=result["status"], reason=result["reason"], parent=result.get("parent"),
                candidate=result.get("candidate"), costs=result["costs"],
                report=path_reference(run.directory / "iteration.json")))
            if result["status"] == "accept":
                current, baseline = result["candidate"], run.candidate_data
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
