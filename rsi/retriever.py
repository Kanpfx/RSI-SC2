"""Unified bounded queries over static analysis, raw traces and frozen references."""
import json
from collections import Counter

from .analyzer import metric_statistics, metric_summary, read_records, execution_summary
from .references import References
from runner.artifacts import project_path, read_json


class Retriever:
    def __init__(self, source_root, context, max_chars=16000, *, analyses=(), max_items=40, memory_path=None):
        """Bind static batch reports and one parent snapshot with frozen references."""
        self.context, self.max_chars = project_path(context), max_chars
        self.memory_path = project_path(memory_path) if memory_path is not None else None
        self.references = References(source_root, context, max_chars=max_chars)
        self.max_items = max_items
        self.batches = {batch["batch_id"]: batch for batch in analyses}
        if any(batch["purpose"] == "holdout" for batch in analyses):
            raise ValueError("Holdout is unavailable to the development Retriever")
        self.records = {}

    def _records(self, report, source="trace"):
        """Load original records lazily; static analysis queries need no raw-log cache."""
        log_path = report["log_path"]
        key = (log_path, source)
        if key not in self.records:
            # Old recordings mixed observations into trace, so state queries include both files.
            files = ("trace", "state") if source == "state" else ("trace",)
            self.records[key], _ = read_records(project_path(log_path).parent, report["run_id"], files=files)
        return self.records[key]

    def query_logs(self, kind, batch_id, runs="all", **filters):
        """Return summaries by default; paginate explicit raw data with bounded references."""
        allowed = dict(overview=set(), metrics={"names"}, events={"types"},
                       trace={"module", "call_id", "request_id", "status"}, state=set(), compare={"names", "reference_batch"})
        unknown = set(filters) - allowed[kind] - {"frame", "offset", "detail"}
        if unknown:
            raise ValueError(f"Unknown {kind} filters: {sorted(unknown)}")
        detail = filters.get("detail", "summary")
        if detail not in {"summary", "raw"}:
            raise ValueError("detail must be summary or raw")
        batch = self.batches[batch_id]
        ids = list(batch["runs"]) if runs == "all" else runs
        if not isinstance(ids, list) or not set(ids) <= batch["runs"].keys():
            raise ValueError('runs must be "all" or a list of known run IDs')
        window = filters.get("frame", [0, float("inf")])
        if len(window) != 2 or not all(type(n) is int and n >= 0 for n in window) or window[0] > window[1]:
            if "frame" in filters:
                raise ValueError("frame must be [start_frame, end_frame] with nonnegative integers and start <= end")
        reports = [(run_id, batch["runs"][run_id]) for run_id in ids]
        coverage = {batch_id: {run_id: self._coverage(report) for run_id, report in reports}}
        if kind == "compare":
            reference = self.batches[filters["reference_batch"]]
            coverage[reference["batch_id"]] = {rid: self._coverage(r) for rid, r in reference["runs"].items()}
            compared = [(batch_id, run_id, report) for run_id, report in reports]
            compared.extend((reference["batch_id"], rid, report) for rid, report in reference["runs"].items())
            items = self._compare(compared, window, filters)
        else:
            items = (dict(item, batch_id=batch_id) for item in self._log_items(kind, reports, filters))

        offset = filters.get("offset", 0)
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        return self._paginate(items, coverage, offset)

    def _coverage(self, report):
        """Keep whole-run coverage and explicit integrity warnings, not repeated boilerplate."""
        coverage = report["coverage"]
        result = {key: coverage[key] for key in ("last_frame", "integrity", "sample_ratio")}
        for key in ("problems", "unattributed_deaths"):
            if coverage.get(key):
                result[key] = coverage[key]
        return result

    def _log_items(self, kind, reports, filters):
        """Dispatch fixed query kinds and attach each row's run identity lazily."""
        window = filters.get("frame", [0, float("inf")])
        detail = filters.get("detail", "summary")
        for run_id, report in reports:
            if kind == "overview":
                rows = self._overview(report, window, detail)
            elif kind == "metrics":
                names = filters.get("names", list(report["metrics"]))
                rows = self._metrics(report, window, names, detail)
            elif kind == "events":
                rows = self._events(report, window, filters.get("types"))
            elif kind == "trace":
                rows = self._trace(report, window, filters)
            elif kind == "state":
                rows = (dict(frame=row["frame"], kind="state", data={key: value for key, value in row.items()
                        if key not in {"event", "frame", "source_ref"}}) for row in self._records(report, "state")
                        if row["event"] == "state" and window[0] <= row["frame"] <= window[1])
            for row in rows:
                yield dict(row, run_id=run_id)

    def _paginate(self, items, coverage, offset):
        """Bound query output without materializing all matching raw records."""
        kept, chars, has_more = [], 0, False
        for index, row in enumerate(items):
            if index < offset:
                continue
            if len(kept) == self.max_items:
                has_more = True
                break
            item = compact_refs(row)
            size = len(json.dumps(item, ensure_ascii=False))
            if size > self.max_chars:
                item = {key: item[key] for key in ("run_id", "event", "frame", "kind", "end_frame",
                                                  "name", "batch_id", "call_id", "request_id", "status", "source_ref", "source_refs",
                                                  "source_ref_count")
                        if key in item}
                item.update(detail_omitted=True, hint="Narrow runs/frame or use kind-specific filters to request less data")
                size = len(json.dumps(item, ensure_ascii=False))
            if chars + size > self.max_chars:
                has_more = True
                break
            kept.append(item)
            chars += size
        result = dict(items=kept, quality=coverage)
        if has_more and kept:
            result["next_offset"] = offset + len(kept)
        if any(i.get("detail_omitted") for i in kept) or (has_more and not kept):
            result["truncated"] = True
        if not kept:
            result["reason"] = "Result exceeds output limit; narrow the query" if has_more else "No matching data"
        return result

    def _overview(self, report, window, detail):
        """Yield overview rows within the requested scope."""
        names = list(report["metrics"]) if detail == "raw" else [
            "minerals", "vespene", "workers", "idle_workers", "supply_left", "supply_block_ratio", "army_supply"]
        item = dict(**report["conditions"],
            outcome=report["outcome"].get("outcome"), termination=report["outcome"].get("termination"),
            execution_status=report["outcome"].get("execution_status"),
            frame=list(window) if window[1] != float("inf") else [window[0], report["coverage"]["last_frame"]],
            metrics={n: metric_summary(n, report, window) for n in names},
            metric_names=sorted(set(report["metrics"]) | set(report["counts"])),
            module_exceptions=sum(window[0] <= frame <= window[1] for frame in report["counts"]["module_exceptions"]),
            events=dict(Counter(e["kind"] for e in self._events(report, window, None))))
        if detail == "raw":
            execution = self._execution(report, window)
            item.update(modules=execution["modules"], actions=execution["actions"], result=report["outcome"])
        yield item

    def _metrics(self, report, window, names, detail):
        """Yield metrics rows within the requested scope."""
        for name in names:
            item = dict(name=name, statistics=metric_summary(name, report, window))
            if name not in report["metrics"] and name not in report["counts"] and metric_statistics(name)[0] != "count":
                item["reason"] = report["coverage"]["missing_metrics"].get(name, "Metric unavailable or no samples in window")
            yield item
            if detail == "raw":
                if name in report["counts"]:
                    frames = [frame for frame in report["counts"][name] if window[0] <= frame <= window[1]]
                    for start in range(0, len(frames), 128):
                        yield dict(name=name, frame=frames[start:start + 128])
                else:
                    selected = [(frame, value) for frame, value in zip(report["frame"], report["metrics"].get(name, []))
                                if window[0] <= frame <= window[1]]
                    for start in range(0, len(selected), 128):
                        points = selected[start:start + 128]
                        yield dict(name=name, frame=[frame for frame, _ in points], values=[value for _, value in points])

    def _events(self, report, window, types):
        """Yield events rows within the requested scope."""
        for event in report["events"]:
            if (event["frame"] <= window[1] and event.get("end_frame", event["frame"] + 1) > window[0]
                    and (not types or event["kind"] in types)):
                yield event

    def _compare(self, reports, window, filters):
        """Return each side once, with explicit batch identity and no assumed run pairing."""
        names = filters.get("names", sorted({name for _, _, report in reports for name in report["metrics"]}))
        for batch_id, run_id, report in reports:
            item = dict(batch_id=batch_id, run_id=run_id, **report["conditions"],
                metrics={name: metric_summary(name, report, window) for name in names},
                events=dict(Counter(e["kind"] for e in self._events(report, window, None))),
                frame=[window[0], min(window[1], report["coverage"]["last_frame"])])
            if filters.get("detail") == "raw":
                item["modules"] = self._execution(report, window)["modules"]
            yield item

    def _execution(self, report, window):
        if window[0] == 0 and window[1] >= report["coverage"]["last_frame"]:
            return report
        if not report["analysis"]["quality"]["trace"]["exact_calls"]:
            raise ValueError("Historical v2 module totals are run-wide; query trace for the original window summaries")
        return execution_summary(self._records(report), report["coverage"]["last_frame"],
                                 report["analysis"]["config"], window)

    def _trace(self, report, window, filters):
        """Follow native call ancestry and request histories, including later confirmations."""
        rows = self._records(report)
        calls = {r["call_id"]: r for r in rows if r["event"] == "module_call"}
        selected = {cid for cid, r in calls.items() if window[0] <= r["frame"] <= window[1]
                    and (not filters.get("module") or filters["module"] in (r["module_id"], r["module_path"], r["path"]))
                    and (not filters.get("call_id") or cid == filters["call_id"])}
        if filters.get("status") in {"invoked_true", "invoked_false", "skipped", "exception"}:
            selected &= {cid for cid, row in calls.items() if row["status"] == filters["status"]}
        if filters.get("request_id"):
            selected = {r.get("call_id") for r in rows if r.get("request_id") == filters["request_id"]}
        descendants = set(selected)
        for _ in range(len(calls)):
            added = {cid for cid, r in calls.items() if r.get("parent_id") in descendants} - descendants
            if not added:
                break
            descendants.update(added)
        linked = set(descendants)
        for cid in list(descendants):
            parent = calls.get(cid, {}).get("parent_id")
            while parent and parent not in linked:
                linked.add(parent)
                parent = calls.get(parent, {}).get("parent_id")
        request_ids = {r["request_id"] for r in rows if r["event"] == "action"
                       and r.get("request_id") and r.get("call_id") in descendants
                       and (not filters.get("request_id") or r["request_id"] == filters["request_id"])}
        if filters.get("status") and filters["status"] not in {"invoked_true", "invoked_false", "skipped", "exception"}:
            request_ids &= {r.get("request_id") for r in rows if r.get("status") == filters["status"]}
        for row in rows:
            if (row["event"] == "module_summary" and not filters.get("call_id") and not filters.get("request_id")
                    and row["start_frame"] <= window[1] and row["frame"] >= window[0]):
                for item in row["modules"]:
                    if (not filters.get("module") or filters["module"] in (item["module_id"], item["module_path"], item["path"])) \
                            and (not filters.get("status") or item.get(filters["status"], 0)):
                        yield dict(kind="module_summary", frame=row["start_frame"], end_frame=row["frame"], data=item)
            if ((row["event"] == "module_call" and row["call_id"] in linked)
                    or (row["event"] == "action" and row.get("request_id") in request_ids)
                    or (row["event"] == "action" and not row.get("request_id")
                        and window[0] <= row["frame"] <= window[1]
                        and (not filters.get("status") or row.get("status") == filters["status"])
                        and not any(filters.get(key)
                        for key in ("module", "call_id", "request_id")))):
                yield dict(frame=row["frame"], kind=row["event"], data={key: value for key, value in row.items()
                           if key not in {"frame", "event", "source_ref"}})

    def read_reference(self, path, lines=None, key=None):
        """Route reference queries through the shared reader with this query's output limit."""
        self.references.max_chars = self.max_chars
        return self.references.read_reference(path, lines=lines, key=key)

    def query_memory(self, text=""):
        """Match text against the experiences frozen for this investigation."""
        if self.memory_path is None:
            raise ValueError("Memory requires an explicit frozen iteration path")
        kept, size = [], 0
        for value in read_json(self.memory_path):
            serialized = json.dumps(value, ensure_ascii=False)
            if text and text.casefold() not in serialized.casefold():
                continue
            length = len(serialized)
            if size + length > self.max_chars:
                return dict(items=kept, truncated=True, reason="Matching experiences exceed output limit; narrow text, e.g. by iteration_id")
            kept.append(value)
            size += length
        return dict(items=kept) if kept else dict(items=[], reason="No matching experiences")



def compact_refs(value):
    """Keep representative evidence references in responses; full lists remain in artifacts."""
    if isinstance(value, list):
        return [compact_refs(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: compact_refs(item) for key, item in value.items() if key != "source_refs"}
    if "source_refs" in value:
        references = value["source_refs"]
        result["source_refs"] = references if len(references) <= 3 else references[:2] + references[-1:]
        if len(references) > 3:
            result["source_ref_count"] = len(references)
    return result
