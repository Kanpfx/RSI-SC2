"""Unified bounded queries over static analysis, raw traces and frozen references."""
import ast
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

from .tools.versions import project_path, source_paths, read_json, write_json

from .analyzer import LOOPS, metric_statistics, metric_summary, read_records


def make_tool(tool_name, description, function, input_type):
    """Bind a read-only SDK tool to an ordinary function and its argument schema."""
    from openhands.sdk.tool import Observation, ToolDefinition, ToolExecutor, ToolAnnotations

    class Output(Observation):
        pass

    class Executor(ToolExecutor):
        def __call__(self, action, conversation=None):
            """Return input errors as observations so the model can correct its call."""
            try:
                value = function(**action.model_dump(exclude_none=True, by_alias=True,
                                                     include=set(type(action).model_fields)))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                value = dict(status="invalid_input", error=dict(code="invalid_input", message=str(exc)))
            return Output.from_text(json.dumps(value, ensure_ascii=False))

    class Definition(ToolDefinition):
        @classmethod
        def create(cls, conv_state=None, **params):
            """Construct the bound SDK-native tool definition."""
            return [cls(description=description, action_type=input_type, observation_type=Output,
                        executor=Executor(), annotations=ToolAnnotations(
                            readOnlyHint=True, destructiveHint=False, openWorldHint=False))]

    tool = type("Rsi" + tool_name.title().replace("_", ""), (Definition,), {"name": tool_name})
    return tool.create()[0]


def reference_tool(function):
    """Share the same read_reference schema and implementation across both agents."""
    from pydantic import ConfigDict
    from openhands.sdk.tool import Action

    class ReferenceInput(Action):
        model_config = ConfigDict(extra="forbid")
        path: str
        lines: list[int] | None = None
        key: str | None = None

    return make_tool("read_reference", "Read indexed source/API/knowledge. Paths begin source/ (relative to bot/), "
        "api/ or knowledge/. Text: lines=[start,end], 1-based; defaults to [1,120], at most 120 lines per read. "
        "JSON: key selects an entry with dot-separated keys; omit key to list entry names. "
        "Use lines for text and key for JSON. Read-only.", function, ReferenceInput)


def freeze_context(project, directory, experiences):
    """Freeze reference inputs once so later queries retain stable provenance."""
    directory = Path(directory)
    shutil.copytree(Path(project) / "knowledge", directory / "knowledge")
    spec = importlib.util.find_spec("sc2")
    if spec and spec.origin:
        api_root = Path(spec.origin).parent
        for path in api_root.rglob("*.py"):
            target = directory / "api" / path.relative_to(api_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    write_json(directory / "memory.json", experiences)


class Retriever:
    def __init__(self, version, context, max_chars=16000, *, analyses=(), max_items=40):
        """Bind static batch reports and one parent snapshot with frozen references."""
        self.context, self.max_chars = project_path(context), max_chars
        self.roots = dict(source=project_path(version["source_root"]) / "bot",
                          knowledge=self.context / "knowledge", api=self.context / "api")
        self.max_items = max_items
        self.batches = {batch["batch_id"]: batch for batch in analyses}
        if any(batch["purpose"] == "holdout" for batch in analyses):
            raise ValueError("Holdout is unavailable to the development Retriever")
        self.records = {}

    def index(self):
        """Expose names only; reference catalogues are retrieved on demand."""
        return dict({kind: sorted(f"{kind}/{path.relative_to(root).as_posix()}" for path in source_paths(root))
                     for kind, root in self.roots.items()},
                    memory_count=len(read_json(self.context / "memory.json")))

    def _records(self, report):
        """Load original records lazily; static analysis queries need no raw-log cache."""
        run_id = report["run_id"]
        if run_id not in self.records:
            self.records[run_id], _ = read_records(project_path(report["log_path"]).parent, run_id)
        return self.records[run_id]

    def query_logs(self, kind, batch_id, runs="all", **filters):
        """Return summaries by default; paginate explicit raw data with bounded references."""
        allowed = dict(overview=set(), metrics={"names"}, events={"types"},
                       trace={"module", "call_id", "request_id", "status"}, compare={"names", "reference_batch"})
        unknown = set(filters) - allowed[kind] - {"time", "offset", "detail"}
        if unknown:
            raise ValueError(f"Unknown {kind} filters: {sorted(unknown)}")
        detail = filters.get("detail", "summary")
        if detail not in {"summary", "raw"}:
            raise ValueError("detail must be summary or raw")
        batch = self.batches[batch_id]
        ids = list(batch["runs"]) if runs == "all" else runs
        if not isinstance(ids, list) or not set(ids) <= batch["runs"].keys():
            raise ValueError("Invalid run scope")
        window = filters.get("time", [0, float("inf")])
        if len(window) != 2 or not all(isinstance(n, (int, float)) for n in window) or window[0] > window[1]:
            raise ValueError("time must be [start_second, end_second]")
        coverage = {i: batch["runs"][i]["coverage"] for i in ids}
        if kind == "compare":
            reference = self.batches[filters["reference_batch"]]
            coverage.update({rid: r["coverage"] for rid, r in reference["runs"].items()})

        def items():
            """Yield matching rows without copying an entire raw result before pagination."""
            for run_id in ids:
                report = batch["runs"][run_id]
                if kind == "overview":
                    records = self._records(report)
                    names = list(report["metrics"]) if detail == "raw" else [
                        "minerals", "vespene", "workers", "idle_workers", "supply_left", "supply_block_ratio", "army_supply"]
                    yield run_id, dict(conditions=report["conditions"], outcome=report["outcome"],
                        metrics={n: metric_summary(n, report["metrics"].get(n, []), window) for n in names},
                        metric_names=sorted(report["metrics"]),
                        modules=report["modules"] if detail == "raw" else dict(
                            count=len(report["modules"]), exceptions=sum(m["exceptions"] for m in report["modules"].values())),
                        actions=report["actions"],
                        events=report["events"][:10] if detail == "raw" else dict(Counter(
                            e["kind"] for e in report["events"] if e["start"] <= window[1] and e["end"] >= window[0])),
                        source_refs=([records[0]["source_ref"], records[-1]["source_ref"]]
                                     if records else []))
                elif kind == "metrics":
                    names = filters.get("names", list(report["metrics"]))
                    for name in names:
                        rows = [r for r in report["metrics"].get(name, []) if window[0] <= r["time"] <= window[1]]
                        yield run_id, dict(name=name, statistics=metric_summary(name, rows),
                            allowed_statistics=metric_statistics(name),
                            source_refs=[ref for row in rows for ref in row.get("source_refs", [])],
                            missing_reason=None if rows else "Metric unavailable or no samples in window")
                    if detail == "raw":
                        for name in names:
                            for row in report["metrics"].get(name, []):
                                if window[0] <= row["time"] <= window[1]:
                                    yield run_id, dict(row, name=name)
                elif kind == "events":
                    for event in report["events"]:
                        if (event["start"] <= window[1] and event["end"] >= window[0]
                                and (not filters.get("types") or event["kind"] in filters["types"])):
                            yield run_id, event
                elif kind == "trace":
                    for row in self._trace(report, window, filters):
                        yield run_id, row
                elif kind == "compare":
                    names = filters.get("names", list(report["metrics"]))
                    yield run_id, dict(conditions=report["conditions"],
                        metrics={n: metric_summary(n, report["metrics"].get(n, []), window) for n in names},
                        modules=report["modules"], events=dict(Counter(e["kind"] for e in report["events"])),
                        reference={rid: dict(conditions=r["conditions"],
                            metrics={n: metric_summary(n, r["metrics"].get(n, []), window) for n in names},
                            modules=r["modules"], events=dict(Counter(e["kind"] for e in r["events"])))
                            for rid, r in reference["runs"].items()},
                        source_refs=[row["source_ref"] for row in self._records(report) if row["event"] == "state"] +
                                    [row["source_ref"] for rid in reference["runs"] for row in self._records(reference["runs"][rid])
                                     if row["event"] == "state"])
        offset = filters.get("offset", 0)
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be nonnegative")
        kept, chars, truncated = [], 0, False
        for index, (run_id, row) in enumerate(items()):
            if index < offset:
                continue
            if len(kept) == self.max_items:
                truncated = True
                break
            item = compact_refs(dict(row, run_id=run_id))
            size = len(json.dumps(item, ensure_ascii=False))
            if size > self.max_chars:
                item = {key: item[key] for key in ("run_id", "event", "frame", "kind", "start", "end",
                                                  "name", "call_id", "request_id", "status", "source_ref", "source_refs")
                        if key in item}
                item.update(detail_omitted=True, hint="Narrow time/runs/names or retrieve the source record")
                size = len(json.dumps(item, ensure_ascii=False))
            if chars + size > self.max_chars:
                truncated = True
                break
            kept.append(item)
            chars += size
        return dict(items=kept, source_refs=sorted(set(refs(kept))), coverage=coverage,
                    truncated=truncated or any(i.get("detail_omitted") for i in kept),
                    missing_reason=None if kept else "No matching data or item exceeds output budget",
                    next_offset=offset + len(kept) if truncated and kept else None)

    def _trace(self, report, window, filters):
        """Follow native call ancestry and request histories, including later confirmations."""
        rows = self._records(report)
        calls = {r["call_id"]: r for r in rows if r["event"] == "module_call"}
        selected = {cid for cid, r in calls.items() if window[0] <= r["frame"] / LOOPS <= window[1]
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
        if filters.get("status"):
            request_ids &= {r.get("request_id") for r in rows if r.get("status") == filters["status"]}
        for row in rows:
            if ((row["event"] == "module_call" and row["call_id"] in linked)
                    or (row["event"] == "action" and row.get("request_id") in request_ids)
                    or (filters.get("detail") == "raw" and row["event"] == "state"
                        and window[0] <= row["frame"] / LOOPS <= window[1])):
                yield row

    def read_reference(self, path, lines=None, key=None):
        """Read frozen text through the SDK or retrieve a JSON entry by key."""
        kind, _, relative = path.partition("/")
        if kind not in self.roots:
            raise ValueError("Reference paths start with source/, api/ or knowledge/")
        root = self.roots[kind].resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file() or target.suffix not in {".py", ".md", ".json"}:
            raise ValueError("Reference path exceeds scope or is unavailable")
        raw = target.read_bytes()
        ref = f"{kind}:{hashlib.sha256(raw).hexdigest()}:{relative}"
        if target.suffix == ".json":
            if lines is not None:
                raise ValueError("Use key for JSON entries; lines is for text files")
            value = json.loads(raw)
            if key is None:
                item = dict(path=path, keys=list(value), source_ref=ref)
            else:
                for part in key.split("."):
                    value = value[part]
                item = dict(path=path, key=key, value=value, source_ref=ref + ":" + key)
            return self._reference_result([item])
        if key is not None:
            raise ValueError("Use lines for text files; key is for JSON entries")
        lines = [1, 120] if lines is None else lines
        if len(lines) != 2 or not all(isinstance(n, int) for n in lines) or not 1 <= lines[0] <= lines[1]:
            raise ValueError("lines must be [start_line, end_line], starting at 1")
        start, end = lines[0], min(lines[1], lines[0] + 119)
        from openhands.tools.file_editor.editor import FileEditor
        from openhands.tools.file_editor.exceptions import ToolError
        try:
            source = FileEditor().read_file(target, start_line=start, end_line=end + 1).splitlines()
        except ToolError as exc:
            raise ValueError(str(exc)) from exc
        count = end - start + 1
        clipped = len(source) > count
        if not source:
            return self._reference_result([])
        end = start + min(len(source), count) - 1
        item = dict(path=path, start_line=start, end_line=end,
                    source="\n".join(source[:count]), source_ref=f"{ref}:{start}-{end}")
        return self._reference_result([item], clipped)

    def query_memory(self, text=""):
        """Match text against the experiences frozen for this investigation."""
        values = read_json(self.context / "memory.json")
        return self._reference_result([dict(v, source_ref=f"memory:{v['iteration_id']}") for v in values
            if not text or text.casefold() in json.dumps(v, ensure_ascii=False).casefold()])

    def _reference_result(self, items, clipped=False):
        """Bound reference output and retain its source locations."""
        kept, size = [], 0
        for item in items:
            length = len(json.dumps(item, ensure_ascii=False))
            if size + length > self.max_chars:
                break
            kept.append(item)
            size += length
        return dict(items=kept, source_refs=[i["source_ref"] for i in kept],
                    coverage={"scope": "frozen references, not match observations"},
                    truncated=clipped or len(kept) < len(items), missing_reason=None if kept else "No match or item exceeds budget")


def symbols(node, prefix=""):
    """Yield qualified Python class/function names without importing candidate code."""
    for child in getattr(node, "body", []):
        if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = prefix + child.name
            yield name, child
            yield from symbols(child, name + ".")


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



def refs(value):
    """Extract only source references actually present in a returned object."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "source_ref":
                yield item
            elif key == "source_refs":
                yield from item
            else:
                yield from refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from refs(item)

