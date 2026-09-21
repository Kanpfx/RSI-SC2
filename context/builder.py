"""Own prompt sources and per-match working memory, independent of SC2."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.context.action_reference import _actions_reference, _section
from agent.runtime.actions.formatting import format_feedback

CONTEXT_ROOT = Path(__file__).resolve().parent


def available_tactics() -> tuple[str, ...]:
    return tuple(sorted(path.stem for path in (CONTEXT_ROOT / "memory/tactics").glob("*.md")))


class ContextBuilder:
    def __init__(self, tactic_name: str = "BattleCruiserRush"):
        if tactic_name not in available_tactics():
            raise ValueError(f"Unknown tactic: {tactic_name!r}")
        self.sources = {
            name: (CONTEXT_ROOT / name).read_text(encoding="utf-8")
            for name in ("prompts/system_working.md", "prompts/system_actions.md",
                         "memory/general.md", "prompts/output.md",
                         "prompts/observation.md", "prompts/output_working.md",
                         f"memory/tactics/{tactic_name}.md")
        }
        self.tactic_name = tactic_name
        self.working = ""

    def update_working(self, value: str | None) -> bool:
        if value is None or value == self.working:
            return False
        self.working = value
        return True

    def build(self, observation: str, action_entries: list[dict[str, Any]]) -> list[dict[str, str]]:
        sections = [
            _section("general_guidance", self.sources["memory/general.md"]),
            _section("tactical_guidance", self.sources[f"memory/tactics/{self.tactic_name}.md"]),
            self._observation(observation),
            _actions_reference(action_entries),
            _section("output_requirements", self.sources["prompts/output_working.md"]),
        ]
        return [{"role": "system", "content": self.sources["prompts/system_working.md"]},
                {"role": "user", "content": "\n\n".join(sections)}]

    def build_action_message(self, observation: str, action_entries: list[dict[str, Any]],
                             feedback: list[dict[str, Any]], *, max_actions: int = 6) -> dict[str, str]:
        output = self.sources["prompts/output.md"].replace("{max_actions}", str(max_actions))
        sections = [
            _section("general_guidance", self.sources["memory/general.md"]),
            self._observation(observation),
            _actions_reference(action_entries),
            _section("execution_feedback", format_feedback(feedback)),
            _section("output_requirements", output),
        ]
        return {"role": "user", "content": "\n\n".join(sections)}

    def _observation(self, observation: str) -> str:
        return _section("observation", "\n\n".join((
            "# observation_guide\n" + self.sources["prompts/observation.md"].strip(), observation,
        )))
