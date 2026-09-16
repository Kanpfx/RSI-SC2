import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from hashlib import sha256

from rsi.agent.context import parent_context
from rsi.agent.runner import Agent
from dotenv import load_dotenv

from rsi.config import load_config
from rsi.evaluation.runner import Evaluator, preflight
from rsi.evolution.archive import Archive
from rsi.evolution.state import redact, save_json
from rsi.llm.client import Client
from rsi.tools.bash import smoke_test
from rsi.tools.git import Git


class Evolution:
    def __init__(self, root, config, llm, evaluator=None, smoke=smoke_test):
        self.root, self.config = Path(root).resolve(), copy.deepcopy(config)
        self.git, self.llm = Git(self.root), llm
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.output = self.root / "runs" / self.run_id
        self.output.mkdir(parents=True, exist_ok=False)
        self.worktrees = self.output / "worktrees"
        self.worktrees.mkdir()
        self.record = {"run_id": self.run_id, "config": self.config, "nodes": [], "environment": None}
        self.evaluator = evaluator or Evaluator(self.config, on_result=self.game_result)
        self.archive = Archive()
        self.failures = []
        self.counter = 0
        self.agent = Agent(llm, self.config["agent"]["max_steps"], self.config["tools"]["bash_timeout_sec"], smoke)
        self.improve_prompt = (self.root / "prompts/improve.md").read_text(encoding="utf-8")

    def usage(self):
        statistics = getattr(self.llm, "statistics", None)
        return statistics() if callable(statistics) else None

    def save(self):
        self.record["usage"] = self.usage()
        for node in self.record["nodes"]:
            save_json(self.output / "nodes" / node["id"] / "node.json", node)
        tree = [{key: node[key] for key in ("id", "parent_id", "generation", "status", "expanded")}
                for node in self.record["nodes"]]
        save_json(self.output / "tree.json", {"nodes": tree})
        save_json(self.output / "run.json", {key: value for key, value in self.record.items() if key != "nodes"})

    def state(self, status, round_number, error=None):
        frontier = self.archive.parents(self.config["search"]["beam_width"])
        self.record.update(status=status, round=round_number,
                           frontier=[node["id"] for node in frontier], error=error)
        self.save()

    def game_result(self, node, record):
        node.setdefault("evaluation", {"results": []})["results"].append(record)
        node["evaluation"]["results"].sort(key=lambda item: item["number"])
        self.save()

    def node(self, parent=None, direction="Seed"):
        number = self.counter
        self.counter += 1
        node_id = f"a{number}"
        node = {"id": node_id, "parent_id": parent["id"] if parent else None,
                "commit": None, "branch": f"candidate/{self.run_id}/{node_id}",
                "generation": parent["generation"] + 1 if parent else 0,
                "created_order": number, "direction": direction, "expanded": False,
                "status": "created"}
        self.record["nodes"].append(node)
        self.save()
        return node

    def evaluate(self, worktree, node, artifact):
        node["status"] = "evaluating"
        self.save()
        metadata = self.evaluator.evaluate(worktree, node, artifact)
        node.update({key: metadata[key] for key in ("games", "wins", "losses", "ties", "crashes")})
        node["evaluation"] = {"results": metadata["results"]}
        node["status"] = "evaluated"
        self.archive.add(node)
        self.save()

    def failure(self, node, stage, exc):
        item = {"candidate_id": node["id"], "parent_id": node["parent_id"],
                "commit": node["commit"], "direction": node["direction"], "stage": stage,
                "error": redact(str(exc))}
        self.failures.append(item)
        node.update(status="failed", failure={"stage": stage, "error": item["error"]})
        self.save()
        print(f"  Invalid {node['id']} ({stage}): {item['error']}", flush=True)

    def candidate(self, parent, attempt):
        node = self.node(parent, f"Attempt {attempt}")
        worktree, artifact = self.worktrees / node["id"], self.output / "nodes" / node["id"]
        stage = "worktree"
        try:
            self.git.create_worktree(worktree, node["branch"], parent["commit"])
            git = Git(worktree)
            stage = "agent"
            node["status"] = "improving"
            self.save()
            context = parent_context(worktree, parent, self.archive, self.failures)
            context["attempt"] = attempt
            result = self.agent.run(worktree, self.improve_prompt, context, artifact / "agent.json",
                                    feedback_dir=self.output / "nodes" / parent["id"])
            node["agent"] = copy.deepcopy(result)
            if result.get("smoke"):
                for check in node["agent"]["smoke"].get("checks", []):
                    if check.get("exit_code") == 0 and not check.get("timed_out"):
                        check.pop("stdout", None)
                        check.pop("stderr", None)
            self.save()
            if not result["ok"]:
                raise ValueError(result["error"])
            node["direction"] = result["summary"]
            if not git.check_bot_only(parent["commit"]):
                raise ValueError("Candidate has no changes")
            stage = "commit"
            git.add()
            node["commit"] = git.commit(f"RSI {node['id']}: {node['direction'][:100]}")
            patch = artifact / "changes.patch"
            git.run("diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
                    f"--output={patch}", parent["commit"], node["commit"], "--", "bot")
            node["patch_sha256"] = sha256(patch.read_bytes()).hexdigest()
            node["bot_tree"] = git.run("rev-parse", f"{node['commit']}:bot")
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
                seed["bot_tree"] = self.git.run("rev-parse", f"{seed['commit']}:bot")
                print(f"Run {self.run_id}: evaluating Seed", flush=True)
                self.evaluate(worktree, seed, self.output / "nodes" / seed["id"])
            finally:
                self.git.remove_worktree(worktree, self.worktrees)
            self.state("running", 0)
            for round_number in range(1, self.config["project"]["max_generations"] + 1):
                parents = self.archive.parents(self.config["search"]["beam_width"])
                if not parents:
                    break
                print(f"Round {round_number}: {len(parents)} parent(s)", flush=True)
                for parent in parents:
                    for attempt in range(1, self.config["search"]["branch_factor"] + 1):
                        self.candidate(parent, attempt)
                        self.state("running", round_number)
                    self.archive.mark_expanded(parent)
                self.state("running", round_number)
            ranked = self.archive.ranked()
            summary = {"run_id": self.run_id, "output": str(self.output),
                       "evaluated_nodes": len(self.archive.nodes), "failed_attempts": len(self.failures),
                       "best": ranked[0] if ranked else None, "usage": self.usage()}
            self.record["summary"] = {"evaluated_nodes": len(self.archive.nodes),
                                      "failed_attempts": len(self.failures),
                                      "best": ranked[0]["id"] if ranked else None}
            self.state("completed", round_number)
            return summary
        except BaseException as exc:
            self.state("failed", round_number, redact(f"{type(exc).__name__}: {exc}"))
            raise
        finally:
            if self.worktrees.exists() and not any(self.worktrees.iterdir()):
                self.worktrees.rmdir()


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
        evolution.record["environment"] = report
        print(json.dumps(evolution.run(), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(redact(f"Error: {exc}"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
