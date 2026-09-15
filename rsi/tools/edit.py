from pathlib import Path, PureWindowsPath


class Editor:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def path(self, name, write=False):
        if not isinstance(name, str) or not name or "\\" in name or ":" in name:
            raise ValueError("Use a project-relative POSIX path")
        relative = Path(name)
        if relative.is_absolute() or PureWindowsPath(name).drive or any(part in ("..", ".git") for part in relative.parts):
            raise ValueError("Path escapes project")
        allowed = {"bot"} if write else {"bot", "rsi", "tests", "config", "prompts", "README.md", "architecture.md"}
        if not relative.parts or relative.parts[0] not in allowed:
            raise ValueError("Path is outside allowed area")
        target = self.root / relative
        for item in (target, *target.parents):
            if item == self.root:
                break
            if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
                raise ValueError("Symlinks/junctions are not allowed")
        if not target.resolve().is_relative_to(self.root):
            raise ValueError("Path escapes project")
        return target

    def view_file(self, path):
        return self.path(path).read_text(encoding="utf-8")

    def search_text(self, text, path="bot"):
        target = self.path(path)
        files = sorted(target.rglob("*")) if target.is_dir() else [target]
        matches = []
        for file in files:
            if not file.is_file() or "__pycache__" in file.parts:
                continue
            self.path(file.relative_to(self.root).as_posix())
            try:
                contents = file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(contents.splitlines(), 1):
                if text in line:
                    matches.append({"path": file.relative_to(self.root).as_posix(), "line": number, "text": line})
        return matches

    def write_file(self, path, content):
        target = self.path(path, write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"written": path}

    def replace_text(self, path, old, new):
        target = self.path(path, write=True)
        contents = target.read_text(encoding="utf-8")
        if not old or contents.count(old) != 1:
            raise ValueError("old must match exactly once")
        target.write_text(contents.replace(old, new, 1), encoding="utf-8")
        return {"replaced": path}
