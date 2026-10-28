"""Decode the current Bot telemetry format without rewriting source files."""
import json
from pathlib import Path


def read_records(directory, run_id, *, files=("trace", "state", "events")):
    records, problems = [], []
    directory = Path(directory)
    modules, calls, requests, errors = {}, {}, {}, {}
    for name in files:
        path = directory / f"{name}.jsonl"
        if not path.exists():
            problems.append("missing_" + name)
            continue
        previous = -1
        with path.open(encoding="utf-8") as stream:
            try:
                schema = json.loads(stream.readline())
                if schema["kind"] != "schema" or schema["version"] != 3:
                    raise ValueError("Unsupported schema")
            except (ValueError, TypeError, KeyError):
                problems.append("invalid_schema:" + name)
                continue
            for number, line in enumerate(stream, 2):
                ref = f"{run_id}:{name}:L{number}"
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("Expected object")
                    if name == "state":
                        start, step = row["start_frame"], schema["frame_step"]
                        if type(start) is not int or start < 0 or type(step) is not int or step < 1:
                            raise ValueError("Invalid frame origin or step")
                        frames = range(start, start + len(row["minerals"]) * step, step)
                        columns = {k: v for k, v in row.items() if k not in {"start_frame", "score"}}
                        if any(not isinstance(v, list) or len(v) != len(frames) for v in columns.values()):
                            raise ValueError("Misaligned columns")
                        if any(len(v) != len(frames) for v in row["score"].values()):
                            raise ValueError("Misaligned score columns")
                        expanded = []
                        for index, frame in enumerate(frames):
                            sample = {k: v[index] for k, v in columns.items()}
                            for side in ("own", "enemies"):
                                units = []
                                for values in sample[side]:
                                    unit = dict(zip(schema["unit_columns"], values, strict=True))
                                    unit["orders"] = [dict(zip(schema["order_columns"], order, strict=True))
                                                      for order in unit["orders"]]
                                    units.append(unit)
                                sample[side] = units
                            sample["score"] = {k: v[index] for k, v in row["score"].items() if v[index] is not None}
                            if not sample["score"]:
                                sample.pop("score")
                            expanded.append(dict(event="state", frame=frame, **sample,
                                                 source_ref=f"{ref}#{index}"))
                    else:
                        frame, kind, data = row["frame"], row["kind"], row["data"]
                        expanded = []
                        if kind == "module":
                            modules[data["id"]] = {k: data[k] for k in ("module_id", "module_path", "path")}
                        elif kind == "exception":
                            errors[data["id"]] = data["traceback"]
                            expanded.append(dict(event="exception", frame=frame, **data))
                        elif kind == "module_calls":
                            for values in data:
                                call = dict(zip(schema["call_columns"], values, strict=True))
                                call.update(modules[call["module_ref"]])
                                detail = call.pop("detail")
                                if call["status"] == "exception":
                                    call["exception"] = errors[detail]
                                elif detail is not None:
                                    call["skip_reason"] = detail
                                elif call["status"] == "invoked_false":
                                    call["reason"] = "no_intent"
                                call = dict(call, event="module_call", frame=frame)
                                calls[call["call_id"]] = call
                                expanded.append(call)
                        elif kind in {"action_request", "action_status"}:
                            request_id = data["request_id"]
                            if kind == "action_status" and request_id not in requests:
                                raise ValueError("Unresolved request")
                            parameters = dict(requests.get(request_id, {}), **data)
                            call = calls[parameters["call_id"]]
                            parameters.update(source=call["path"], priority=call["priority"])
                            requests[request_id] = parameters
                            expanded.append(dict(event="action", frame=frame, **parameters))
                        elif kind == "action_error":
                            expanded.append(dict(event="action", frame=frame, **data))
                        else:
                            expanded.append(dict(event=kind, frame=frame, **data))
                    for item in expanded:
                        if type(item.get("frame")) is not int or not isinstance(item.get("event"), str):
                            raise ValueError("Invalid event")
                    for item in expanded:
                        if item["frame"] < previous or name == "state" and item["frame"] == previous:
                            problems.append(f"frame_decreased:{name}:{number}")
                        previous = item["frame"]
                        records.append(dict(item, source_ref=item.get("source_ref", ref)))
                except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                    problems.append(f"corrupt_line:{name}:{number}")
    for call in calls.values():
        if call["parent_id"] is not None and call["parent_id"] not in calls:
            problems.append(f"unresolved_parent:trace:{call['call_id']}")
    records.sort(key=lambda row: row["frame"])
    return records, problems
