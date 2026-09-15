import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from rsi.agent.context import parent_context
from rsi.agent.runner import Agent
from rsi.analysis.analyzer import analyze
from dotenv import load_dotenv

from rsi.config import load_config
from rsi.evaluation.runner import Evaluator, preflight
from rsi.evaluation.selector import eligible
from rsi.evolution.archive import Archive
from rsi.evolution.search import select_parents
from rsi.evolution.state import read_json, redact, save_json
from rsi.llm.client import Client
from rsi.tools.bash import smoke_test
from rsi.tools.git import Git


class Evolution:
    def __init__(self, root, config, llm, evaluator=None, smoke=smoke_test):
        self.root, self.config = Path(root).resolve(), copy.deepcopy(config)
        self.git, self.llm, self.smoke = Git(self.root), llm, smoke
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
        self.output = self.root / "runs" / self.run_id
        self.output.mkdir(parents=True)
        self.worktrees = self.output / "worktrees"
        self.worktrees.mkdir()
        save_json(self.output / "config.json", self.config)
        self.evaluator = evaluator or Evaluator(self.config, self.output / "config.json")
        self.archive = Archive(self.output / "archive.json")
        self.failures = []
        self.counter = 0
        self.agent = Agent(llm, self.config["agent"]["max_steps"], self.config["tools"]["bash_timeout_sec"])
        self.analyze_prompt = (self.root / "prompts/analyze.md").read_text(encoding="utf-8")
        self.improve_prompt = (self.root / "prompts/improve.md").read_text(encoding="utf-8")

    def state(self, status, round_number, error=None):
        frontier = select_parents(self.archive.nodes, self.config["search"]["beam_width"])
        save_json(self.output / "state.json", {"run_id": self.run_id, "status": status,
                  "round": round_number, "frontier": [node["id"] for node in frontier], "error": error})

    def node(self, parent=None, direction="Seed"):
        number = self.counter
        self.counter += 1
        node_id = f"{self.run_id}_n{number:04d}"
        return {"id": node_id, "parent_id": parent["id"] if parent else None,
                "commit": None, "branch": f"candidate/{node_id}",
                "generation": parent["generation"] + 1 if parent else 0,
                "created_order": number, "direction": direction, "expanded": False}

    def evaluate(self, worktree, node, artifact):
        save_json(artifact / "node.json", node)
        metadata = self.evaluator.evaluate(worktree, node, artifact)
        node.update({key: metadata[key] for key in ("games", "wins", "losses", "ties", "crashes")})
        save_json(artifact / "metadata.json", metadata)
        save_json(artifact / "node.json", node)
        self.archive.add(node)

    def failure(self, node, stage, exc):
        item = {"candidate_id": node["id"], "parent_id": node["parent_id"],
                "commit": node["commit"], "direction": node["direction"], "stage": stage,
                "error": redact(str(exc))}
        self.failures.append(item)
        save_json(self.output / "failures.json", self.failures)
        print(f"  Invalid {node['id']} ({stage}): {item['error']}", flush=True)

    def context(self, parent):
        worktree = self.worktrees / f"context_{parent['id']}"
        self.git.run("worktree", "add", "--detach", str(worktree), parent["commit"])
        try:
            context = parent_context(worktree, parent, self.archive)
            context["evaluation"] = read_json(self.output / "nodes" / parent["id"] / "metadata.json")
            return context
        finally:
            self.git.remove_worktree(worktree, self.worktrees)

    def candidate(self, parent, direction, context):
        node = self.node(parent, direction)
        worktree, artifact = self.worktrees / node["id"], self.output / "nodes" / node["id"]
        stage = "worktree"
        try:
            self.git.create_worktree(worktree, node["branch"], parent["commit"])
            git = Git(worktree)
            stage = "agent"
            result = self.agent.run(worktree, self.improve_prompt,
                                    {**context, "assigned_direction": direction}, artifact / "agent.json")
            save_json(artifact / "agent_result.json", result)
            if not result["ok"]:
                raise ValueError(result["error"])
            stage = "smoke"
            checks = self.smoke(worktree, parent["commit"], self.config["tools"]["bash_timeout_sec"])
            save_json(artifact / "smoke.json", checks)
            if not checks["ok"]:
                raise ValueError("Smoke test failed; see smoke.json")
            if not git.check_bot_only(parent["commit"]):
                raise ValueError("Candidate has no changes")
            stage = "commit"
            git.add()
            node["commit"] = git.commit(f"RSI {node['id']}: {direction[:100]}")
            stage = "evaluation"
            self.evaluate(worktree, node, artifact)
        except Exception as exc:
            self.failure(node, stage, exc)
        finally:
            if worktree.exists():
                self.git.remove_worktree(worktree, self.worktrees)

    def run(self):
        round_number = 0
        self.state("running", round_number)
        try:
            seed = self.node()
            seed["commit"] = self.git.current_commit()
            worktree = self.worktrees / seed["id"]
            self.git.create_worktree(worktree, seed["branch"], seed["commit"])
            try:
                print(f"Run {self.run_id}: evaluating Seed", flush=True)
                self.evaluate(worktree, seed, self.output / "nodes" / seed["id"])
            finally:
                self.git.remove_worktree(worktree, self.worktrees)
            self.state("running", 0)
            for round_number in range(1, self.config["project"]["max_generations"] + 1):
                parents = select_parents(self.archive.nodes, self.config["search"]["beam_width"])
                if not parents:
                    break
                print(f"Round {round_number}: {len(parents)} parent(s)", flush=True)
                for parent in parents:
                    context = self.context(parent)
                    try:
                        analysis = analyze(self.llm, self.analyze_prompt, context,
                                           self.config["search"]["branch_factor"],
                                           self.output / "nodes" / parent["id"] / "analysis.json")
                    except Exception as exc:
                        self.failure(parent, "analysis", exc)
                        raise
                    for direction in analysis["candidates"]:
                        self.candidate(parent, direction, {**context, "problem": analysis["problem"]})
                        self.state("running", round_number)
                    self.archive.mark_expanded(parent)
                self.state("running", round_number)
            ranked = eligible(self.archive.nodes)
            summary = {"run_id": self.run_id, "output": str(self.output),
                       "evaluated_nodes": len(self.archive.nodes), "failed_attempts": len(self.failures),
                       "best": ranked[0] if ranked else None}
            save_json(self.output / "summary.json", summary)
            self.state("completed", round_number)
            return summary
        except BaseException as exc:
            self.state("failed", round_number, redact(f"{type(exc).__name__}: {exc}"))
            raise


def main():
    parser = argparse.ArgumentParser(description="SC2-RSI minimal evolution loop")
    parser.add_argument("--config", default="config/mvp.yaml")
    parser.add_argument("--preflight", action="store_true", help="Check setup without LLM calls or games")
    args = parser.parse_args()
    try:
        root = Path(__file__).resolve().parents[1]
        load_dotenv(root / ".env", override=False, encoding="utf-8-sig")
        config = load_config(args.config)
        report = preflight(root, config)
        if args.preflight:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        llm = Client()
        evolution = Evolution(root, config, llm)
        save_json(evolution.output / "environment.json", report)
        print(json.dumps(evolution.run(), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(redact(f"Error: {exc}"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
