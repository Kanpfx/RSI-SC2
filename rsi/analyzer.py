"""Derive aligned columns and frame-based events; keep original facts in their source files."""
from collections import Counter, defaultdict
import hashlib
from pathlib import Path
from statistics import mean

from runner.artifacts import path_reference, project_path, read_json, write_json
from runner.environment import LOOPS_PER_SECOND as LOOPS
from .logs import read_records


WORKERS = {"SCV", "DRONE", "PROBE", "MULE"}
BASES = {"COMMANDCENTER", "ORBITALCOMMAND", "PLANETARYFORTRESS", "NEXUS", "HATCHERY", "LAIR", "HIVE"}
PRODUCERS = BASES | {"BARRACKS", "FACTORY", "STARPORT", "GATEWAY", "WARPGATE", "STARGATE", "ROBOTICSFACILITY"}
DEFAULTS = dict(sample_gap_frames=2, event_frames=224, float_minerals=500, float_vespene=300,
                loss_window_frames=224, worker_loss_count=3, army_loss_count=3, block_count=5)


def save_batch_analysis(path, analysis):
    """Persist derived data once; the batch contains only run references and aggregates."""
    path = Path(path)
    runs = {}
    for run_id, report in analysis["runs"].items():
        source = project_path(report["log_path"]).parent
        run_path = path.parent / run_id / "analysis.json"
        write_json(run_path, report["analysis"])
        runs[run_id] = dict(source=path_reference(source), analysis=path_reference(run_path))
    write_json(path, dict(analysis, runs=runs))


def load_batch_analysis(path):
    analysis = read_json(project_path(path))
    runs = {}
    for run_id, reference in analysis["runs"].items():
        if isinstance(reference, dict) and "source" in reference:
            directory = project_path(reference["source"])
            stored = read_json(project_path(reference["analysis"]))
        elif isinstance(reference, str):
            run_path = project_path(reference)
            stored = read_json(run_path)
            directory = run_path.parent
        else:
            stored = reference
            directory = project_path(stored["log_path"]).parent
            if "analysis" in stored:
                runs[run_id] = run_view(directory, run_id, stored["analysis"])
                continue
        if stored.get("schema_version") != 3:
            raise ValueError("Legacy analysis format: re-analyze the original batch to create frame-based analysis")
        runs[run_id] = run_view(directory, run_id, stored)
    analysis["runs"] = runs
    return analysis


def state_columns(samples):
    columns = {key: [row[key] for row in samples]
               for key in ("minerals", "vespene", "supply_used", "supply_cap")}
    names = dict.fromkeys(name for row in samples for name in row.get("score", {}))
    columns.update({"score." + name: [row.get("score", {}).get(name) for row in samples] for name in names})
    return columns


def metric_statistics(name):
    if name in {"module_exceptions", "requests", "submitted_commands", "observed_actions", "completed_actions",
                "worker_losses", "army_losses", "unknown_losses"} or name.startswith(("units_produced.", "blocked.")):
        return ("count", "first_frame")
    return ("mean", "min", "max", "first", "last")


def metric_summary(name, report, window=None):
    """Aggregate native-frame columns on demand; duration weights never become per-value records."""
    lower, upper = window or (0, report["coverage"]["last_frame"])
    if metric_statistics(name)[0] == "count":
        points = [frame for frame in report["counts"].get(name, []) if lower <= frame <= upper]
        return dict(count=len(points), first_frame=min(points) if points else None)
    values = report["metrics"].get(name, [])
    frames = report["frame"]
    selected = [(index, value) for index, value in enumerate(values)
                if value is not None and lower <= frames[index] <= upper]
    if not selected:
        return dict.fromkeys(("mean", "min", "max", "first", "last"))
    weighted, weight = 0, 0
    gap = report["analysis"]["config"]["sample_gap_frames"]
    for index, value in selected:
        end = frames[index + 1] if index + 1 < len(frames) else frames[index]
        if 0 < end - frames[index] <= gap:
            interval = max(0, min(end, upper) - max(frames[index], lower))
            weighted += value * interval
            weight += interval
    numbers = [value for _, value in selected]
    return dict(mean=weighted / weight if weight else mean(numbers), min=min(numbers), max=max(numbers),
                first=numbers[0], last=numbers[-1])


def run_view(directory, run_id, analysis, rows=None):
    """Join source data in memory for queries/evaluation; this joined view is never serialized."""
    directory = project_path(directory)
    read_problems = []
    if rows is None:
        rows, read_problems = read_records(directory, run_id)
    samples = [row for row in rows if row["event"] == "state"]
    if any(len(values) != len(samples) for values in analysis["series"].values()):
        raise ValueError("State observation count differs from the saved analysis; re-analyze the original batch")
    manifest, result = read_json(directory / "manifest.json"), read_json(directory / "result.json")
    last_frame = result.get("last_frame", result.get("duration_loops"))
    last_frame = last_frame if last_frame is not None else max((row["frame"] for row in rows), default=0)
    actions = execution_summary(rows, last_frame, analysis["config"])
    metrics = dict(state_columns(samples), **analysis["series"])
    counts = dict(actions["counts"])
    counts["module_exceptions"] = [row["frame"] for row in rows
                                   if row["event"] == "module_call" and row["status"] == "exception"]
    events = list(analysis["events"])
    for row in rows:
        if row["event"] in {"unit_created", "construction_started", "construction_complete"}:
            events.append(dict(frame=row["frame"], kind=row["event"],
                               data={k: v for k, v in row.items() if k not in {"frame", "event", "source_ref"}}))
        if row["event"] == "unit_created":
            counts.setdefault("units_produced." + row["type"], []).append(row["frame"])
    for event in analysis["events"]:
        if event["kind"] == "unit_loss":
            counts.setdefault(event["data"]["category"] + "_losses", []).append(event["frame"])
    quality = analysis["quality"]
    problems = [problem for source in ("state", "trace", "events") for problem in quality[source]["problems"]]
    problems.extend(problem for problem in read_problems if problem not in problems)
    if result.get("execution_status") != "normal":
        problems.append("run_not_normal")
    if result.get("schema_version") == 3 and result.get("steps", 0) != len(samples):
        problems.append("state_step_count_mismatch")
    actual = manifest.get("actual", {})
    coverage = dict(last_frame=last_frame, sampled_frames=quality["state"]["covered_frames"],
        sample_ratio=min(1, quality["state"]["covered_frames"] / last_frame) if last_frame else None,
        integrity="partial" if problems else "complete", problems=problems,
        missing_metrics=quality["missing_metrics"], unattributed_deaths=quality["unattributed_deaths"])
    return dict(run_id=run_id, log_path=path_reference(directory / "trace.jsonl"),
        conditions={key: manifest.get(key) for key in ("map", "race", "difficulty", "seed")},
        environment=dict(map_sha256=actual.get("map_sha256", manifest.get("map_sha256")),
                         environment=manifest.get("environment"), actual=actual,
                         game_time_limit=manifest.get("game_time_limit")),
        outcome=result, coverage=coverage, frame=[row["frame"] for row in samples], metrics=metrics, counts=counts,
        events=sorted(events, key=lambda event: event["frame"]), modules=actions["modules"],
        actions=actions["actions"], analysis=analysis)


def execution_summary(rows, duration, config, window=None):
    """Follow request lifecycles and summarize execution by module and block reason."""
    all_calls = {row["call_id"]: row for row in rows if row["event"] == "module_call"}
    if window is not None:
        origin_requests = {row["request_id"] for row in rows if row["event"] == "action" and row.get("status") == "proposed"
                           and window[0] <= row["frame"] <= window[1]}
        rows = [row for row in rows if (row["event"] == "action" and row.get("request_id") in origin_requests)
                or (row["event"] != "action" and window[0] <= row["frame"] <= window[1])]
        duration = max(0, min(duration, window[1]) - window[0])
    calls, requests, modules = {}, defaultdict(list), defaultdict(Counter)
    series, events = defaultdict(list), []
    compact = any(row["event"] == "trace_format" and row.get("version") == 2 for row in rows)
    for row in rows:
        if row["event"] == "module_call":
            calls[row["call_id"]] = row
            key = row["module_path"] + ":" + row["path"]
            module = modules[key]
            if not compact:
                module["calls"] += 1
                module[row["status"]] += 1
        elif row["event"] == "module_summary":
            for item in row["modules"]:
                module = modules[item["module_path"] + ":" + item["path"]]
                module.update({key: item[key] for key in
                               ("calls", "invoked_true", "invoked_false", "skipped", "exception")
                               if item.get(key)})
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
        call = all_calls.get(first["call_id"], {})
        key = call.get("module_path", "unknown") + ":" + call.get("path", first.get("source", ""))
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
                series[metric].append(fact["frame"])
        if refusal:
            series["blocked." + refusal["reason"]].append(refusal["frame"])
    for reason in {r["reason"] for r in blocked}:
        clusters(sorted((r for r in blocked if r["reason"] == reason), key=lambda r: r["frame"]),
                       "action_block_cluster", config["block_count"], events, config, reason=reason)
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
    return dict(counts=series, events=events, modules=module_stats, actions=summary)


def clusters(rows, kind, threshold, events, config, **details):
    recent = []
    for row in rows:
        recent = [item for item in recent if row["frame"] - item["frame"] <= config["loss_window_frames"]]
        recent.append(row)
        if len(recent) >= threshold:
            events.append(dict(frame=recent[0]["frame"], end_frame=row["frame"] + 1,
                               kind=kind, data=dict(details, count=len(recent))))
            recent = []


class Analyzer:
    def __init__(self, project, config=None):
        self.project = project_path(project)
        self.config = dict(DEFAULTS, **(config or {}))
        self.catalog = {}
        digest = hashlib.sha256()
        for path in sorted((self.project / "knowledge/units").glob("*.json")):
            digest.update(path.name.encode() + b"\0" + path.read_bytes())
            self.catalog.update(read_json(path))
        self.knowledge_hash = digest.hexdigest()

    def analyze_run(self, directory, run_id=None):
        directory = project_path(directory)
        rows, problems = read_records(directory, run_id or directory.name)
        return self._analyze(rows, problems)

    def _analyze(self, rows, problems):
        samples = [row for row in rows if row["event"] == "state"]
        modern = any(row["event"] == "trace_format" and row.get("version") == 3 for row in rows)
        config = dict(self.config, knowledge_sha256=self.knowledge_hash)
        if not modern:
            config["sample_gap_frames"] = 32
        series, events = self._analyze_samples(samples, config)
        losses, unattributed = self._analyze_losses(rows)
        events.extend(losses)
        actions = execution_summary(rows, max((row["frame"] for row in rows), default=0), config)
        events.extend(actions["events"])
        quality = {source: dict(problems=[p for p in problems if source in p])
                   for source in ("state", "trace", "events")}
        if not modern:
            for name in ("game_started", "game_ended"):
                if not any(row["event"] == name for row in rows):
                    quality["trace"]["problems"].append("missing_" + name)
        frames = [sample["frame"] for sample in samples]
        gaps = [[a, b] for a, b in zip(frames, frames[1:]) if b - a > config["sample_gap_frames"]]
        quality["state"].update(samples=len(samples), first_frame=frames[0] if frames else None,
            last_frame=frames[-1] if frames else None, gaps=gaps,
            covered_frames=sum(b - a for a, b in zip(frames, frames[1:]) if 0 < b - a <= config["sample_gap_frames"]))
        if not samples:
            quality["state"]["problems"].append("no_state_samples")
        quality["trace"]["exact_calls"] = not any(row["event"] == "module_summary" for row in rows)
        quality["unattributed_deaths"] = unattributed
        quality["missing_metrics"] = dict(mineral_income="Separate mineral income is not derived",
            gas_income="Separate gas income is not derived", upgrade_state="Upgrade state is not recorded")
        if any(value is None for value in series.get("army_supply", [])):
            quality["missing_metrics"]["army_supply"] = "Unknown static unit supply at null samples; workers excluded"
        for name in ("combat_value", "trade_efficiency", "army_value", "army_investment", "resource_collection_rate"):
            if not any(value is not None for value in series.get(name, [])):
                quality["missing_metrics"][name] = "Required native score is unavailable"
        return dict(schema_version=3, config=config, series=series,
                    events=sorted(events, key=lambda event: event["frame"]), quality=quality)

    def _analyze_samples(self, samples, config):
        series, events, active = defaultdict(list), [], {}
        unit_types = {unit["type"] for sample in samples for unit in sample["own"]}
        for index, sample in enumerate(samples):
            frame = sample["frame"]
            next_frame = samples[index + 1]["frame"] if index + 1 < len(samples) else frame
            covered = 0 < next_frame - frame <= config["sample_gap_frames"]
            units = sample["own"]
            counts = Counter(unit["type"] for unit in units)
            workers = [unit for unit in units if unit["type"] in WORKERS]
            values = dict(supply_left=sample["supply_cap"] - sample["supply_used"], workers=len(workers),
                idle_workers=sum(not unit["orders"] for unit in workers), bases=sum(counts[t] for t in BASES),
                supply_block_ratio=int(sample["supply_used"] >= sample["supply_cap"]))
            values.update({"unit_count." + name: counts[name] for name in unit_types})
            score = sample.get("score")
            score_names = {"combat_value", "trade_efficiency", "army_value", "army_investment", "resource_collection_rate"}
            values.update(dict.fromkeys(score_names))
            required = {"killed_value_units", "killed_value_structures", "used_minerals_army", "used_vespene_army",
                        "total_used_minerals_army", "total_used_vespene_army", "collected_minerals", "collected_vespene"}
            if score and required <= score.keys():
                killed = score["killed_value_units"] + score["killed_value_structures"]
                lost = sum(value for name, value in score.items() if name.startswith("lost_"))
                values.update(combat_value=killed, trade_efficiency=killed / (killed + lost) if killed + lost else 0,
                    army_value=score["used_minerals_army"] + score["used_vespene_army"],
                    army_investment=score["total_used_minerals_army"] + score["total_used_vespene_army"],
                    resource_collection_rate=60 * LOOPS * (score["collected_minerals"] + score["collected_vespene"]) / frame if frame else None)
            army_supply, complete = 0, True
            for unit in units:
                entry = self.catalog.get(unit["type"], {})
                if unit["type"] in WORKERS or entry.get("category", "").lower() in {"building", "add-on", "structure"}:
                    continue
                supply = entry.get("data", {}).get("supply")
                if supply is None:
                    complete = False
                else:
                    army_supply += supply
            values["army_supply"] = army_supply if complete else None
            flags = dict(supply_block=bool(values["supply_block_ratio"]),
                resource_float=sample["minerals"] >= config["float_minerals"] or sample["vespene"] >= config["float_vespene"])
            for name in PRODUCERS:
                ready = [unit for unit in units if unit["type"] == name and unit["build_progress"] >= 1]
                busy = sum(bool(unit["orders"]) for unit in ready)
                values["production_busy_ratio." + name] = busy / len(ready) if ready else None
                flags["production_idle." + name] = busy < len(ready)
            for name, value in values.items():
                series[name].append(value)
            for kind in set(active) | set(flags):
                if kind in active and (not flags.get(kind) or not covered or active[kind]["end_frame"] != frame):
                    event = active.pop(kind)
                    if event["end_frame"] - event["frame"] >= config["event_frames"]:
                        events.append(event)
                if flags.get(kind) and covered:
                    event = active.setdefault(kind, dict(frame=frame, end_frame=next_frame,
                        kind=kind.partition(".")[0], data=dict(facility=kind.partition(".")[2]) if "." in kind else {}))
                    event["end_frame"] = next_frame
        for event in active.values():
            if event["end_frame"] - event["frame"] >= config["event_frames"]:
                events.append(event)
        return {name: values for name, values in series.items()
                if not name.startswith("production_busy_ratio.") or any(value is not None for value in values)}, events

    def _analyze_losses(self, rows):
        known, events, unattributed = {}, [], 0
        for row in rows:
            if row["event"] == "state":
                known.update({unit["tag"]: unit for unit in row["own"]})
            elif row["event"] == "unit_created":
                known[row["tag"]] = dict(type=row["type"])
            elif row["event"] == "unit_destroyed":
                unit = known.pop(row["tag"], None)
                if unit is None:
                    unattributed += 1
                    continue
                category = self.catalog.get(unit["type"], {}).get("category", "").lower()
                category = "worker" if unit["type"] in WORKERS else "army" if category in {"unit", "combat unit", "army", "combat"} else "unknown"
                events.append(dict(frame=row["frame"], kind="unit_loss", data=dict(tag=row["tag"], type=unit["type"], category=category)))
        for category in ("worker", "army"):
            deaths = [event for event in events
                      if event["kind"] == "unit_loss" and event["data"]["category"] == category]
            clusters(deaths, category + "_loss_spike", self.config[category + "_loss_count"], events, self.config)
        return events, unattributed

    def analyze_batch(self, batch):
        if batch["purpose"] == "holdout":
            raise ValueError("Holdout is unavailable to the development Analyzer")
        reports = {}
        for run_id, path in batch["runs"].items():
            rows, problems = read_records(project_path(path), run_id)
            reports[run_id] = run_view(path, run_id, self._analyze(rows, problems), rows)
        summary = dict(outcomes=dict(Counter(report["outcome"].get("outcome") or "unknown" for report in reports.values())),
                       event_counts=dict(Counter(event["kind"] for report in reports.values() for event in report["events"])))
        return dict(batch_id=batch["batch_id"], version=batch["version"], purpose=batch["purpose"], runs=reports, summary=summary)


def analyze_evidence(analyzer, batch, batch_path, analysis_path):
    analysis = analyzer.analyze_batch(batch)
    save_batch_analysis(analysis_path, analysis)
    return dict(batch=batch, analysis=analysis, batch_path=path_reference(batch_path), analysis_path=path_reference(analysis_path))
