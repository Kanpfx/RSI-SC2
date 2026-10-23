"""Run one bounded iteration: python -B -m rsi.run --help."""
import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import uuid4

from sc2.data import Difficulty

from .analyzer import Analyzer
from .console import error_summary, progress, progress_line
from .diagnosis.session import make_job as diagnosis_job
from .editor import make_job as editor_job
from .evaluator import evaluate, freeze_plan, parse_specialist, specialist_job
from .memory import record, search
from .tools.llm import call_session, load_settings, preflight
from .tools.matches import MATCH_START_INTERVAL, run_batch
from .tools.retriever import freeze_context
from .tools.validate import validate
from .tools.versions import Versions, path_reference, project_path, read_json, write_json


def validate_config(config):
    """Reject invalid budgets and research controls before starting external work."""
    if config["mode"] not in {"analyze_only", "iterate"}:
        raise ValueError("mode must be analyze_only or iterate")
    for key in ("max_matches", "max_wall_seconds", "parallel_matches"):
        if not isinstance(config[key], int) or config[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    if not isinstance(config["max_repairs"], int) or config["max_repairs"] < 0:
        raise ValueError("max_repairs must be nonnegative")
    diagnosis = config["diagnosis"]
    if diagnosis["query_policy"] not in {"active", "fixed"}:
        raise ValueError("query_policy must be active or fixed")
    for key in ("max_queries", "max_items", "max_chars"):
        if not isinstance(diagnosis[key], int) or diagnosis[key] <= 0:
            raise ValueError(f"diagnosis.{key} must be positive")
    for key in ("competing_hypotheses", "use_memory"):
        if not isinstance(diagnosis[key], bool):
            raise ValueError(f"diagnosis.{key} must be bool")
    evaluation = config["evaluation"]
    if not isinstance(evaluation["formal"], bool) or not isinstance(evaluation["specialist"], bool):
        raise ValueError("evaluation.formal/specialist must be bool")
    if not isinstance(evaluation["max_checks"], int) or evaluation["max_checks"] < 0:
        raise ValueError("evaluation.max_checks must be nonnegative")
    if not 0 < evaluation["alpha"] < 1 or not 0 <= evaluation["min_improvement"] <= 1:
        raise ValueError("Invalid fixed evaluation thresholds")
    if evaluation["min_samples"] < 1 or not 0 < evaluation["min_coverage"] <= 1:
        raise ValueError("Invalid minimum samples/coverage")
    if evaluation["time_limit_score"] not in {"nonwin", "exclude"}:
        raise ValueError("Invalid time-limit policy")
    for key in ("max_total_tokens", "max_cost", "max_iterations", "max_output_tokens", "request_timeout", "wall_timeout"):
        if config["model"][key] <= 0:
            raise ValueError(f"model.{key} must be positive")
    for conditions in config["explore"] + [config["validation"]] + evaluation["matches"]:
        match = dict(config["matches"], **conditions)
        if match["game_time_limit"] <= 0 or match["wall_timeout"] <= 0 or not isinstance(match["seed"], int):
            raise ValueError("Matches require positive timeouts and integer seeds")
        if match["race"] not in {"Terran", "Protoss", "Zerg"} or match["difficulty"] not in Difficulty.__members__:
            raise ValueError("Unsupported match opponent")


class Run:
    def __init__(self, project, config, *, collect=False, enable_model=False):
        """Bind one iteration's fixed configuration; execution starts only in execute()."""
        self.project = Path(project).resolve()
        self.config = copy.deepcopy(config)
        validate_config(self.config)
        self.collect, self.enable_model = collect, enable_model
        self.root = self.project / "runtime/rsi"
        self.versions = Versions(self.project, self.root)
        self.analyzer = Analyzer(self.project, config["analysis"],
                                 config["diagnosis"]["max_items"], config["diagnosis"]["max_chars"])
        self.started = time.monotonic()
        self.directory = self.root / "iterations" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:8])
        self.report = dict(iteration_id=self.directory.name, status="running", stage="prepare",
                           config=self.config, costs=dict(matches=0, sessions=[], wall_seconds=0), artifacts={})

    def save(self, name, value):
        """Persist a stage artifact and update the small iteration report."""
        path = self.directory / f"{name}.json"
        write_json(path, value)
        self.report["artifacts"][name] = path_reference(path)
        self.report["stage"] = name
        self.report["costs"]["wall_seconds"] = time.monotonic() - self.started
        summary = dict(self.report)
        if "diagnosis" in summary:
            summary["diagnosis"] = dict(status=self.report["diagnosis"]["conclusion"]["status"])
        if "validation" in summary:
            summary["validation"] = dict(passed=self.report["validation"]["passed"], failed_checks=[
                check["name"] for check in self.report["validation"]["checks"] if not check["passed"]])
        if "evaluation" in summary:
            summary["evaluation"] = {key: self.report["evaluation"][key] for key in ("decision", "reason")}
        write_json(self.directory / "iteration.json", summary)
        return value

    def remaining(self):
        """Enforce the overall wall budget between operations."""
        remaining = self.config["max_wall_seconds"] - (time.monotonic() - self.started)
        if remaining < 1:
            raise RuntimeError("Iteration wall budget exhausted")
        return remaining

    def batch(self, version, overrides, purpose):
        """Reserve attempts before running a bounded batch; failures also consume budget."""
        count = len(overrides)
        if not count or self.report["costs"]["matches"] + count > self.config["max_matches"]:
            raise RuntimeError("Empty batch or match budget exhausted")
        self.report["costs"]["matches"] += count
        remaining = self.remaining()
        remaining -= MATCH_START_INTERVAL * (count - 1)
        if remaining < 1:
            raise RuntimeError("Insufficient wall budget for staggered match starts")
        # Share the remaining deadline across queued waves, not just each worker.
        waves = (count + self.config["parallel_matches"] - 1) // self.config["parallel_matches"]
        conditions = []
        for override in overrides:
            condition = dict(self.config["matches"], **override)
            condition["wall_timeout"] = min(condition["wall_timeout"], max(1, int(remaining / waves)))
            conditions.append(condition)
        name = purpose + "_" + uuid4().hex[:8]
        self.save(name + "_started", dict(version=version, purpose=purpose, conditions=conditions))
        print(progress_line(purpose, f"Starting {count} matches; parallel={self.config['parallel_matches']}"), flush=True)
        batch = run_batch(self.project, version, conditions, self.directory / "batches" / name,
                          purpose, self.config["parallel_matches"])
        print(progress_line(purpose, "Matches finished; analyzing logs"), flush=True)
        analysis = self.analyzer.add_batch(batch)
        self.save(name + "_analysis", analysis)
        if purpose != "explore":
            for run_id in batch["runs"]:
                self.analyzer.records.pop(run_id, None)
        summary = analysis["summary"]
        outcomes = ", ".join(f"{name}={count}" for name, count in summary["outcomes"].items())
        events = sorted(summary["event_counts"].items(), key=lambda item: item[1], reverse=True)[:3]
        progress(purpose, f"Outcomes: {outcomes}; main events: " +
                 (", ".join(f"{name}={count}" for name, count in events) or "none"))
        return batch, analysis

    def model(self, name, job):
        """Allocate remaining shared model budget and charge every completed/failed session."""
        settings = dict(self.model_settings)
        spent = self.report["costs"]["sessions"]
        settings["max_total_tokens"] -= sum(s.get("tokens") or 0 for s in spent)
        settings["max_cost"] -= sum(s.get("cost") or 0 for s in spent)
        if settings["max_total_tokens"] <= 0 or settings["max_cost"] <= 0:
            raise RuntimeError("Iteration model budget exhausted")
        settings["wall_timeout"] = min(settings["wall_timeout"], self.remaining())
        job = dict(job, model=settings)
        self.save(name + "_started", dict(role=job["role"], model=settings))
        print(progress_line(name, f"Model session started; timeout={settings['wall_timeout']:.0f}s"), flush=True)
        response = call_session(self.project, job, self.directory / "sessions" / name)
        usage = response.get("usage", {})
        self.report["costs"]["sessions"].append(dict(name=name, **usage))
        self.save(name + "_response", response)
        print(progress_line(name, f"Model session finished: status={response['status']} "
                            f"tokens={usage.get('tokens')} cost={usage.get('cost')}"), flush=True)
        # Unknown usage after a failed worker cannot safely be treated as free.
        if response["status"] != "ok":
            raise RuntimeError(response["reason"])
        if usage.get("tokens") is None:
            raise RuntimeError("Session token usage unavailable; stop before another paid session")
        return response["value"]

    def finish(self, status, reason):
        """Retain all outcomes and write memory only when diagnosis actually ran."""
        self.report.update(status=status, reason=reason)
        self.analyzer.records.clear()
        if "diagnosis" in self.report:
            record(self.root, self.report)
        self.save("final", dict(status=status, reason=reason))
        progress("iteration", f"Finished: {status}; {error_summary(reason)}; elapsed={time.monotonic() - self.started:.1f}s")
        progress("iteration", f"Report: {path_reference(self.directory / 'iteration.json')}")
        return self.report

    def execute(self, reuse=()):
        """Run the minimal sequential loop; no generic state machine or automatic resume."""
        try:
            progress("iteration", f"Started: {path_reference(self.directory)}")
            if self.config["mode"] == "iterate":
                if not self.enable_model:
                    return self.finish("awaiting_model", "Use --enable-model to explicitly enable paid sessions")
                self.model_settings = load_settings(self.project, self.config["model"])
                preflight(self.model_settings)
            parent = self.versions.current()
            self.report["parent"] = parent
            self.save("config", self.config)
            progress("prepare", f"Parent: {parent['version'][:12]}; mode={self.config['mode']}")
            batches, analyses = [], []
            for path in reuse:
                print(progress_line("explore", f"Loading reused batch: {path}"), flush=True)
                batch = read_json(Path(path))
                if batch["purpose"] != "explore" or batch["version"] != parent["version"]:
                    raise ValueError("Reuse requires exploration data from the current parent")
                for run_id, run_path in batch["runs"].items():
                    manifest = read_json(project_path(run_path) / "manifest.json")
                    if (manifest.get("purpose") != "explore" or manifest.get("version") != parent["version"]
                            or manifest.get("run_id") != run_id):
                        raise ValueError("Reused run identity/purpose mismatch")
                batches.append(batch)
                analyses.append(self.analyzer.add_batch(batch))
            if self.collect:
                batch, analysis = self.batch(parent, self.config["explore"], "explore")
                batches.append(batch)
                analyses.append(analysis)
            if not batches:
                return self.finish("awaiting_data", "Use --collect or --reuse-batch PATH")
            overview = [self.analyzer.query("overview", b["batch_id"]) for b in batches]
            self.save("overview", overview)
            if self.config["mode"] == "analyze_only":
                return self.finish("analysis_only", "Exploration analysis saved; no model calls")
            if not any(r["coverage"]["sampled_seconds"] > 0 for a in analyses for r in a["runs"].values()):
                return self.finish("unresolved", "No usable observation intervals")
            conditions = [{k: r["conditions"][k] for k in ("map", "race", "difficulty")}
                          for a in analyses for r in a["runs"].values()]
            self.report["conditions"] = conditions[0] if conditions and all(c == conditions[0] for c in conditions) else {}
            context = self.directory / "context"
            experiences = search(self.root, self.report["conditions"]) if self.config["diagnosis"]["use_memory"] else []
            print(progress_line("diagnosis", f"Freezing references; memory_items={len(experiences)}"), flush=True)
            freeze_context(self.project, context, experiences)
            # Analysis and future queries use the same frozen reference catalogue.
            self.analyzer.catalog = Analyzer(context).catalog
            # Diagnosis loads its own exploration logs; the coordinator no longer queries them.
            self.analyzer.records.clear()
            diagnosis = self.model("diagnosis", diagnosis_job(self.project, parent, batches, overview, context, self.config))
            self.report["diagnosis"] = self.save("diagnosis", diagnosis)
            progress("diagnosis", f"Completed: {diagnosis['conclusion']['status']}; queries={len(diagnosis.get('queries', []))}")
            for hypothesis in diagnosis.get("hypotheses", []):
                updates = hypothesis.get("updates", [])
                progress("diagnosis", f"{hypothesis['id']} -> {hypothesis.get('status', 'open')}; " +
                         (updates[-1]["reason"] if updates else hypothesis["explanation"]))
            if diagnosis["conclusion"]["status"] != "repair_ready":
                return self.finish("unresolved", diagnosis["conclusion"].get("reason", "Diagnosis unresolved"))
            candidate = self.versions.candidate(parent, self.directory / "candidate")
            feedback = None
            for attempt in range(self.config["max_repairs"] + 1):
                print(progress_line("editor", f"Attempt {attempt + 1}/{self.config['max_repairs'] + 1}"), flush=True)
                self.model(f"editor_{attempt}", editor_job(candidate, diagnosis, feedback, context=context))
                changed = self.versions.diff(parent, candidate, self.directory / "patch.diff")
                self.report["changed_files"] = changed
                self.report["artifacts"]["patch"] = path_reference(self.directory / "patch.diff")
                progress("editor", "Changed files: " + (", ".join(changed) or "none"))
                feedback = validate(parent, candidate, timeout=min(30, self.remaining()))
                for check in feedback["checks"]:
                    print(progress_line("validation", f"{check['name']}: {'passed' if check['passed'] else 'failed'}"), flush=True)
                smoke_unavailable = False
                if feedback["passed"]:
                    child = self.versions.freeze(candidate)
                    _, smoke = self.batch(child, [self.config["validation"]], "validation")
                    run = next(iter(smoke["runs"].values()))
                    passed = run["outcome"].get("execution_status") == "normal" and run["coverage"]["integrity"] == "complete"
                    feedback["checks"].append(dict(name="smoke", passed=passed, outcome=run["outcome"], coverage=run["coverage"]))
                    feedback["passed"] = passed
                    smoke_unavailable = not passed and run["outcome"].get("execution_status") != "bot_error"
                    print(progress_line("validation", f"Smoke match: {'passed' if passed else 'failed'}"), flush=True)
                self.report["validation"] = self.save(f"validation_{attempt}", feedback)
                if smoke_unavailable:
                    return self.finish("inconclusive", "Validation match execution or log integrity is incomplete")
                if feedback["passed"]:
                    break
            if not feedback["passed"]:
                return self.finish("reject", "Candidate validation failed")
            self.report["candidate"] = child
            rules = self.config["evaluation"]
            if 2 * len(rules["matches"]) > self.config["max_matches"] - self.report["costs"]["matches"]:
                return self.finish("inconclusive", "Remaining budget cannot complete the fixed evaluation")
            supplementary = []
            if rules["specialist"]:
                value = self.model("evaluation_plan", specialist_job(diagnosis,
                    (self.directory / "patch.diff").read_text(encoding="utf-8"), rules["max_checks"]))
                supplementary = parse_specialist(value, rules["max_checks"])
            plan = self.save("evaluation_plan", freeze_plan(rules, diagnosis, supplementary))
            print(progress_line("evaluation", f"Plan frozen; formal={rules['formal']} "
                                f"matches_per_version={len(rules['matches'])}"), flush=True)
            _, parent_analysis = self.batch(parent, rules["matches"], "eval_parent")
            _, candidate_analysis = self.batch(child, rules["matches"], "eval_candidate")
            evaluation = evaluate(parent_analysis, candidate_analysis, plan)
            self.report["evaluation"] = self.save("evaluation", evaluation)
            for check in evaluation["expected_effects"]:
                effect = check["effect"]
                progress("evaluation", f"{effect['name']}.{effect['statistic']}: {check['result']}; " +
                         f"parent={check['parent']} candidate={check['candidate']}")
            print(progress_line("evaluation", f"Decision: {evaluation['decision']}; reason={evaluation['reason']}"), flush=True)
            if evaluation["decision"] == "accept":
                self.versions.accept(child)
                print(progress_line("evaluation", f"Accepted version: {child['version']}"), flush=True)
            return self.finish(evaluation["decision"], evaluation["reason"])
        except KeyboardInterrupt:
            return self.finish("interrupted", "Cancelled without automatic retry")
        except Exception as exc:
            return self.finish("error", f"{type(exc).__name__}: {exc}")


def main():
    """Expose explicit collection/model flags while keeping the default analysis-only."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--mode", choices=("analyze_only", "iterate"))
    parser.add_argument("--difficulty", choices=tuple(Difficulty.__members__))
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--enable-model", action="store_true")
    parser.add_argument("--reuse-batch", type=Path, action="append", default=[])
    parser.add_argument("--json", action="store_true", help="Also print the final machine-readable summary")
    args = parser.parse_args()
    config = read_json(args.config)
    if args.mode:
        config["mode"] = args.mode
    if args.difficulty:
        config["matches"]["difficulty"] = args.difficulty
    try:
        report = Run(Path(__file__).resolve().parents[1], config,
                     collect=args.collect, enable_model=args.enable_model).execute(args.reuse_batch)
    except (ValueError, KeyError) as exc:
        print(json.dumps(dict(status="configuration_error", reason=str(exc)), ensure_ascii=False))
        return 2
    if args.json:
        print(json.dumps({k: report.get(k) for k in ("iteration_id", "status", "stage", "reason", "costs", "artifacts")},
                         ensure_ascii=False, indent=2))
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
