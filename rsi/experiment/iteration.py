"""Execute one RSI round and its independent candidate tasks."""
import time
from dataclasses import dataclass, field

from ..analysis.analyzer import metric_summary
from ..analysis.evidence import analyze_evidence
from ..evaluator import evaluate, batch_problem
from .memory import record, search
from .workspace import allocate, create_workspace, remove_workspace
from ..tools.validate import validate
from ..tools.llm import SessionError
from runner.artifacts import path_reference, write_json
from runner.console import error_summary, progress
from runner.matches import run_batch
from runner.versions import SnapshotError


@dataclass
class IterationResult:
    """A round report and the evidence needed to continue its version chain."""
    report: dict
    baseline: dict | None = None
    candidate: dict | None = None
    children: list = field(default_factory=list)


class Run:
    def __init__(self, experiment):
        """Bind shared experiment resources and keep only this round's mutable state."""
        self.experiment = experiment
        self.project, self.config, self.root = experiment.project, experiment.config, experiment.root
        self.versions, self.runtime = experiment.versions, experiment.runtime
        self.started = time.monotonic()
        self.directory = allocate(self.root, "iterations")
        self.report = dict(iteration_id=self.directory.name, status="running", stage="prepare",
                           costs=dict(matches=0, sessions=[], wall_seconds=0), artifacts={})
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
        if "diagnosis" in summary:
            diagnosis = summary["diagnosis"]
            summary["diagnosis"] = dict(status="repair_ready" if diagnosis.get("conclusion") else "unresolved",
                                        tasks=len((diagnosis.get("conclusion") or {}).get("tasks", [])))
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
        evidence = analyze_evidence(self.experiment.analyzer, batch, directory / "batch.json", directory / "analysis.json")
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
        settings = dict(self.experiment.model_settings[job["role"]])
        sessions = self.report["costs"]["sessions"]
        settings["wall_timeout"] = min(settings["wall_timeout"], self.remaining())
        job = dict(job, model=settings, iteration=path_reference(self.directory / "iteration.json"))
        if job["role"] == "editor":
            job["execution"] = self.config["editor_execution"]
        directory = self.runtime.root / "sessions" / self.runtime.next_name(job["role"])
        self.report["artifacts"][name + "_job"] = path_reference(directory / "job.json")
        self.report["artifacts"][name + "_response"] = path_reference(directory / "response.json")
        if job["role"] == "diagnosis":
            self.report["artifacts"]["diagnosis"] = path_reference(directory / "investigation.json")
        self.checkpoint(name)
        progress(name, f"Session started; timeout={settings['wall_timeout']:.0f}s")
        response = self.runtime.call_session(job, directory)
        usage = response.get("usage", {})
        sessions.append(dict(name=name, **usage))
        self.checkpoint(name)
        progress(name, f"Session finished: status={response['status']}; "
                      f"tokens={usage.get('tokens')}; cost={usage.get('cost')}")
        if response["status"] != "ok":
            raise SessionError(response["reason"], fatal=response.get("fatal", False))
        if usage.get("tokens") is None:
            raise SessionError("Session token usage unavailable; stop before another paid session", fatal=True)
        return response["value"]

    def finish(self, status, reason, *, fatal=False):
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
        if fatal:
            self.report["fatal"] = True
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
        from ..diagnosis.session import make_job as diagnosis_job
        conditions = [{k: r["conditions"][k] for k in ("map", "race", "difficulty")}
                      for a in analyses for r in a["runs"].values()]
        self.report["conditions"] = conditions[0] if conditions and all(c == conditions[0] for c in conditions) else {}
        context = self.experiment.context
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
                 f"queries={diagnosis.get('query_count', 0)}")
        for hypothesis in diagnosis.get("hypotheses", []):
            progress("diagnosis", f"{hypothesis['id']}: {hypothesis['explanation']}")
        return diagnosis

    def _edit_and_validate(self, parent, diagnosis, task, branch):
        """Edit one independent task, retaining the existing static-repair policy."""
        from ..editor import make_job as editor_job
        context = self.experiment.context
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

    def _evaluate_candidate(self, child, parent_analysis, branch, cache):
        """Reuse or collect candidate evidence and save its comparison with the parent."""
        slot = branch["slot"]
        evidence = cache.get(child["version"]) or self.experiment.load_evidence(child["version"])
        branch["reused_evidence"] = evidence is not None
        if evidence is None:
            evidence = self.batch(child, "candidate_" + slot)
        branch["batch"] = evidence["batch_path"]
        evaluation = evaluate(parent_analysis, evidence["analysis"])
        name = "evaluation_" + slot
        self.save(name, evaluation)
        branch["evaluation"] = self.report["artifacts"][name]
        branch.update(status=evaluation["decision"], reason=evaluation["reason"])
        progress("evaluation", f"Task {branch['task_id']}: {evaluation['decision']}; {evaluation['reason']}")
        return evidence, evaluation

    def _branch_budget_problem(self):
        """Skip unstarted branches when the shared round budget is exhausted."""
        if self.report["costs"]["matches"] + self.config["parallel_matches"] > self.config["max_matches"]:
            return "Match budget exhausted"
        if time.monotonic() - self.started >= self.config["max_wall_seconds"]:
            return "Iteration wall budget exhausted"
        return None

    def execute(self, parent=None, parent_evidence=None, *, champion_evidence=None):
        """Expand independent tasks under shared budgets, retaining all valid evaluated children."""
        try:
            progress("iteration", f"Started: {path_reference(self.directory)}")
            self.report["context"] = dict(id=self.experiment.context.name, source_root=path_reference(self.experiment.context))
            parent = parent or self.versions.freeze(self.project)
            self.versions.verify(parent)
            self.report["parent"] = parent
            self.checkpoint("prepare")
            progress("prepare", f"Parent: {parent['version'][:12]}")
            if parent_evidence is None:
                parent_evidence = self.batch(parent, "parent")
            else:
                self.experiment.restore_evidence(parent_evidence)
            if parent_evidence["batch"]["version"] != parent["version"]:
                raise ValueError("Parent batch version mismatch")
            self.outcome.baseline = parent_evidence
            batch, analysis = parent_evidence["batch"], parent_evidence["analysis"]
            problem = batch_problem(analysis)
            if problem:
                return self.finish("error", "Parent batch unavailable: " + problem["reason"])
            self.experiment.remember(parent, parent_evidence)
            self.report["parent_batch"] = parent_evidence["batch_path"]
            self.report["artifacts"][batch["batch_id"] + "_analysis"] = parent_evidence["analysis_path"]
            overview = self._overview([analysis])
            diagnosis = self._diagnose(parent, [batch], overview, [analysis])
            if not diagnosis.get("conclusion"):
                return self.finish("unresolved", diagnosis.get("reason", "Diagnosis unresolved"))
            champion_evidence = champion_evidence or parent_evidence
            cache = {parent["version"]: parent_evidence, champion_evidence["batch"]["version"]: champion_evidence}
            self.report["champion_before"] = champion_evidence["batch"]["version"]
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
                try:
                    child, validation = self._edit_and_validate(parent, diagnosis, task, branch)
                except SessionError as exc:
                    if exc.fatal:
                        raise
                    branch.update(status="error", reason=str(exc))
                    self.checkpoint("task_" + task["id"])
                    continue
                if not validation["passed"]:
                    branch.update(status="reject", reason="Candidate static validation failed")
                    self.checkpoint("task_" + task["id"])
                    continue
                branch.update(candidate=child, status="evaluating")
                self.checkpoint("task_" + task["id"])
                evidence, evaluation = self._evaluate_candidate(child, analysis, branch, cache)
                if not evaluation["comparable"]:
                    if evaluation.get("code") == "candidate_bot_error":
                        continue
                    branch["status"] = "error"
                    return self.finish("error", evaluation["reason"])
                cache[child["version"]] = evidence
                self.experiment.remember(child, evidence)
                self.outcome.children.append(dict(version=child, task_id=task["id"],
                                                  decision=evaluation["decision"]))
                if champion_evidence["batch"]["version"] == parent["version"]:
                    promotion = evaluation
                    branch["promotion"] = branch["evaluation"]
                else:
                    promotion = evaluate(champion_evidence["analysis"], evidence["analysis"])
                    self.save("promotion_" + slot, promotion)
                    branch["promotion"] = self.report["artifacts"]["promotion_" + slot]
                if not promotion["comparable"]:
                    return self.finish("error", promotion["reason"])
                branch["promoted"] = promotion["decision"] == "accept"
                if branch["promoted"]:
                    champion_evidence = evidence
                    self.outcome.candidate = evidence
                    self.report["candidate"] = child
                    self.report["candidate_batch"] = evidence["batch_path"]
                self.checkpoint("task_" + task["id"])
            if self.outcome.candidate:
                return self.finish("accept", "An evaluated child improved the experiment champion")
            if any(branch["status"] == "error" for branch in self.report["branches"]):
                return self.finish("error", "Candidate model sessions failed; see branch errors")
            return self.finish("reject", "No child improved the experiment champion")
        except KeyboardInterrupt:
            return self.finish("interrupted", "Cancelled without automatic retry")
        except Exception as exc:
            return self.finish("error", f"{type(exc).__name__}: {exc}",
                               fatal=isinstance(exc, SnapshotError) or getattr(exc, "fatal", False))

