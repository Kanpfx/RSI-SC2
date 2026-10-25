"""Compute static metrics and descriptive events from the Bot's original JSONL."""
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean

from runner.artifacts import path_reference, project_path, read_json, write_json
from runner.environment import LOOPS_PER_SECOND as LOOPS


def save_batch_analysis(path, analysis):
    """Store each run once and keep only references in the batch report."""
    path = Path(path)
    runs = {}
    for run_id, report in analysis["runs"].items():
        run_path = path.parent / run_id / "analysis.json"
        write_json(run_path, report)
        runs[run_id] = path_reference(run_path)
    write_json(path, dict(analysis, runs=runs))


def load_batch_analysis(path):
    """Load referenced run reports, including existing embedded historical reports."""
    analysis = read_json(project_path(path))
    analysis["runs"] = {run_id: read_json(project_path(report)) if isinstance(report, str) else report
                        for run_id, report in analysis["runs"].items()}
    return analysis

WORKERS = {"SCV", "DRONE", "PROBE", "MULE"}
BASES = {"COMMANDCENTER", "ORBITALCOMMAND", "PLANETARYFORTRESS", "NEXUS", "HATCHERY", "LAIR", "HIVE"}
PRODUCERS = BASES | {"BARRACKS", "FACTORY", "STARPORT", "GATEWAY", "WARPGATE", "STARGATE", "ROBOTICSFACILITY"}
DEFAULTS = dict(sample_gap_loops=32, event_seconds=10, float_minerals=500, float_vespene=300,
                loss_window_seconds=10, worker_loss_count=3, army_loss_count=3, block_count=5)


def read_records(directory, run_id):
    """Read original records with stable physical-line references; retain corruption warnings."""
    records, problems, previous = [], [], -1
    path = Path(directory) / "trace.jsonl"
    if not path.exists():
        return [], ["missing_trace"]
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or not isinstance(row.get("frame"), int) or not isinstance(row.get("event"), str):
                    raise ValueError("Invalid event")
            except (ValueError, TypeError):
                problems.append(f"corrupt_line:{number}")
                continue
            if row["frame"] < previous:
                problems.append(f"frame_decreased:{number}")
            previous = row["frame"]
            records.append(dict(row, source_ref=f"{run_id}:L{number}"))
    return records, problems


def statistics(rows, window=None):
    """Summarize samples using their covered duration when available; missing stays null."""
    selected = [r for r in rows if window is None or window[0] <= r["time"] <= window[1]]
    if not selected:
        return dict(mean=None, min=None, max=None, first=None, last=None, count=0, first_time=None)
    weights = [r.get("seconds", 0) for r in selected]
    values = [r["value"] for r in selected]
    total_weight = sum(weights)
    average = sum(v * w for v, w in zip(values, weights)) / total_weight if total_weight else mean(values)
    return dict(mean=average, min=min(values), max=max(values), first=values[0], last=values[-1],
                count=len(values), first_time=selected[0]["time"])


def metric_statistics(name):
    """Expose the meaningful statistics for existing state samples and event counters."""
    if name in {"requests", "submitted_commands", "observed_actions", "completed_actions",
                "worker_losses", "army_losses", "unknown_losses"} or name.startswith(("units_produced.", "blocked.")):
        return ("count", "first_time")
    if name in {"minerals", "vespene", "supply_used", "supply_cap", "supply_left", "workers",
                "idle_workers", "bases", "supply_block_ratio", "army_supply", "combat_value",
                "trade_efficiency", "army_value", "army_investment", "resource_collection_rate"} or name.startswith(
                    ("unit_count.", "production_busy_ratio.", "score.")):
        return ("mean", "min", "max", "first", "last")
    return ()


def metric_summary(name, rows, window=None):
    """Omit statistics that have no meaning for this metric's data type."""
    values = statistics(rows, window)
    return {key: values[key] for key in metric_statistics(name)}


class Analyzer:
    def __init__(self, project, config=None):
        """Bind fixed analysis thresholds and the unit knowledge used for calculations."""
        self.project = project_path(project)
        self.config = dict(DEFAULTS, **(config or {}))
        self.catalog = {}
        for path in (self.project / "knowledge/units").glob("*.json"):
            self.catalog.update(read_json(path))

    def analyze_run(self, directory, run_id):
        """Read one run and return its static report without writing to the input directory."""
        directory = project_path(directory)
        manifest = read_json(directory / "manifest.json")
        result = read_json(directory / "result.json")
        rows, problems = read_records(directory, run_id)
        duration = result.get("duration_loops")
        if duration is None:
            duration = max((r["frame"] for r in rows), default=0)
        for event in ("game_started", "game_ended"):
            if not any(row["event"] == event for row in rows):
                problems.append("missing_" + event)
        if result.get("execution_status") != "normal":
            problems.append("run_not_normal")

        series, events = defaultdict(list), []
        for row in rows:
            if row["event"] in {"unit_created", "construction_started", "construction_complete"}:
                events.append(dict(kind=row["event"], start=row["frame"] / LOOPS, end=row["frame"] / LOOPS,
                                   details=row, source_refs=[row["source_ref"]]))
            if row["event"] == "unit_created":
                series["units_produced." + row["type"]].append(dict(
                    time=row["frame"] / LOOPS, value=1, source_refs=[row["source_ref"]]))

        samples = self._analyze_samples([row for row in rows if row["event"] == "state"], duration)
        losses = self._analyze_losses(rows)
        actions = self._analyze_actions(rows, duration)
        for analysis in (samples, losses, actions):
            series.update(analysis["metrics"])
            events.extend(analysis["events"])
        coverage = self._coverage(duration, problems, samples["sampled_seconds"])
        if "army_value" in series:
            coverage["missing_metrics"].pop("army_value", None)
        if losses["unattributed_deaths"]:
            coverage["unattributed_deaths"] = losses["unattributed_deaths"]
        report = dict(run_id=run_id, log_path=path_reference(directory / "trace.jsonl"),
                      conditions={k: manifest.get(k) for k in ("map", "race", "difficulty", "seed")},
                      environment={k: manifest.get(k) for k in ("map_sha256", "environment", "game_time_limit")},
                      outcome=result, coverage=coverage, metrics=dict(series),
                      events=sorted(events, key=lambda e: e["start"]),
                      modules=actions["modules"], actions=actions["actions"])
        return report

    def _coverage(self, duration, problems, sampled_seconds):
        """Describe observed coverage and known limits without filling missing metrics."""
        return dict(problems=problems, duration_seconds=duration / LOOPS,
                    sampled_seconds=sampled_seconds,
                    sample_ratio=min(1, sampled_seconds * LOOPS / duration) if duration else None,
                    approximate_durations=True, integrity="partial" if problems else "complete",
                    missing_metrics={
                        "mineral_income": "Separate mineral income metric is not derived",
                        "gas_income": "Separate gas income metric is not derived",
                        "upgrade_state": "No upgrade-state snapshots or completion callbacks",
                        "army_value": "Static costs do not reliably encode invested unit value",
                        "army_supply": "Omitted at samples with missing static supply; estimate excludes workers"})

    def _analyze_samples(self, samples, duration):
        """Calculate sampled state metrics and sustained events over covered intervals."""
        series, events = defaultdict(list), []
        sampled_seconds = 0
        intervals = []
        unit_types = {u["type"] for sample in samples for u in sample["own"]}
        for index, sample in enumerate(samples):
            next_frame = samples[index + 1]["frame"] if index + 1 < len(samples) else sample["frame"]
            seconds = (next_frame - sample["frame"]) / LOOPS
            if not 0 < next_frame - sample["frame"] <= self.config["sample_gap_loops"]:
                seconds = 0
            sampled_seconds += seconds
            units = sample["own"]
            counts = Counter(u["type"] for u in units)
            workers = [u for u in units if u["type"] in WORKERS]
            values = {k: sample[k] for k in ("minerals", "vespene", "supply_used", "supply_cap")}
            values.update(supply_left=sample["supply_cap"] - sample["supply_used"],
                          workers=len(workers), idle_workers=sum(not u["orders"] for u in workers),
                          bases=sum(counts[t] for t in BASES),
                          supply_block_ratio=int(sample["supply_used"] >= sample["supply_cap"]))
            score = sample.get("score")
            if score is not None:
                values.update({"score." + name: value for name, value in score.items()})
                killed = score["killed_value_units"] + score["killed_value_structures"]
                lost = sum(value for name, value in score.items() if name.startswith("lost_"))
                values.update(combat_value=killed,
                    trade_efficiency=killed / (killed + lost) if killed + lost else 0,
                    army_value=score["used_minerals_army"] + score["used_vespene_army"],
                    army_investment=score["total_used_minerals_army"] + score["total_used_vespene_army"])
                if sample["frame"] > 0:
                    values["resource_collection_rate"] = 60 * LOOPS * (
                        score["collected_minerals"] + score["collected_vespene"]) / sample["frame"]
            values.update({f"unit_count.{name}": counts[name] for name in unit_types})
            army_supply, complete = 0, True
            for unit in units:
                entry = self.catalog.get(unit["type"], {})
                if unit["type"] in WORKERS or entry.get("category") in {"building", "add-on", "structure"}:
                    continue
                supply = entry.get("data", {}).get("supply")
                if supply is None:
                    complete = False
                else:
                    army_supply += supply
            if complete:
                values["army_supply"] = army_supply
            ready = defaultdict(list)
            for unit in units:
                if unit["type"] in PRODUCERS and unit["build_progress"] >= 1:
                    ready[unit["type"]].append(unit)
            flags = dict(supply_block=values["supply_block_ratio"] == 1,
                         resource_float=sample["minerals"] >= self.config["float_minerals"] or
                                        sample["vespene"] >= self.config["float_vespene"])
            for name in PRODUCERS:
                group = ready.get(name, ())
                busy = sum(bool(u["orders"]) for u in group)
                if group:
                    values[f"production_busy_ratio.{name}"] = busy / len(group)
                flags[f"production_idle.{name}"] = busy < len(group)
            for name, value in values.items():
                series[name].append(dict(time=sample["frame"] / LOOPS, value=value, seconds=seconds,
                                         source_refs=[sample["source_ref"]]))
            intervals.append((sample, next_frame, seconds, flags))
        active = {}
        for sample, end_frame, seconds, flags in intervals + [(dict(frame=duration), duration, 0, {})]:
            for kind in set(active) | set(flags):
                if kind in active and (not flags.get(kind) or not seconds or active[kind]["end"] != sample["frame"] / LOOPS):
                    event = active.pop(kind)
                    if event["end"] - event["start"] >= self.config["event_seconds"]:
                        events.append(event)
                if flags.get(kind) and seconds:
                    event = active.setdefault(kind, dict(kind=kind.split(".")[0], start=sample["frame"] / LOOPS,
                                              end=sample["frame"] / LOOPS, details=dict(facility=kind.partition(".")[2]),
                                              source_refs=[]))
                    event["end"] = end_frame / LOOPS
                    event["source_refs"].append(sample["source_ref"])
        return dict(metrics=series, events=events, sampled_seconds=sampled_seconds)

    def _analyze_losses(self, rows):
        """Attribute destruction to known units and detect worker/army loss bursts."""
        known, losses = {}, defaultdict(list)
        series, events = defaultdict(list), []
        unattributed = 0
        for row in rows:
            if row["event"] == "state":
                known.update({u["tag"]: u for u in row["own"]})
            elif row["event"] == "unit_created":
                known[row["tag"]] = dict(type=row["type"])
            elif row["event"] == "unit_destroyed":
                unit = known.pop(row["tag"], None)
                if unit:
                    category = self.catalog.get(unit["type"], {}).get("category", "").lower()
                    kind = ("worker" if unit["type"] in WORKERS else
                            "army" if category in {"unit", "combat unit", "army", "combat"} else "unknown")
                    losses[kind].append(row)
                else:
                    unattributed += 1
        for kind, deaths in losses.items():
            for death in deaths:
                series[f"{kind}_losses"].append(dict(time=death["frame"] / LOOPS, value=1,
                                                     source_refs=[death["source_ref"]]))
            if kind in {"worker", "army"}:
                self._clusters(deaths, f"{kind}_loss_spike", self.config[f"{kind}_loss_count"], events)
        return dict(metrics=series, events=events, unattributed_deaths=unattributed)

    def _analyze_actions(self, rows, duration):
        """Follow request lifecycles and summarize execution by module and block reason."""
        calls, requests, modules = {}, defaultdict(list), defaultdict(Counter)
        series, events = defaultdict(list), []
        for row in rows:
            if row["event"] == "module_call":
                calls[row["call_id"]] = row
                key = row["module_path"] + ":" + row["module_id"]
                module = modules[key]
                module["calls"] += 1
                module[row["status"]] += 1
            elif row["event"] == "action" and row.get("request_id"):
                requests[row["request_id"]].append(row)
        blocked = []
        submitted = confirmed = 0
        for history in requests.values():
            first = history[0]
            first_by_status = {}
            for row in history:
                first_by_status.setdefault(row["status"], row)
            submitted += "submitted" in first_by_status
            confirmed += "observed" in first_by_status or "completed" in first_by_status
            call = calls.get(first["call_id"], {})
            key = call.get("module_path", "unknown") + ":" + call.get("module_id", first.get("source", ""))
            module = modules[key]
            module["requests"] += 1
            for status in ("submitted", "observed", "completed", "failed", "unknown"):
                if status in first_by_status:
                    module[status] += 1
            refusal = next((r for r in history if r["status"] in {"rejected", "deferred"}), None)
            if refusal:
                module["blocked:" + refusal["reason"]] += 1
                blocked.append(refusal)
            for status, metric in (("proposed", "requests"), ("submitted", "submitted_commands"),
                                   ("observed", "observed_actions"), ("completed", "completed_actions")):
                fact = first_by_status.get(status)
                if fact:
                    series[metric].append(dict(time=fact["frame"] / LOOPS, value=1,
                                               source_refs=[fact["source_ref"]]))
            if refusal:
                series["blocked." + refusal["reason"]].append(dict(time=refusal["frame"] / LOOPS, value=1,
                                                                  source_refs=[refusal["source_ref"]]))
        for reason in {r["reason"] for r in blocked}:
            self._clusters(sorted((r for r in blocked if r["reason"] == reason), key=lambda r: r["frame"]),
                           "action_block_cluster", self.config["block_count"], events, reason=reason)
        proposals = len(requests)
        blocked_by_reason = dict(Counter(row["reason"] for row in blocked))
        summary = dict(requests=proposals, submitted=submitted,
                       submitted_ratio=submitted / proposals if proposals else None,
                       bot_commands_per_minute=submitted * 60 * LOOPS / duration if duration else None,
                       confirmed=confirmed,
                       blocked_by_reason=blocked_by_reason,
                       duplicate_requests=sum(r["reason"] == "duplicate_order" for r in blocked))
        summary["blocked_ratio_by_reason"] = {
            reason: count / proposals for reason, count in blocked_by_reason.items()} if proposals else {}
        module_stats = {}
        for key, counters in modules.items():
            item = {name: count for name, count in counters.items() if not name.startswith("blocked:")}
            for name in ("calls", "skipped", "invoked_true", "invoked_false", "requests"):
                item.setdefault(name, 0)
            item["exceptions"] = counters.get("exception", 0)
            item["blocked_by_reason"] = {name.removeprefix("blocked:"): count for name, count in counters.items()
                                         if name.startswith("blocked:")}
            module_stats[key] = item
        return dict(metrics=series, events=events, modules=module_stats, actions=summary)

    def _clusters(self, rows, kind, threshold, events, **details):
        """Emit nonoverlapping threshold bursts within a configured time window."""
        recent = []
        for row in rows:
            recent = [r for r in recent if row["frame"] - r["frame"] <= self.config["loss_window_seconds"] * LOOPS]
            recent.append(row)
            if len(recent) >= threshold:
                events.append(dict(kind=kind, start=recent[0]["frame"] / LOOPS, end=row["frame"] / LOOPS,
                                   details=dict(details, count=len(recent)),
                                   source_refs=[r["source_ref"] for r in recent]))
                recent = []

    def analyze_batch(self, batch):
        """Analyze an explicitly supplied batch; holdout remains outside development queries."""
        if batch["purpose"] == "holdout":
            raise ValueError("Holdout is unavailable to the development Analyzer")
        reports = {run_id: self.analyze_run(path, run_id) for run_id, path in batch["runs"].items()}
        summary = dict(outcomes=dict(Counter(r["outcome"].get("outcome") or "unknown" for r in reports.values())),
                       event_counts=dict(Counter(e["kind"] for r in reports.values() for e in r["events"])))
        return dict(batch_id=batch["batch_id"], version=batch["version"], purpose=batch["purpose"],
                    runs=reports, summary=summary)


def analyze_evidence(analyzer, batch, batch_path, analysis_path):
    """Analyze and persist one batch in the selected output directory, including reused evidence."""
    analysis = analyzer.analyze_batch(batch)
    save_batch_analysis(analysis_path, analysis)
    return dict(batch=batch, analysis=analysis, batch_path=path_reference(batch_path),
                analysis_path=path_reference(analysis_path))
