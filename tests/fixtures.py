"""Current-format match evidence and scripted model inputs for offline RSI tests."""
import json
from pathlib import Path
from types import SimpleNamespace

from bot.telemetry.logger import Logger
from runtime.workspace.storage import write_json


ROOT = Path(__file__).resolve().parents[1]


def metric():
    return dict(name="gas", definition="Mean observed gas balance", inputs=["gas.vespene"],
                units="vespene")


def evidence(observation="Recorded gas trajectory"):
    return [dict(source="run_01/gas.bin; frames 0-3360; analysis.json", observation=observation)]


def problem():
    return dict(problem="fixture", conditions="Observed parent games", impact="Delayed production",
                evidence=evidence(), uncertainty="Limited repetitions")


def hypotheses():
    return [dict(id=str(i), explanation=f"Fixture mechanism {i}", evidence=evidence(f"Fixture observation {i}"),
                 predictions=dict(support="a", refute="b"), verification="Compare resource and request windows")
            for i in range(2)]


def tests():
    return [dict(hypothesis_id=str(i), method="Compare resource and request windows",
                 evidence=evidence(f"Fixture prediction {i} matches observations"), outcome="supported",
                 interpretation=f"Fixture mechanism {i} supported") for i in range(2)]


def diagnosis():
    return dict(mechanism="Fixture mechanism", hypothesis_ids=["0", "1"],
                expected_change="Production resumes", uncertainty="Limited repetitions")


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
        namespace = dict(__name__="fixture_logger", __package__="bot.telemetry")
        logger_path = Path(version["source_root"]) / "bot/telemetry/logger.py"
        exec(compile(logger_path.read_text(encoding="utf-8-sig"), str(logger_path), "exec"), namespace)
        logger = namespace["Logger"](path)
        for frame in range(0, 3361, 2):
            score = dict(killed_value_units=1000 * frame / 3360,
                         killed_value_structures=200 * frame / 3360)
            logger.observe(SimpleNamespace(minerals=600, vespene=0,
                state=SimpleNamespace(game_loop=frame, score=SimpleNamespace(**score))))
        logger.emit("construction_started", 16, tag=1, type="BARRACKS")
        logger.emit("construction_complete", 32, tag=1, type="BARRACKS")
        logger.emit("game_ended", 3360, result="Result.Tie")
        logger.close()
    write_json(directory / "batch.json", batch)
    return batch


def proposal():
    """Describe a scoped intervention without changing evaluation rules."""
    return dict(change="Fixture intervention", preserve="Keep economy behavior",
                verification="Inspect worker growth and check that supply and technology remain healthy.",
                code_targets=["bot/components/tactics/bc_rush.py"])


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
