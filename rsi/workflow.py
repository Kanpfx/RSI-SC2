"""Execute one RSI round and its independent candidate tasks."""
import time
import copy
from importlib import import_module
from dataclasses import dataclass, field

from runtime.matches.evidence import analyze_evidence
from rsi.evaluation import batch_problem, conditions as conditions_for_evaluation, sampling_conditions
from rsi.memory import record, search
from rsi.contracts import Problem, ROLE_INPUTS, investigation_context
from runtime.workspace.candidates import allocate, create_workspace, remove_workspace
from runtime.workspace.checks import validate_sampling
from runtime.models.sessions import SessionError
from runtime.workspace.storage import path_reference, project_path, read_json, write_json
from runtime.console import error_summary, progress
from runtime.matches.runner import run_batch
from runtime.workspace.versions import SnapshotError


@dataclass
class IterationResult:
    """A round report and its evaluated children."""
    report: dict
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
        self.analysis_records = {}

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
            summary["diagnosis"] = dict(status="supported", mechanism=diagnosis["mechanism"])
        write_json(self.directory / "iteration.json", summary)

    def remaining(self):
        """Enforce the overall wall budget between operations."""
        remaining = self.config["max_wall_seconds"] - (time.monotonic() - self.started)
        if remaining < 1:
            raise RuntimeError("Iteration wall budget exhausted")
        return remaining

    def batch(self, version, purpose, *, match_set=None):
        """Run the configured independent repetitions in one bounded wave; failures also consume budget."""
        match_set = conditions_for_evaluation(self.config) if match_set is None else match_set
        count = len(match_set)
        if self.report["costs"]["matches"] + count > self.config["max_matches"]:
            raise RuntimeError("Match budget exhausted")
        self.report["costs"]["matches"] += count
        remaining = self.remaining()
        name = self.runtime.next_name("batch")
        directory = self.directory / "batches" / name
        self.report["artifacts"][name] = path_reference(directory / "batch.json")
        self.checkpoint(name)
        parallel = min(count, self.config["match_parallelism"])
        progress(purpose, f"Starting {count} matches; parallel={parallel}")
        batch = run_batch(self.project, version, match_set, directory,
                          purpose, parallel, wall_budget=remaining)
        progress(purpose, "Matches finished; analyzing logs")
        evidence = analyze_evidence(batch, directory / "batch.json", directory / "evidence.json")
        analysis = evidence["analysis"]
        self.report["artifacts"][name + "_analysis"] = path_reference(directory / "evidence.json")
        self.checkpoint(name + "_analysis")
        summary = analysis["summary"]
        outcomes = ", ".join(f"{name}={count}" for name, count in summary["outcomes"].items())
        progress(purpose, f"Outcomes: {outcomes}")
        return evidence

    def model(self, name, job, *, on_sampling=None):
        """Give each session its role's full limits and record usage for reporting."""
        settings = dict(self.experiment.model_settings[job["role"]])
        sessions = self.report["costs"]["sessions"]
        settings["wall_timeout"] = min(settings["wall_timeout"], self.remaining())
        job = dict(job, model=settings, wall_remaining=self.remaining(),
                   iteration=path_reference(self.directory / "iteration.json"))
        directory = self.runtime.root / "sessions" / self.runtime.next_name(job["role"])
        self.report["artifacts"][name + "_job"] = path_reference(directory / "job.json")
        self.report["artifacts"][name + "_response"] = path_reference(directory / "response.json")
        self.checkpoint(name)
        progress(name, f"Session started; timeout={settings['wall_timeout']:.0f}s")
        response = self.runtime.call_session(job, directory, on_sampling=on_sampling)
        if "execution_path" in response:
            self.analysis_records[job["role"]] = read_json(project_path(response["execution_path"]))["analyses"]
            self.report["artifacts"][name + "_execution"] = response["execution_path"]
        if "result_path" in response:
            self.report["artifacts"][name] = response["result_path"]
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
        """Retain all outcomes and record any research performed."""
        if status in {"error", "interrupted"}:
            self.report["failed_stage"] = self.report["stage"]
            for branch in self.report.get("branches", []):
                if branch["status"] in {"editing", "evaluating"}:
                    branch.update(status=status, reason=reason)
        observation = self.report.get("observation_workspace")
        if status not in {"error", "interrupted"} and observation and observation["status"] == "retained":
            remove_workspace(self.runtime.root, project_path(observation["path"]))
            observation["status"] = "removed"
        self.report.update(status=status, reason=reason)
        if fatal:
            self.report["fatal"] = True
        if "research" in self.report["artifacts"]:
            record(self.root, self.report)
        self.checkpoint("finished")
        progress("iteration", f"Finished: {status}; {error_summary(reason)}; elapsed={time.monotonic() - self.started:.1f}s")
        progress("iteration", f"Report: {path_reference(self.directory / 'iteration.json')}")
        return self.outcome

    def _overview(self, analyses):
        """Save the full evidence index once per update and return only its path."""
        runs = [dict(batch_id=batch["batch_id"], **run)
                for batch in analyses for run in batch["runs"].values()]
        overview = dict(runs=runs)
        self.save("overview", overview)
        return self.report["artifacts"]["overview"]

    def _measure(self, parent, candidate, request):
        """Validate the owning role's collectors and execute its observation request."""
        self.report.setdefault("sampling", [])
        name = self.runtime.next_name("sampling")
        patch = self.directory / (name + ".diff")
        validation = validate_sampling(parent, candidate, timeout=min(30, self.remaining()),
                                       patch_path=patch, allow_unchanged=True)
        self.save(name + "_validation", validation)
        if not validation["passed"]:
            raise ValueError("Collector validation failed; inspect " + self.report["artifacts"][name + "_validation"])
        measured = self.versions.freeze(candidate)
        self.versions.verify(parent)
        used = [condition for item in self.report["sampling"]
                if request["mode"] == "repeat" or item["source"]["version"] == measured["version"]
                for condition in item["conditions"]]
        if measured["version"] == parent["version"]:
            used.extend(conditions_for_evaluation(self.config))
        match_set = sampling_conditions(self.config, request, used)
        sampling = dict(parent=parent["version"], source=measured, request=request,
                        conditions=match_set, patch=path_reference(patch))
        self.report["sampling"].append(sampling)
        self.save(name, sampling)
        evidence = self.batch(measured, "sampling", match_set=match_set)
        problem = batch_problem(evidence["analysis"])
        if problem:
            raise ValueError("Sampling evidence unavailable: " + problem["reason"])
        sampling.update(batch=evidence["batch_path"], evidence=evidence["analysis_path"])
        self.save(name, sampling)
        return evidence

    def _research(self, parent, baseline):
        """Route explicit stage handoffs, preserving evidence and bounding all revisits."""
        conditions = dict(self.config["matches"], styles=self.config["evaluation_styles"])
        self.report["conditions"] = conditions
        experiences = search(self.root, self.report["conditions"]) if self.config["use_memory"] else []
        self.save("memory", experiences)
        evidence, history = [baseline], []
        overview = self._overview([baseline["analysis"]])
        self.save("research", dict(stage="discovery", history=history))
        payload = {}
        observation = None
        role, sampled = "discovery", 0

        def sampling_remaining():
            return max(0, min(self.config["trials"] - sampled,
                self.config["max_matches"] - self.report["costs"]["matches"] - len(conditions_for_evaluation(self.config))))

        def sample(request):
            """Execute sampling inside the active research conversation."""
            nonlocal sampled, overview
            before = self.report["costs"]["matches"]
            try:
                if type(request["matches"]) is not int or not 1 <= request["matches"] <= sampling_remaining():
                    raise ValueError("Sampling request exceeds the remaining shared budget")
                measured = self._measure(parent, observation, request)
                evidence.append(measured)
                overview = self._overview([e["analysis"] for e in evidence])
                value = dict(evidence_index=overview, evidence=measured["analysis_path"])
            except SnapshotError:
                raise
            except ValueError as exc:
                value = dict(error=str(exc))
            finally:
                sampled += self.report["costs"]["matches"] - before
            return dict(value, sampling_remaining=sampling_remaining())

        for step in range(1, self.config["max_research_steps"] + 1):
            session = "rsi.agents." + role
            module = import_module(session)
            remaining = sampling_remaining()
            fields = ROLE_INPUTS[role]
            records = self.analysis_records.get(role, [])
            task = dict({key: copy.deepcopy(payload[key]) for key in fields if key in payload},
                        conditions=conditions, evidence_index=overview)
            if "feedback" in payload:
                task["feedback"] = payload["feedback"]
            if role in {"discovery", "investigation"}:
                if experiences:
                    task["experience"] = self.report["artifacts"]["memory"]
            job = dict(role=role, session=session,
                       parent=parent, context=path_reference(self.experiment.context),
                       analysis_records=records, task=task, sampling_remaining=remaining, prompt=module.PROMPT)
            job["max_analyses"] = self.config["max_analyses"]
            if role == "resolution":
                job["max_tasks"] = self.config["resolution_max_tasks"]
            if role in {"discovery", "investigation"}:
                job["sampling_styles"] = self.config["evaluation_styles"]
                if observation is None:
                    observation = create_workspace(self.versions, parent, self.directory / "iteration.json",
                        "observations", root=self.runtime.root, name=self.runtime.next_name("observations"))
                    self.report["observation_workspace"] = dict(path=path_reference(observation.parent), status="retained")
                job["workspace"] = path_reference(observation)
            name = f"{role}_{step:02d}"
            result = self.model(name, job, on_sampling=sample if role in {"discovery", "investigation"} else None)
            if role == "investigation":
                payload.update(investigation_context(payload, result, parent))
            elif role == "discovery" and "metrics" in result:
                payload["metrics"] = result["metrics"]
            action = result["action"]
            artifact = self.report["artifacts"][name]
            history.append(dict(stage=role, action=action, reason=result.get("reason"),
                                artifact=artifact))
            self.save("research", dict(stage=role, history=history))
            if action == "unresolved":
                return None, result["reason"]
            if role == "discovery" and action == "problem":
                payload.pop("feedback", None)
                problem = {key: result[key] for key in Problem.model_fields if key in result}
                payload["problem"] = problem
                self.report["problem"] = problem
                self.report["artifacts"]["problem"] = artifact
                role = "hypotheses"
            elif role == "hypotheses" and action == "hypotheses":
                payload.pop("feedback", None)
                payload["hypotheses"] = result["hypotheses"]
                self.report["artifacts"]["hypotheses"] = artifact
                role = "investigation"
            elif role == "investigation" and action == "diagnosis":
                payload.pop("feedback", None)
                payload["diagnosis"] = result["diagnosis"]
                self.report["diagnosis"] = result["diagnosis"]
                self.report["artifacts"]["diagnosis"] = artifact
                role = "resolution"
            elif role == "resolution" and action == "resolution":
                self.report["artifacts"]["resolution"] = artifact
                return dict(problem=payload["problem"], hypotheses=payload["hypotheses"], conditions=conditions,
                            diagnosis=payload["diagnosis"], tests=payload["tests"], tasks=result["tasks"]), None
            elif ((role == "investigation" and action == "discovery")
                  or (role == "resolution" and action == "investigation")):
                previous = (dict(problem=payload["problem"]) if action == "discovery"
                            else dict(diagnosis=payload["diagnosis"]))
                payload["feedback"] = dict(reason=result["reason"], **previous)
                invalid = (("problem", "hypotheses", "tests", "diagnosis", "resolution")
                           if action == "discovery" else ("diagnosis", "resolution"))
                for key in invalid:
                    payload.pop(key, None)
                    self.report.pop(key, None)
                    self.report["artifacts"].pop(key, None)
                role = action
            else:
                raise ValueError(f"Invalid research handoff: {role} -> {action}")
        return None, "Research step budget exhausted; evidence and stage history retained"

    def execute(self, parent=None, parent_evidence=None):
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
            batch, analysis = parent_evidence["batch"], parent_evidence["analysis"]
            problem = batch_problem(analysis)
            if problem:
                return self.finish("error", "Parent batch unavailable: " + problem["reason"])
            self.experiment.remember(parent, parent_evidence)
            self.report["parent_batch"] = parent_evidence["batch_path"]
            self.report["artifacts"][batch["batch_id"] + "_analysis"] = parent_evidence["analysis_path"]
            diagnosis, reason = self._research(parent, parent_evidence)
            if diagnosis is None:
                return self.finish("unresolved", reason)
            from rsi.agents.resolution import solve
            return solve(self, parent, diagnosis)
        except KeyboardInterrupt:
            return self.finish("interrupted", "Cancelled without automatic retry")
        except Exception as exc:
            return self.finish("error", f"{type(exc).__name__}: {exc}",
                               fatal=isinstance(exc, SnapshotError) or getattr(exc, "fatal", False))
