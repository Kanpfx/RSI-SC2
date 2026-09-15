import re
from pathlib import Path, PureWindowsPath
from rsi.tools import INTEGER, STRING, schema


TOOLS = [
    schema("read_file", "Read source or read-only feedback/path in 12000-character chunks; continue at next_offset",
           {"path": STRING, "offset": INTEGER}, ["path"],
           [{"path": "bot/main.py"}, {"path": "feedback/metadata.json", "offset": 12000}]),
    schema("search", "List files under path; supply text for literal content search. Up to 200 results",
           {"path": STRING, "text": STRING}, [],
           [{"path": "bot"}, {"path": "bot", "text": "SeedBot"}, {"path": "feedback"}]),
    schema("apply_patch", "Apply text unified diffs with exact current content: --- a/bot/path, +++ b/bot/path, "
           "@@ -start,count +start,count @@ and context/removed/added lines. Use /dev/null for add/delete. "
           "Use different paths for move, including a hunk even if unchanged. "
           "No diff --git, binary or mode headers. All sections validated before writing",
           {"patch": STRING}, ["patch"],
           [{"patch": "--- a/bot/example.py\n+++ b/bot/example.py\n@@ -1,1 +1,1 @@\n-old_value = 1\n+old_value = 2\n"}]),
]


class Editor:
    def __init__(self, root, feedback_dir=None):
        self.root = Path(root).resolve()
        self.feedback_root = None if feedback_dir is None else Path(feedback_dir).resolve()
        self.feedback = {} if self.feedback_root is None else {
            "feedback/" + p.name: p for p in sorted(self.feedback_root.glob("*.json"))
            if p.name == "metadata.json" or p.name.startswith("game_")}

    def path(self, name, write=False):
        if not isinstance(name, str) or not name or "\\" in name or ":" in name:
            raise ValueError("Use a project-relative POSIX path")
        relative = Path(name)
        if relative.is_absolute() or PureWindowsPath(name).drive or any(part in ("..", ".git") for part in relative.parts):
            raise ValueError("Path escapes project")
        if not write and name in self.feedback:
            target = self.feedback[name]
            if target.is_symlink() or not target.resolve().is_relative_to(self.feedback_root):
                raise ValueError("Symlinks/junctions are not allowed")
            return target
        allowed = {"bot"} if write else {"bot", "rsi", "tests", "config", "prompts", "README.md", "architecture.md"}
        if relative.parts[0] not in allowed:
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

    def read_file(self, path, offset=0):
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be nonnegative")
        text = self.path(path).read_text(encoding="utf-8")
        end = offset + 12000
        return {"text": text[offset:end], "next_offset": end if end < len(text) else None}

    def search(self, path="bot", text=None):
        if path == "feedback":
            names = list(self.feedback)
        else:
            target = self.path(path)
            names = [p.relative_to(self.root).as_posix() for p in sorted(target.rglob("*"))
                     if "__pycache__" not in p.parts] if target.is_dir() else [path]
        matches = []
        for name in names:
            file = self.path(name)
            if not file.is_file():
                continue
            if text is None:
                matches.append(name)
            else:
                try:
                    lines = file.read_text(encoding="utf-8").splitlines()
                except UnicodeDecodeError:
                    continue
                for number, line in enumerate(lines, 1):
                    if text in line:
                        matches.append({"path": name, "line": number, "text": line})
                        if len(matches) == 200:
                            return matches
            if len(matches) == 200:
                break
        return matches

    def apply_patch(self, patch):
        """Apply text-only unified diffs after validating every file and hunk."""
        lines = patch.splitlines(keepends=True)
        changes, touched, index = [], set(), 0
        while index < len(lines):
            if not lines[index].startswith("--- ") or index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                raise ValueError("Expected --- a/path and +++ b/path headers; omit diff --git and mode headers")
            paths = []
            for line, prefix in zip(lines[index:index + 2], ("a/", "b/")):
                name = line[4:].rstrip("\r\n")
                if name == "/dev/null":
                    paths.append(None)
                elif name.startswith(prefix):
                    paths.append(self.path(name[2:], write=True))
                else:
                    raise ValueError("Use a/ and b/ paths, or /dev/null")
            source, destination = paths
            if source is None and destination is None:
                raise ValueError("Patch must name a file")
            for path in set(paths) - {None}:
                if path in touched:
                    raise ValueError("Each file may appear in only one patch section")
                touched.add(path)
            if destination != source and destination is not None and destination.exists():
                raise ValueError("Destination already exists")
            original = [] if source is None else source.read_text(encoding="utf-8").splitlines(keepends=True)
            output, cursor, hunks = [], 0, 0
            index += 2
            while index < len(lines) and lines[index].startswith("@@ "):
                match = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n?", lines[index])
                if not match:
                    raise ValueError("Invalid unified diff hunk")
                old_start, old_count, new_start, new_count = (
                    int(value) if value is not None else 1 for value in match.groups())
                position = old_start if old_count == 0 else old_start - 1
                if position < cursor or position > len(original):
                    raise ValueError("Hunk position is out of range")
                output.extend(original[cursor:position])
                if (new_start if new_count == 0 else new_start - 1) != len(output):
                    raise ValueError("New hunk position does not match")
                old, new = [], []
                index += 1
                while index < len(lines):
                    line = lines[index]
                    if len(old) == old_count and len(new) == new_count and not line.startswith("\\ No newline"):
                        break
                    if line.startswith("\\ No newline at end of file") and index > 0:
                        previous = lines[index - 1][:1]
                        if previous in (" ", "-") and old:
                            old[-1] = old[-1].rstrip("\r\n")
                        if previous in (" ", "+") and new:
                            new[-1] = new[-1].rstrip("\r\n")
                    elif line[:1] in (" ", "-", "+"):
                        if line[0] in (" ", "-"):
                            old.append(line[1:])
                        if line[0] in (" ", "+"):
                            new.append(line[1:])
                    else:
                        raise ValueError("Invalid hunk line")
                    index += 1
                    if len(old) > old_count or len(new) > new_count:
                        raise ValueError("Hunk counts do not match")
                if len(old) != old_count or len(new) != new_count or original[position:position + old_count] != old:
                    raise ValueError("Patch does not match current file; read it and retry")
                output.extend(new)
                cursor = position + old_count
                hunks += 1
            if not hunks:
                raise ValueError("Each file section needs a hunk")
            output.extend(original[cursor:])
            if destination is None and output:
                raise ValueError("Deletion must remove the entire file")
            changes.append((source, destination, "".join(output)))
        if not changes:
            raise ValueError("Patch is empty")
        for path in touched:
            if any(parent in touched or (parent.exists() and not parent.is_dir()) for parent in path.parents):
                raise ValueError("Patch file paths conflict with a parent directory")
        for source, destination, content in changes:
            if destination is not None:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
            if source is not None and source != destination:
                source.unlink()
        return {"changed": sorted(path.relative_to(self.root).as_posix() for path in touched)}
