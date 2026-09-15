import copy
import json
import shutil
import sys
import threading
from pathlib import Path

import psutil
import pytest

from rsi.agent.runner import Agent
from rsi.analysis.analyzer import analyze
from rsi.config import load_config
from rsi.evaluation.metadata import summarize
from rsi.evaluation.runner import Evaluator
from rsi.evolution.archive import Archive
from rsi.evolution.search import select_parents
from rsi.evolution.state import read_json, save_json
from rsi.loop import Evolution
from rsi.process import child_env, run_process
from rsi.tools.bash import Commands
from rsi.tools.edit import Editor
from rsi.tools.git import Git

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for name in ("bot", "rsi", "prompts", "config"):
        shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
    (root / "tests").mkdir()
    shutil.copy2(ROOT / "tests/test_bot_smoke.py", root / "tests/test_bot_smoke.py")
    shutil.copy2(ROOT / ".gitignore", root / ".gitignore")
    git = Git(root)
    git.run("init", "-b", "main")
    git.run("config", "user.name", "RSI Test")
    git.run("config", "user.email", "rsi-test@example.invalid")
    git.run("add", ".")
    git.commit("Seed fixture")
    return root


def test_editor_boundaries_and_replacement(repo):
    editor = Editor(repo)
    for path in ("../escape.py", "/tmp/escape.py", "C:/escape.py", "bot/../../escape.py",
                 "bot\\main.py", "rsi/loop.py", "bot/.git/config"):
        with pytest.raises(ValueError):
            editor.write_file(path, "bad")
    editor.write_file("bot/new.py", "hello hello")
    with pytest.raises(ValueError, match="exactly once"):
        editor.replace_text("bot/new.py", "hello", "bye")
    editor.replace_text("bot/new.py", "hello hello", "world")
    assert editor.search_text("world", "bot/new.py")[0]["line"] == 1
    with pytest.raises(ValueError):
        editor.view_file(".env")


def test_editor_rejects_symlink(repo, tmp_path):
    target = tmp_path / "outside.py"
    target.write_text("untouched", encoding="utf-8")
    link = repo / "bot/link.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("OS does not permit creating symlinks")
    with pytest.raises(ValueError, match="Symlink"):
        Editor(repo).write_file("bot/link.py", "bad")
    assert target.read_text(encoding="utf-8") == "untouched"


def test_commands_reject_arbitrary_execution(repo):
    commands = Commands(repo)
    for argv in (["python", "-c", "print('bad')"], ["pytest"], ["cmd", "/c", "dir"],
                 ["rg", "-n", "--", "key", "../outside"], "python -m compileall bot"):
        with pytest.raises(ValueError):
            commands.run_command(argv)


def test_timeout_kills_child_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-private-key")
    assert "LLM_API_KEY" not in child_env()
    code = ("import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            "print(p.pid,flush=True); time.sleep(30)")
    result = run_process([sys.executable, "-c", code], tmp_path, timeout=2)
    assert result["timed_out"] and result["exit_code"] != 0
    assert not psutil.pid_exists(int(result["stdout"].strip()))


def call(name, arguments, identifier="call_1"):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": identifier, "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments)}}]}


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def complete(self, messages, tools=None):
        self.requests.append(copy.deepcopy(messages))
        return next(self.replies)


def test_agent_roundtrip_and_no_changes(repo, tmp_path):
    done = {"role": "assistant", "content": "Done"}
    llm = ScriptedLLM([
        call("view_file", {"path": "bot/main.py"}),
        call("replace_text", {"path": "bot/modules/strategy/strategy_config.py",
                              "old": "attack_threshold = 12", "new": "attack_threshold = 10"}, "call_2"), done])
    result = Agent(llm).run(repo, "test", {}, tmp_path / "agent.json")
    assert result["ok"]
    assert llm.requests[1][-1]["role"] == "tool"
    assert llm.requests[1][-1]["tool_call_id"] == "call_1"
    assert "SeedBot" in llm.requests[1][-1]["content"]
    assert llm.requests[2][-1]["tool_call_id"] == "call_2"
    Git(repo).add()
    Git(repo).commit("Accept test edit")
    result = Agent(ScriptedLLM([done])).run(repo, "test", {}, tmp_path / "empty.json")
    assert not result["ok"] and "no changes" in result["error"]


def test_agent_limit_and_bad_tool(repo, tmp_path):
    read = call("view_file", {"path": "bot/main.py"})
    result = Agent(ScriptedLLM([read]), max_steps=1).run(repo, "test", {}, tmp_path / "limit.json")
    assert not result["ok"] and "max_steps" in result["error"]
    broken = call("write_file", {"path": "rsi/loop.py", "content": "bad"})
    result = Agent(ScriptedLLM([broken, {"role": "assistant", "content": "done"}])).run(
        repo, "test", {}, tmp_path / "bad.json")
    assert not result["ok"]
    assert "error" in read_json(tmp_path / "bad.json")[3]["content"]


def test_analyzer_retry_and_failure(tmp_path):
    invalid = {"role": "assistant", "content": "not json"}
    valid = {"role": "assistant", "content": json.dumps({"problem": "slow", "candidates": ["a", "b"]})}
    llm = ScriptedLLM([invalid, valid])
    assert analyze(llm, "test", {}, 2, tmp_path / "analysis.json")["candidates"] == ["a", "b"]
    assert len(llm.requests) == 2
    with pytest.raises(ValueError, match="twice"):
        analyze(ScriptedLLM([invalid, invalid]), "test", {}, 2, tmp_path / "bad.json")


def make_node(identifier, wins, depth=1, order=0, crash=0, expanded=False):
    return {"id": identifier, "wins": wins, "losses": 5 - wins - crash, "ties": 0,
            "crashes": crash, "games": 5, "generation": depth, "created_order": order, "expanded": expanded}


def test_archive_and_search(tmp_path):
    archive = Archive(tmp_path / "archive.json")
    for node in [make_node("crashed", 4, crash=1), make_node("expanded", 4, expanded=True),
                 make_node("deep", 3, depth=2), make_node("late", 3, order=2),
                 make_node("early", 3, order=1), make_node("history", 3, depth=0)]:
        archive.add(node)
    assert [node["id"] for node in select_parents(archive.nodes, 2)] == ["history", "early"]
    assert Archive(archive.path).nodes == archive.nodes
    with pytest.raises(ValueError, match="five"):
        archive.add({**make_node("invalid", 1), "games": 4})


def test_evaluation_five_records(tmp_path, monkeypatch):
    config = load_config(ROOT / "config/mvp.yaml")
    (tmp_path / "bot").mkdir()
    (tmp_path / "bot/main.py").write_text("# snapshot", encoding="utf-8")
    barrier = threading.Barrier(5)
    launches, intervals = {}, []
    monkeypatch.setattr("rsi.evaluation.runner.time.sleep", intervals.append)

    def process(argv, cwd, timeout, env, cancel_event):
        number = int(Path(argv[-1]).stem.split("_")[1])
        launches[number] = (cwd, env["TEMP"])
        assert env["TEMP"] == env["TMP"] == env["TMPDIR"]
        assert cwd != tmp_path and (cwd / "bot/main.py").read_text() == "# snapshot"
        (cwd / "bot/runtime.txt").write_text(str(number))
        barrier.wait(timeout=10)  # All five processes must overlap.
        if number == 5:
            return {"stdout": "", "stderr": "", "exit_code": -1, "timed_out": True}
        result = "win" if number < 3 else "loss" if number < 4 else "tie"
        save_json(Path(argv[-1]), {"result": result, "crashed": False, "error": None})
        return {"stdout": "", "stderr": "", "exit_code": 0, "timed_out": False}

    monkeypatch.setattr("rsi.evaluation.runner.run_process", process)
    result = Evaluator(config, tmp_path / "config.json").evaluate(
        tmp_path, {"id": "node", "parent_id": None, "commit": "abc"}, tmp_path / "results")
    assert (result["wins"], result["losses"], result["ties"], result["crashes"]) == (2, 1, 1, 1)
    assert result["games"] == 5 and intervals == [3, 3, 3, 3]
    assert [item["result"] for item in result["results"]] == ["win", "win", "loss", "tie", "crash"]
    assert len({cwd for cwd, _ in launches.values()}) == 5
    assert len({temp for _, temp in launches.values()}) == 5
    assert not (tmp_path / "bot/runtime.txt").exists()
    assert "exceeded" in result["results"][-1]["error"]


def test_git_parent_isolation_and_boundary(repo):
    git = Git(repo)
    seed = git.current_commit()
    worktrees = repo / "runs/test/worktrees"
    worktrees.mkdir(parents=True)
    first, second = worktrees / "first", worktrees / "second"
    git.create_worktree(first, "candidate/first", seed)
    git.create_worktree(second, "candidate/second", seed)
    try:
        Editor(first).write_file("bot/only_first.py", "x = 1\n")
        candidate = Git(first)
        assert candidate.check_bot_only(seed) == ["bot/only_first.py"]
        candidate.add()
        commit = candidate.commit("First candidate")
        assert candidate.run("rev-parse", f"{commit}^") == seed
        assert not (second / "bot/only_first.py").exists()
        (second / ".env").write_text("ignored but forbidden", encoding="utf-8")
        with pytest.raises(ValueError, match="outside bot"):
            Git(second).check_bot_only(seed)
        (second / ".env").unlink()
        (second / "rsi/loop.py").write_text("bad", encoding="utf-8")
        with pytest.raises(ValueError, match="outside bot"):
            Git(second).check_bot_only(seed)
    finally:
        git.remove_worktree(first, worktrees)
        git.remove_worktree(second, worktrees)
    assert git.current_commit() == seed and not git.status()


class EvolutionLLM:
    def complete(self, messages, tools=None):
        if tools is None:
            return {"role": "assistant", "content": json.dumps({
                "problem": "Slow pressure", "candidates": ["Earlier attack", "Earlier supply"]})}
        if messages[-1]["role"] == "tool":
            return {"role": "assistant", "content": "Updated one strategy parameter"}
        context = json.loads(messages[1]["content"])
        old, new = (("attack_threshold = 12", "attack_threshold = 10")
                    if context["assigned_direction"] == "Earlier attack"
                    else ("supply_buffer = 4", "supply_buffer = 6"))
        return call("replace_text", {"path": "bot/modules/strategy/strategy_config.py", "old": old, "new": new})


class FakeEvaluator:
    def evaluate(self, worktree, node, output):
        wins = {"Seed": 1, "Earlier attack": 3, "Earlier supply": 2}[node["direction"]]
        records = [{"result": "win" if index < wins else "loss", "crashed": False,
                    "duration": 0.0, "error": None} for index in range(5)]
        return {"candidate_id": node["id"], "parent_id": node["parent_id"],
                "commit": node["commit"], **summarize(records)}


def test_one_round_complete_loop(repo):
    config = load_config(repo / "config/mvp.yaml")
    config["project"]["max_generations"] = 1
    evolution = Evolution(repo, config, EvolutionLLM(), evaluator=FakeEvaluator())
    summary = evolution.run()
    seed, first, second = evolution.archive.nodes
    assert summary["evaluated_nodes"] == 3 and summary["failed_attempts"] == 0
    assert seed["expanded"] and not first["expanded"] and not second["expanded"]
    assert first["parent_id"] == second["parent_id"] == seed["id"]
    git = Git(repo)
    for candidate in (first, second):
        assert git.run("rev-parse", f"{candidate['commit']}^") == seed["commit"]
        changed = git.run("diff", "--name-only", seed["commit"], candidate["commit"]).splitlines()
        assert changed == ["bot/modules/strategy/strategy_config.py"]
    state = read_json(evolution.output / "state.json")
    assert state["status"] == "completed" and state["frontier"] == [first["id"], second["id"]]
    assert summary["best"]["id"] == first["id"]
    assert not list(evolution.worktrees.iterdir())
    assert git.current_commit() == seed["commit"] and not git.status()


def test_sc2_logged_error_is_not_a_normal_loss(monkeypatch):
    from loguru import logger
    from sc2.data import Result
    from rsi.evaluation.game import play

    def failed_game(*args, **kwargs):
        logger.error("Bot on_start failed")
        return Result.Defeat

    monkeypatch.setattr("sc2.maps.get", lambda name: object())
    monkeypatch.setattr("sc2.main.run_game", failed_game)
    with pytest.raises(RuntimeError, match="on_start failed"):
        play(ROOT, load_config(ROOT / "config/mvp.yaml"))


def test_smoke_failure_is_not_evaluated(repo):
    config = load_config(repo / "config/mvp.yaml")
    config["project"]["max_generations"] = 1
    evolution = Evolution(repo, config, EvolutionLLM(), evaluator=FakeEvaluator(),
                          smoke=lambda *args: {"ok": False, "checks": []})
    summary = evolution.run()
    assert summary["evaluated_nodes"] == 1 and summary["failed_attempts"] == 2
    assert len(evolution.archive.nodes) == 1 and evolution.archive.nodes[0]["expanded"]
    assert all(item["stage"] == "smoke" and item["commit"] is None for item in evolution.failures)
    assert read_json(evolution.output / "state.json")["frontier"] == []


def test_agent_sc2_lookup_roundtrip(repo, tmp_path):
    llm = ScriptedLLM([
        call("lookup_sc2_api", {"symbol": "BotAI.build"}),
        call("replace_text", {"path": "bot/modules/strategy/strategy_config.py",
                              "old": "attack_threshold = 12", "new": "attack_threshold = 10"}, "call_2"),
        {"role": "assistant", "content": "Done"},
    ])
    result = Agent(llm).run(repo, "test", {}, tmp_path / "api-agent.json")
    assert result["ok"]
    reply = llm.requests[1][-1]
    assert reply["role"] == "tool" and reply["tool_call_id"] == "call_1"
    api = json.loads(reply["content"])
    assert api["status"] == "found" and api["async"]
    assert api["symbol"] == "sc2.bot_ai.BotAI.build"


def test_cancel_kills_game_process_tree(tmp_path):
    cancel = threading.Event()
    timer = threading.Timer(2, cancel.set)
    code = ("import subprocess,sys,time,pathlib; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            "pathlib.Path('child.pid').write_text(str(p.pid)); time.sleep(30)")
    timer.start()
    try:
        with pytest.raises(InterruptedError, match="cancelled"):
            run_process([sys.executable, "-c", code], tmp_path, timeout=30, cancel_event=cancel)
    finally:
        timer.cancel()
    assert not psutil.pid_exists(int((tmp_path / "child.pid").read_text()))


def test_parallel_setup_failure_cancels_other_games(tmp_path, monkeypatch):
    evaluator = Evaluator(load_config(ROOT / "config/mvp.yaml"), tmp_path / "config.json")
    barrier = threading.Barrier(5)
    cancelled = []
    monkeypatch.setattr("rsi.evaluation.runner.time.sleep", lambda seconds: None)

    def game(number, worktree, node, output, cancel_event):
        barrier.wait(timeout=5)
        if number == 5:
            raise RuntimeError("setup failed")
        assert cancel_event.wait(timeout=5)
        cancelled.append(number)

    monkeypatch.setattr(evaluator, "_game", game)
    with pytest.raises(RuntimeError, match="setup failed"):
        evaluator.evaluate(tmp_path, {}, tmp_path / "results")
    assert sorted(cancelled) == [1, 2, 3, 4]
    assert not (tmp_path / "results/metadata.json").exists()
