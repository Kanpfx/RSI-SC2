"""Own prompt sources and per-match working memory, independent of SC2."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.context.action_reference import _actions_reference, _section
from agent.runtime.actions.formatting import format_feedback

CONTEXT_ROOT = Path(__file__).resolve().parent
MAX_WORKING_CHARS = 2000


def available_tactics() -> tuple[str, ...]:
    return tuple(sorted(path.stem for path in (CONTEXT_ROOT / "memory/tactics").glob("*.md")))


class ContextBuilder:
    def __init__(self, tactic_name: str = "BattleCruiserRush"):
        if tactic_name not in available_tactics():
            raise ValueError(f"Unknown tactic: {tactic_name!r}")
        self.sources = {
            name: (CONTEXT_ROOT / name).read_text(encoding="utf-8")
            for name in ("prompts/system.md", "memory/general.md", "prompts/output.md",
                         "prompts/observation.md",
                         f"memory/tactics/{tactic_name}.md")
        }
        self.tactic_name = tactic_name
        self.working = ""

    def update_working(self, value: str | None) -> bool:
        if value is None or value == self.working:
            return False
        if len(value) > MAX_WORKING_CHARS:
            raise ValueError(f"Working memory exceeds {MAX_WORKING_CHARS} characters")
        self.working = value
        return True

    def build(self, observation: str, action_entries: list[dict[str, Any]],
              feedback: list[dict[str, Any]], *, max_actions: int = 8) -> list[dict[str, str]]:
        output = self.sources["prompts/output.md"].replace("{max_actions}", str(max_actions))
        output = output.replace("{max_working_chars}", str(MAX_WORKING_CHARS))
        sections = [
            _section("general", self.sources["memory/general.md"]),
            _section("tactic_memory", self.sources[f"memory/tactics/{self.tactic_name}.md"]),
            _section("working_memory", self.working),
            _actions_reference(action_entries),
            _section("observation", "\n\n".join((
                _section("notation", self.sources["prompts/observation.md"]),
                observation,
            ))),
            _section("execution_feedback", format_feedback(feedback)),
            _section("output", output),
        ]
        return [{"role": "system", "content": self.sources["prompts/system.md"]},
                {"role": "user", "content": "\n\n".join(sections)}]
