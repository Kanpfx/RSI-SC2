import copy
import json
import shutil
import sys
import threading
from pathlib import Path

import psutil
import pytest

from rsi.agent.runner import Agent
from rsi.config import load_config
from rsi.evaluation.metadata import summarize
from rsi.evaluation.runner import Evaluator
from rsi.evolution.archive import Archive
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


def patch_text(path, old, new, line=1, destination=None):
    if old is None:
        body = f"*** Add File: {path}\n" + "".join("+" + value + "\n" for value in (new or "").splitlines())
    elif new is None:
        body = f"*** Delete File: {path}\n"
    else:
        body = f"*** Update File: {path}\n"
        if destination:
            body += f"*** Move to: {destination}\n"
        body += "@@\n"
        body += "".join("-" + value + "\n" for value in old.splitlines())
        body += "".join("+" + value + "\n" for value in new.splitlines())
    return "*** Begin Patch\n" + body + "*** End Patch\n"


def combine_patches(*patches):
    return "*** Begin Patch\n" + "".join(
        patch.removeprefix("*** Begin Patch\n").removesuffix("*** End Patch\n")
        for patch in patches) + "*** End Patch\n"


def test_editor_boundaries_and_replacement(repo):
    editor = Editor(repo)
    for path in ("../escape.py", "/tmp/escape.py", "C:/escape.py", "bot/../../escape.py",
                 "bot\\main.py", "rsi/loop.py", "bot/.git/config"):
        with pytest.raises(ValueError):
            editor.apply_patch(patch_text(path, None, "bad"))
    editor.apply_patch(patch_text("bot/new.py", None, "hello hello"))
    with pytest.raises(ValueError, match="does not match"):
        editor.apply_patch(patch_text("bot/new.py", "hello", "bye"))
    editor.apply_patch(patch_text("bot/new.py", "hello hello", "world"))
    assert editor.search("bot/new.py", "world")[0]["line"] == 1
    with pytest.raises(ValueError):
        editor.read_file(".env")


def test_editor_rejects_symlink(repo, tmp_path):
    target = tmp_path / "outside.py"
    target.write_text("untouched", encoding="utf-8")
    link = repo / "bot/link.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("OS does not permit creating symlinks")
    with pytest.raises(ValueError, match="Symlink"):
        Editor(repo).apply_patch(patch_text("bot/link.py", None, "bad"))
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
    done = call("finish", {"summary": "Earlier attack"})
    llm = ScriptedLLM([
        call("read_file", {"path": "bot/main.py"}),
        call("apply_patch", {"patch": patch_text("bot/modules/strategy/strategy.py",
                              "attack_threshold = 12", "attack_threshold = 10")}, "call_2"), done])
    result = Agent(llm).run(repo, "test", {}, tmp_path / "agent.json")
    assert result["ok"]
    assert llm.requests[1][-1]["role"] == "tool"
    assert llm.requests[1][-1]["tool_call_id"] == "call_1"
    assert "SeedBot" in llm.requests[1][-1]["content"]
    assert llm.requests[2][-1]["tool_call_id"] == "call_2"
    Git(repo).add()
    Git(repo).commit("Accept test edit")
    result = Agent(ScriptedLLM([done]), max_steps=1).run(repo, "test", {}, tmp_path / "empty.json")
    assert not result["ok"]
    assert "no changes" in read_json(tmp_path / "empty.json")[-1]["content"]


def test_agent_limit_and_bad_tool(repo, tmp_path):
    read = call("read_file", {"path": "bot/main.py"})
    result = Agent(ScriptedLLM([read]), max_steps=1).run(repo, "test", {}, tmp_path / "limit.json")
    assert not result["ok"] and "max_steps" in result["error"]
    broken = call("apply_patch", {"patch": patch_text("rsi/loop.py", None, "bad")})
    result = Agent(ScriptedLLM([broken, {"role": "assistant", "content": "done"}])).run(
        repo, "test", {}, tmp_path / "bad.json")
    assert not result["ok"]
    assert "error" in read_json(tmp_path / "bad.json")[3]["content"]


def make_node(identifier, wins, depth=1, order=0, crash=0, expanded=False):
    return {"id": identifier, "wins": wins, "losses": 5 - wins - crash, "ties": 0,
            "crashes": crash, "games": 5, "generation": depth, "created_order": order, "expanded": expanded}


def test_archive_and_search(tmp_path):
    archive = Archive(tmp_path / "archive.json")
    for node in [make_node("crashed", 4, crash=1), make_node("expanded", 4, expanded=True),
                 make_node("deep", 3, depth=2), make_node("late", 3, order=2),
                 make_node("early", 3, order=1), make_node("history", 3, depth=0)]:
        archive.add(node)
    assert [node["id"] for node in archive.parents(2)] == ["history", "early"]
    assert archive.ranked()[0]["id"] == "expanded"
    assert all(node["crashes"] == 0 for node in archive.ranked())
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
        Editor(first).apply_patch(patch_text("bot/only_first.py", None, "x = 1"))
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
        assert tools is not None
        context = json.loads(messages[1]["content"])
        assert "sources" not in context and "bot/main.py" in context["files"]
        assert "evaluation" not in context and context["parent"]["games"] == 5
        assert context["lineage"][-1]["id"] == context["parent"]["id"]
        assert "feedback/metadata.json" in context["feedback_files"]
        first = context["attempt"] == 1
        if messages[-1]["role"] == "tool":
            return call("finish", {"summary": "Earlier attack" if first else "Earlier supply"})
        if not first and not context["failed_attempts"]:
            assert context["siblings"][0]["direction"] == "Earlier attack"
        old, new = (("attack_threshold = 12", "attack_threshold = 10") if first
                    else ("supply_buffer = 4", "supply_buffer = 6"))
        return call("apply_patch", {"patch": patch_text("bot/modules/strategy/strategy.py", old, new, 1 if first else 2)})


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
        assert changed == ["bot/modules/strategy/strategy.py"]
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
    assert all(item["stage"] == "agent" and item["commit"] is None for item in evolution.failures)
    assert read_json(evolution.output / "state.json")["frontier"] == []


def test_agent_sc2_lookup_roundtrip(repo, tmp_path):
    llm = ScriptedLLM([
        call("tech_tree", {"entity": "BARRACKS"}),
        call("entity_info", {"entity": "MARAUDER"}),
        call("api_query", {"path": "BotAI.build"}),
        call("apply_patch", {"patch": patch_text("bot/modules/strategy/strategy.py",
                              "attack_threshold = 12", "attack_threshold = 10")}, "call_2"),
        call("finish", {"summary": "Earlier attack"}),
    ])
    result = Agent(llm).run(repo, "test", {}, tmp_path / "api-agent.json")
    assert result["ok"]
    assert json.loads(llm.requests[1][-1]["content"])["entity"] == "BARRACKS"
    assert json.loads(llm.requests[2][-1]["content"])["entity"] == "MARAUDER"
    reply = llm.requests[3][-1]
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


def test_editor_module_reorganization(repo):
    editor = Editor(repo)
    editor.apply_patch(patch_text("bot/new.py", None, "first\nsecond"))
    assert editor.read_file("bot/new.py")["text"] == "first\nsecond\n"
    editor.apply_patch(patch_text("bot/new.py", "first\nsecond", "first\nsecond", destination="bot/sub/module.py"))
    assert "bot/sub/module.py" in editor.search("bot/sub")
    assert not (repo / "bot/new.py").exists()
    for source, destination in (("bot/main.py", "rsi/new.py"), ("rsi/loop.py", "bot/new.py"),
                                ("bot/main.py", "bot/sub/module.py")):
        with pytest.raises(ValueError):
            editor.apply_patch(patch_text(source, "old", "new", destination=destination))
    with pytest.raises(ValueError):
        editor.apply_patch(patch_text("rsi/loop.py", "old", None))
    editor.apply_patch(patch_text("bot/sub/module.py", "first\nsecond", None))
    assert editor.search("bot/sub") == []


def test_agent_recovers_from_tool_and_smoke_errors(repo, tmp_path):
    llm = ScriptedLLM([
        call("read_file", {"path": "bot/missing.py"}),
        call("apply_patch", {"patch": patch_text("bot/new.py", None, "invalid python!")}),
        call("finish", {"summary": "New module"}),
        call("apply_patch", {"patch": patch_text("bot/new.py", "invalid python!", "value = 1")}),
        call("finish", {"summary": "New module"}),
    ])
    result = Agent(llm).run(repo, "test", {}, tmp_path / "recover.json")
    assert result["ok"] and result["smoke"]["ok"]
    assert "error" in json.loads(llm.requests[1][-1]["content"])
    assert not json.loads(llm.requests[3][-1]["content"])["ok"]


def test_agent_feedback_is_read_only_and_scoped(repo, tmp_path):
    feedback = tmp_path / "feedback"
    feedback.mkdir()
    save_json(feedback / "metadata.json", {"wins": 2})
    save_json(feedback / "agent.json", {"private": True})
    llm = ScriptedLLM([
        call("read_file", {"path": "feedback/../agent.json"}),
        call("read_file", {"path": "feedback/agent.json"}),
        call("read_file", {"path": "feedback/metadata.json"}),
    ])
    result = Agent(llm, max_steps=3).run(repo, "test", {}, tmp_path / "feedback-agent.json", feedback)
    assert not result["ok"]
    messages = read_json(tmp_path / "feedback-agent.json")
    replies = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
    assert "error" in replies[0] and "error" in replies[1]
    assert json.loads(replies[2]["text"])["wins"] == 2
    assert replies[2]["next_offset"] is None


def test_finish_must_be_alone(repo, tmp_path):
    message = call("finish", {"summary": "Done"})
    message["tool_calls"] += call("apply_patch", {"patch": patch_text("bot/new.py", None, "value = 1")})["tool_calls"]
    llm = ScriptedLLM([message, call("finish", {"summary": "New module"})])
    result = Agent(llm).run(repo, "test", {}, tmp_path / "finish.json")
    assert result["ok"]
    assert "Call finish alone" in llm.requests[1][-2]["content"]


def test_patch_validates_all_sections_before_writing(repo):
    editor = Editor(repo)
    valid = patch_text("bot/new.py", None, "value = 1")
    invalid = patch_text("bot/main.py", "missing context", "replacement")
    with pytest.raises(ValueError, match="does not match"):
        editor.apply_patch(combine_patches(valid, invalid))
    assert not (repo / "bot/new.py").exists()
    with pytest.raises(ValueError):
        editor.apply_patch(combine_patches(valid, patch_text("feedback/metadata.json", None, "{}")))
    assert not (repo / "bot/new.py").exists()
    with pytest.raises(ValueError, match="conflict"):
        editor.apply_patch(combine_patches(valid, patch_text("bot/new.py/child.py", None, "x = 1")))
    assert not (repo / "bot/new.py").exists()
    editor.apply_patch(combine_patches(valid, patch_text("bot/second.py", None, "value = 2")))
    assert (repo / "bot/new.py").read_text().strip() == "value = 1"
    assert (repo / "bot/second.py").read_text().strip() == "value = 2"


def test_patch_multiple_hunks_and_no_final_newline(repo):
    editor = Editor(repo)
    (repo / "bot/new.py").write_text("a\nb\nc\nd\ne", encoding="utf-8")
    patch = ("*** Begin Patch\n*** Update File: bot/new.py\n"
             "@@\n a\n-b\n+B\n+extra\n"
             "@@\n d\n-e\n+E\n*** End Patch\n")
    editor.apply_patch(patch)
    assert (repo / "bot/new.py").read_text(encoding="utf-8") == "a\nB\nextra\nc\nd\nE"


def test_file_reads_continue_and_search_feedback(repo, tmp_path):
    feedback = tmp_path / "feedback"
    feedback.mkdir()
    value = "x" * 13000
    (feedback / "game_01.process.json").write_text(value, encoding="utf-8")
    editor = Editor(repo, feedback)
    assert editor.search("feedback") == ["feedback/game_01.process.json"]
    first = editor.read_file("feedback/game_01.process.json")
    second = editor.read_file("feedback/game_01.process.json", first["next_offset"])
    assert first["text"] + second["text"] == value and second["next_offset"] is None
    assert editor.search("bot", "SeedBot")[0]["path"] == "bot/main.py"


def test_patch_ambiguous_context_and_pure_move(tmp_path):
    (tmp_path / "bot").mkdir()
    path = tmp_path / "bot/repeated.py"
    path.write_text("a\nx\nb\nx\n", encoding="utf-8")
    editor = Editor(tmp_path)
    with pytest.raises(ValueError, match="hunk 1: context matches 2 places"):
        editor.apply_patch(patch_text("bot/repeated.py", "x", "y"))
    assert path.read_text() == "a\nx\nb\nx\n"
    editor.apply_patch(patch_text("bot/repeated.py", "b\nx", "b\ny"))
    editor.apply_patch("*** Begin Patch\n*** Update File: bot/repeated.py\n"
                       "*** Move to: bot/moved.py\n*** End Patch")
    assert not path.exists()
    assert (tmp_path / "bot/moved.py").read_text() == "a\nx\nb\ny\n"
