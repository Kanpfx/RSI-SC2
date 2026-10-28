"""Current-format match evidence and scripted model inputs for offline RSI tests."""
import json
from pathlib import Path

from bot.telemetry.logger import Logger
from runner.artifacts import write_json


ROOT = Path(__file__).resolve().parents[1]


def fixture_batch(project, version, conditions, directory, purpose, parallel=3, *, wall_budget=None):
    """Write the same telemetry format as the Bot without starting SC2."""
    directory = Path(directory)
    directory.mkdir(parents=True)
    batch = dict(batch_id=directory.name, version=version["version"], purpose=purpose, runs={})
    for index, condition in enumerate(conditions):
        run_id = directory.name + f"_{index}"
        path = directory / run_id
        batch["runs"][run_id] = str(path)
        write_json(path / "manifest.json", dict(condition, run_id=run_id,
                   purpose=purpose, version=version["version"], actual={}))
        write_json(path / "result.json", dict(schema_version=3, execution_status="normal",
                   termination="time_limit", outcome="draw", last_frame=3360, steps=1681))
        logger = Logger(path)
        for frame in range(0, 3361, 2):
            logger.emit("state", frame, minerals=600, vespene=0, supply_used=15, supply_cap=15,
                score=dict(killed_value_units=1000 * frame / 3360, killed_value_structures=200 * frame / 3360,
                    lost_minerals_army=100 * frame / 3360, lost_vespene_army=50 * frame / 3360,
                    used_minerals_army=400, used_vespene_army=300,
                    total_used_minerals_army=800 * frame / 3360, total_used_vespene_army=600 * frame / 3360,
                    collected_minerals=3000 * frame / 3360, collected_vespene=1000 * frame / 3360),
                own=[dict(tag=1, type="SCV", build_progress=1, orders=[], health=45, shield=0,
                          position=[0, 0])], enemies=[])
        call = dict(frame=16, call_id="call1", parent_id="", priority=50,
                    module_id="Economy", module_path="bot.components.tasks.economy",
                    path="Economy", status="invoked_true")
        request = dict(frame=16, call_id="call1", request_id="request1", status="proposed",
                       reason="", source="Economy", priority=50, unit_tag=1, ability="MOVE",
                       target=[1, 1], product=None, queue=False)
        logger._execution(16, [call], [request, dict(request, status="submitted", reason="local_queued")])
        logger._execution(32, [], [dict(request, frame=32, status="observed", reason="unit_order")])
        logger.close()
    write_json(directory / "batch.json", batch)
    return batch


def proposal():
    """Describe a scoped intervention without changing evaluation rules."""
    return dict(change="Fixture intervention", preserve="Keep economy behavior",
                verification="Inspect worker growth and check that supply and technology remain healthy.",
                code_targets=["bot/components/tasks/economy.py"])


def task(task_id="T1", hypothesis_id="0", **changes):
    """Return one independent intervention linked to an existing hypothesis."""
    return dict(id=task_id, hypothesis_ids=[hypothesis_id], **dict(proposal(), **changes))


def plan():
    """Return a single-task diagnosis plan."""
    return dict(tasks=[task()])


def sdk_messages(calls):
    """Build prescribed SDK tool calls without contacting a model endpoint."""
    from openhands.sdk.llm import Message, MessageToolCall, TextContent
    return [Message(role="assistant", content=[TextContent(text="Offline fixture")], tool_calls=[
        MessageToolCall(id=str(index), name=name, arguments=json.dumps(arguments), origin="completion")])
        for index, (name, arguments) in enumerate(calls)]
