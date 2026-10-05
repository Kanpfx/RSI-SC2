"""Own prompt sources and per-match working memory, independent of SC2."""

from __future__ import annotations

import re
from typing import Any

from agent.harness.action_reference import _action_capabilities, _actions_reference, _section
from agent.paths import RESOURCES_ROOT

CONTEXT_ROOT = RESOURCES_ROOT


def available_tactics() -> tuple[str, ...]:
    return tuple(sorted(path.stem for path in (CONTEXT_ROOT / "knowledge/tactics").glob("*.md")))


class ContextBuilder:
    def __init__(self, tactic_name: str = "BattleCruiserRush"):
        if tactic_name not in available_tactics():
            raise ValueError(f"Unknown tactic: {tactic_name!r}")
        self.sources = {
            name: (CONTEXT_ROOT / name).read_text(encoding="utf-8")
            for name in ("prompts/planner/system.md", "prompts/excutor/system.md",
                         "prompts/excutor/rules.md", "prompts/excutor/output.md",
                         "prompts/observation.md", "prompts/planner/output.md",
                         "prompts/planner/rules.md", "knowledge/terran.md",
                         f"knowledge/tactics/{tactic_name}.md")
        }
        self.tactic_name = tactic_name
        self.working = ""
        self.previous_decisions: list[tuple[str, str]] = []

    def update_working(self, value: str | None, time: str = "--:--") -> bool:
        if value is None:
            return False
        self.previous_decisions.append((time, value))
        self.previous_decisions = self.previous_decisions[-2:]
        changed = value != self.working
        self.working = value
        return changed

    def _previous_decisions(self) -> str:
        rows = []
        for time, decision in self.previous_decisions:
            phase = re.search(r"^## Current phase\s*\n(.*?)(?=^## |\Z)", decision, re.M | re.S)
            guidance = re.search(r"^## Guidance\s*\n(.*?)(?=^## |\Z)", decision, re.M | re.S)
            phase_text = ' '.join(phase.group(1).split()) if phase else '[Unknown]'
            guidance_text = ' | '.join(line.strip() for line in
                                     (guidance.group(1) if guidance else decision).splitlines() if line.strip())
            rows.append(f"time={time} phase={phase_text} guidance={guidance_text or '[None]'}")
        return '\n'.join(rows) or 'No previous decision.'

    def build_planner_messages(self, observation: str,
                               action_entries: list[dict[str, Any]]) -> list[dict[str, str]]:
        sections = [
            _section("general_guidance", self.sources["prompts/planner/rules.md"]),
            _section("static_knowledge", self.sources["knowledge/terran.md"]),
            _section("tactical_guidance", self.sources[f"knowledge/tactics/{self.tactic_name}.md"]),
            _action_capabilities(action_entries),
            _section("previous_decision", self._previous_decisions()),
            self._observation(observation),
            _section("output_requirements", self.sources["prompts/planner/output.md"]),
        ]
        return [{"role": "system", "content": self.sources["prompts/planner/system.md"]},
                {"role": "user", "content": "\n\n".join(sections)}]

    def build_excutor_messages(self, working: str, *, observation: str,
                              action_entries: list[dict[str, Any]],
                              max_actions: int = 6) -> list[dict[str, str]]:
        output = self.sources["prompts/excutor/output.md"].replace("{max_actions}", str(max_actions))
        sections = [
            _section("general_guidance", self.sources["prompts/excutor/rules.md"]),
            _section("static_knowledge", self.sources["knowledge/terran.md"]),
            _actions_reference(action_entries),
            _section("current_decision", working),
            self._observation(observation),
            _section("output_requirements", output),
        ]
        return [{"role": "system", "content": self.sources["prompts/excutor/system.md"]},
                {"role": "user", "content": "\n\n".join(sections)}]

    def _observation(self, observation: str) -> str:
        return _section("observation", "\n\n".join((
            "# observation_guide\n" + self.sources["prompts/observation.md"].strip(), observation,
        )))
