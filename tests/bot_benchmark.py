"""Manual-only five-game benchmark; never collected by pytest or Smoke Test."""
import argparse
import json
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from rsi.config import load_config
from rsi.evaluation.runner import Evaluator, check_sc2
from rsi.evolution.state import redact, save_json
from rsi.tools.git import Git


def snapshot_directory(source, worktree):
    source = Path(source).resolve()
    if not (source / "main.py").is_file():
        raise ValueError("--bot-dir must contain main.py exporting SeedBot")
    for directory, dirs, files in os.walk(source, followlinks=False):
        for name in dirs + files:
            item = Path(directory) / name
            if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
                raise ValueError(f"Bot snapshot cannot contain links: {item}")
    shutil.copytree(source, worktree / "bot",
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.pyo"))
    return {"bot_dir": str(source), "commit": None}


def snapshot_ref(ref, worktree):
    git = Git(ROOT)
    commit = git.run("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    archive_path = worktree.parent / "bot-source.tar"
    git.run("archive", "--format=tar", f"--output={archive_path}", commit, "--", "bot")
    with tarfile.open(archive_path) as archive:
        for member in archive:
            relative = PurePosixPath(member.name)
            if (relative.is_absolute() or not relative.parts or relative.parts[0] != "bot"
                    or ".." in relative.parts or "\\" in member.name or ":" in member.name
                    or not (member.isdir() or member.isfile())):
                raise ValueError(f"Unsupported Bot archive entry: {member.name}")
            target = worktree.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
    if not (worktree / "bot/main.py").is_file():
        raise ValueError("Selected commit has no bot/main.py")
    return {"ref": ref, "commit": commit}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manually benchmark a Bot for the same 5 games as Evaluation")
    parser.add_argument("--config", type=Path, default=ROOT / "config/mvp.yaml")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--bot-dir", type=Path, help="Directory containing main.py (default: current bot/)")
    selection.add_argument("--ref", help="Commit or branch in this repository; read-only export of bot/")
    args = parser.parse_args(argv)
    try:
        load_dotenv(ROOT / ".env", override=False, encoding="utf-8-sig")
        config = load_config(args.config)
        environment = check_sc2(config)
        output = Path(tempfile.mkdtemp(prefix="sc2-benchmark-"))
        print(f"Benchmark output (retained): {output}", flush=True)
        worktree = output / "snapshot"
        worktree.mkdir()
        origin = (snapshot_ref(args.ref, worktree) if args.ref is not None
                  else snapshot_directory(args.bot_dir or ROOT / "bot", worktree))
        # A regular package must win over the original project's bot/ on sys.path.
        package = worktree / "bot/__init__.py"
        if not package.exists():
            package.write_text("", encoding="utf-8")
        config_path = output / "config.json"
        save_json(config_path, config)
        save_json(output / "source.json", {**origin, **environment})
        node = {"id": output.name, "parent_id": None, "commit": origin["commit"]}
        temporary = output / "tmp"
        temporary.mkdir()
        previous = {key: os.environ.get(key) for key in ("TMP", "TEMP", "TMPDIR")}
        try:
            for key in previous:
                os.environ[key] = str(temporary)
            metadata = Evaluator(config, config_path).evaluate(worktree, node, output)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        print(json.dumps({key: metadata[key] for key in
                          ("candidate_id", "commit", "games", "wins", "losses", "ties", "crashes")},
                         ensure_ascii=False, indent=2))
        print(f"Metadata: {output / 'metadata.json'}", flush=True)
        return 1 if metadata["crashes"] else 0
    except Exception as exc:
        print(redact(f"Benchmark error: {exc}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
