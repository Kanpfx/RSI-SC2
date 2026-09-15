from pathlib import Path

from rsi.process import run_process


class Git:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def run(self, *args):
        result = run_process(["git", "-c", "core.quotepath=false", *args], self.root, 60)
        if result["exit_code"] != 0 or result["timed_out"]:
            raise RuntimeError(f"git {args[0]}: {result['stderr']}")
        return result["stdout"].rstrip("\r\n")

    def status(self):
        return self.run("status", "--porcelain", "--untracked-files=all")

    def diff(self):
        return self.run("diff", "HEAD", "--", "bot")

    def current_commit(self):
        return self.run("rev-parse", "HEAD")

    def create_branch(self, name, parent):
        self.run("branch", name, parent)

    def checkout(self, name):
        self.run("checkout", name)

    def add(self):
        self.run("add", "-A", "--", "bot")

    def commit(self, message):
        self.run("commit", "-m", message)
        return self.current_commit()

    def merge_ff(self, branch):
        self.run("merge", "--ff-only", branch)

    def create_worktree(self, path, branch, parent):
        self.run("worktree", "add", "-b", branch, str(path), parent)

    def remove_worktree(self, path, worktree_root):
        path, allowed = Path(path).resolve(), Path(worktree_root).resolve()
        if path.parent != allowed or not allowed.is_relative_to(self.root / "runs"):
            raise ValueError("Refusing to remove a worktree outside this run")
        self.run("worktree", "remove", "--force", str(path))

    def changed_files(self, include_ignored=True):
        files = set(self.run("diff", "HEAD", "--name-only", "--no-renames", "-z").split("\0"))
        files.update(self.run("ls-files", "--others", "--exclude-standard", "-z").split("\0"))
        if include_ignored:
            files.update(self.run("ls-files", "--others", "--ignored", "--exclude-standard", "-z").split("\0"))
        return sorted(name for name in files if name and not self._cache(name))

    @staticmethod
    def _cache(name):
        parts = Path(name).parts
        return ".pytest_cache" in parts or ("__pycache__" in parts and name.endswith(".pyc"))

    def check_bot_only(self, parent):
        if self.current_commit() != parent:
            raise ValueError("Agent changed the candidate HEAD")
        files = self.changed_files()
        outside = [name for name in files if not name.startswith("bot/")]
        if outside:
            raise ValueError(f"Changes outside bot/: {outside}")
        bot_dir = self.root / "bot"
        for path in [bot_dir, *bot_dir.rglob("*")]:
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ValueError(f"Symlink/junction forbidden: {path}")
        return files
