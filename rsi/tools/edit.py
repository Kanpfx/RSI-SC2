from fnmatch import fnmatch
from pathlib import Path, PureWindowsPath
from rsi.context.feedback import Feedback
from rsi.tools import INTEGER, STRING, schema


TOOLS = [
    schema("read_file", "Read source or read-only feedback/path with offset/limit in characters (max 12000); continue at next_offset",
           {"path": STRING, "offset": INTEGER, "limit": INTEGER}, ["path"],
           [{"path": "bot/main.py"}, {"path": "feedback/metadata.json", "offset": 12000}]),
    schema("search", "List files or search literal text. Filter paths with glob; paginate matches with offset/limit (max 200). Long matching lines are clipped; read_file retrieves full content",
           {"path": STRING, "text": STRING, "glob": STRING, "offset": INTEGER, "limit": INTEGER}, [],
           [{"path": "bot"}, {"path": "bot", "text": "SeedBot"}, {"path": "feedback"}]),
    schema("apply_patch", "Apply a context patch wrapped in *** Begin Patch / *** End Patch. "
           "Use *** Update File: bot/path followed by @@ chunks, with space for unchanged lines, "
           "- for removed lines and + for added lines, including blank lines. No line numbers/counts. "
           "Old/context lines must match exactly one location; include context for insertions. "
           "Chunks follow file order and cannot overlap. Use *** Add File: bot/path with + lines, "
           "*** Delete File: bot/path without a body, or *** Move to: bot/new-path immediately "
           "after an Update header (chunks optional for a pure move). Existing final newline is preserved. "
           "All sections validated before writing; only bot/ can be changed",
           {"patch": STRING}, ["patch"],
           [{"patch": "*** Begin Patch\n*** Update File: bot/example.py\n@@\n-old_value = 1\n+old_value = 2\n*** End Patch"},
            {"patch": "*** Begin Patch\n*** Add File: bot/helper.py\n+value = 1\n*** End Patch"}]),

]


class Editor:
    def __init__(self, root, feedback_dir=None):
        self.root = Path(root).resolve()
        self.feedback = Feedback(feedback_dir)

    def path(self, name, write=False):
        if not isinstance(name, str) or not name or "\\" in name or ":" in name:
            raise ValueError("Use a project-relative POSIX path")
        relative = Path(name)
        if relative.is_absolute() or PureWindowsPath(name).drive or any(part in ("..", ".git") for part in relative.parts):
            raise ValueError("Path escapes project")
        if not write and name in self.feedback.files:
            return self.feedback.path(name)
        allowed = {"bot"} if write else {"bot", "rsi", "tests", "config.yaml", "README.md", "architecture.md"}
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

    def read_file(self, path, offset=0, limit=12000):
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be nonnegative")
        text = (self.feedback.read(path) if path in self.feedback.names()
                else self.path(path).read_text(encoding="utf-8"))
        if type(limit) is not int or not 1 <= limit <= 12000:
            raise ValueError("limit must be 1-12000 characters")
        end = offset + limit
        return {"text": text[offset:end], "next_offset": end if end < len(text) else None}

    def search(self, path="bot", text=None, glob="*", offset=0, limit=50):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("offset must be nonnegative; limit must be 1-200")
        if path == "feedback":
            names = self.feedback.names()
        else:
            target = self.path(path)
            names = [p.relative_to(self.root).as_posix() for p in sorted(target.rglob("*"))
                     if "__pycache__" not in p.parts] if target.is_dir() else [path]
        matches = []
        for name in names:
            if not fnmatch(name, glob):
                continue
            virtual = name in self.feedback.names()
            file = None if virtual else self.path(name)
            if not virtual and not file.is_file():
                continue
            if text is None:
                matches.append(name)
            else:
                try:
                    lines = (self.feedback.read(name) if virtual else file.read_text(encoding="utf-8")).splitlines()
                except UnicodeDecodeError:
                    continue
                for number, line in enumerate(lines, 1):
                    if text in line:
                        matches.append({"path": name, "line": number, "text": line[:500], "truncated": len(line) > 500})
                        if len(matches) > offset + limit:
                            break
            if len(matches) > offset + limit:
                break
        return {"matches": matches[offset:offset + limit],
                "next_offset": offset + limit if len(matches) > offset + limit else None}

    def apply_patch(self, patch):
        """Apply context patches, validating all sections before writing."""
        lines = patch.splitlines()
        if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
            raise ValueError("Wrap the patch in *** Begin Patch and *** End Patch")
        changes, touched, index = [], set(), 1
        while index < len(lines) - 1:
            header = lines[index]
            action = next((value for value in ("Add", "Update", "Delete")
                           if header.startswith(f"*** {value} File: ")), None)
            if action is None:
                raise ValueError(f"Patch line {index + 1}: expected *** Add/Update/Delete File: bot/path")
            name = header.split(": ", 1)[1]
            source = None if action == "Add" else self.path(name, write=True)
            destination = None if action == "Delete" else self.path(name, write=True)
            index += 1
            if action == "Update" and lines[index].startswith("*** Move to: "):
                destination = self.path(lines[index][13:], write=True)
                index += 1
            for path in {source, destination} - {None}:
                if path in touched:
                    raise ValueError(f"{name}: each file may appear in only one section")
                touched.add(path)
            if destination != source and destination is not None and destination.exists():
                raise ValueError(f"{name}: Destination already exists")
            original = "" if source is None else source.read_text(encoding="utf-8")
            if action == "Add":
                added = []
                while index < len(lines) - 1 and not lines[index].startswith("*** "):
                    if not lines[index].startswith("+"):
                        raise ValueError(f"{name}, patch line {index + 1}: added lines must start with +")
                    added.append(lines[index][1:])
                    index += 1
                content = "\n".join(added) + ("\n" if added else "")
            elif action == "Delete":
                content = ""
            else:
                old_lines, output, cursor, hunks = original.splitlines(), [], 0, 0
                while index < len(lines) - 1 and lines[index] == "@@":
                    hunks += 1
                    index += 1
                    before, after = [], []
                    while index < len(lines) - 1 and lines[index] != "@@" and not lines[index].startswith("*** "):
                        line = lines[index]
                        if not line or line[0] not in " +-":
                            raise ValueError(f"{name}, hunk {hunks}, patch line {index + 1}: "
                                             "prefix each line with space, - or + (including blank lines)")
                        if line[0] in " -":
                            before.append(line[1:])
                        if line[0] in " +":
                            after.append(line[1:])
                        index += 1
                    if not before and old_lines:
                        raise ValueError(f"{name}, hunk {hunks}: include unchanged context to locate insertion")
                    positions = [pos for pos in range(len(old_lines) - len(before) + 1)
                                 if old_lines[pos:pos + len(before)] == before]
                    if not positions:
                        raise ValueError(f"{name}, hunk {hunks}: old content does not match; "
                                         "read the file and copy exact context")
                    if len(positions) != 1:
                        raise ValueError(f"{name}, hunk {hunks}: context matches {len(positions)} places; "
                                         "include more unchanged lines")
                    position = positions[0]
                    if position < cursor:
                        raise ValueError(f"{name}, hunk {hunks}: overlapping or out-of-order chunks; combine them")
                    output.extend(old_lines[cursor:position])
                    output.extend(after)
                    cursor = position + len(before)
                if not hunks and destination == source:
                    raise ValueError(f"{name}: use @@ before each chunk, without line numbers or counts")
                output.extend(old_lines[cursor:])
                content = "\n".join(output)
                if output and (original.endswith("\n") or not original):
                    content += "\n"
            changes.append((source, destination, content))
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
