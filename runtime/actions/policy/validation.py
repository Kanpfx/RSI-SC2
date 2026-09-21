"""Recoverable model action review before Ares behavior construction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.config import GameConfig
from agent.runtime.actions.execution.adapter import AresActionAdapter
from agent.runtime.actions.errors import (
    ActionNameError,
    ConflictError,
    InstructionError,
    OutputFormatError,
    ParameterError,
)
from agent.runtime.actions.policy.exposure import ActionSurface
from agent.runtime.actions.policy.rules import validate_resolved
from agent.runtime.actions.resolution.types import symbol, type_names
from agent.runtime.actions.resolution.resolver import EntityContext
from agent.runtime.actions.resolution.loader import ActionCatalog


@dataclass(frozen=True)
class ValidationIssue:
    """One action-level problem suitable for next-turn validation feedback."""

    index: int
    action: Any
    reason: str

    def text(self) -> str:
        return f"Action {self.index + 1}: {self.reason}"


@dataclass(frozen=True)
class ActionReview:
    """Valid actions are retained even when sibling actions need repair."""

    actions: list[dict[str, Any]]
    issues: list[ValidationIssue]
    normalizations: list[str]
    notices: list[ValidationIssue] = field(default_factory=list)
    arguments: list[dict[str, Any]] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return not self.issues

    @property
    def message(self) -> str:
        if not self.issues:
            return "accepted"
        return "\n".join(issue.text() for issue in self.issues)


class PolicyValidator:
    def __init__(self, catalog: ActionCatalog, game_config: GameConfig):
        self.catalog = catalog
        self.adapter = AresActionAdapter(catalog)
        self.game_config = game_config

    def review(self, bot: Any, actions: list[dict[str, Any]], context: EntityContext,
               surface: ActionSurface | None = None, *, persistent: bool = False) -> ActionReview:
        if not isinstance(actions, list):
            return ActionReview([], [ValidationIssue(0, actions, 'actions must be a list')], [])
        context.bot = bot
        context.max_point_nudge_tiles = self.game_config.max_point_nudge_tiles
        valid, issues, notes, notices, arguments = [], [], [], [], []
        seen = set()
        for index, action in enumerate(actions):
            current = action
            try:
                if index >= self.game_config.max_actions_per_decision:
                    raise InstructionError('action limit exceeded', str(self.game_config.max_actions_per_decision))
                if not isinstance(action, dict):
                    raise InstructionError('format error', 'each action must be an object')
                current = self._normalize_fields(action, {'id': 'id', 'args': 'args'})
                entry = self.catalog.get(current.get('id'))
                if entry['llm_exposure'] != 'eligible':
                    raise ActionNameError.disabled(entry['name'])
                if isinstance(current.get('args'), dict):
                    names = {symbol(p['name']): p['name'] for p in entry['params']}
                    current['args'] = self._normalize_fields(current['args'], names)
                # Remove known dead group members before resolving the current group.
                actor_param = entry['availability']['param']
                group = current.get('args', {}).get(actor_param) if isinstance(current.get('args'), dict) else None
                if actor_param == 'group' and isinstance(group, list):
                    surviving = []
                    for value in group:
                        alias = context.canonical_entity_alias(value)
                        if alias in context.known_own_aliases and alias not in context.own_entities:
                            notices.append(ValidationIssue(index, f'unit {alias}', 'unit disappeared; removed from group'))
                        else:
                            surviving.append(alias)
                    current['args']['group'] = surviving
                normalized, kwargs = self.adapter.prepare(current, context)
                actor_tags = validate_resolved(entry, kwargs, context, ready=not persistent)
                if surface is not None:
                    surface.validate(entry, normalized['args'])
                overlap = seen & actor_tags
                if overlap:
                    raise ConflictError.unit_reused(sorted(overlap)[0])
                self._validate_limits(entry, kwargs)
                seen.update(actor_tags)
                valid.append(normalized)
                arguments.append(kwargs)
                if normalized != action:
                    notes.append(f'Action {index + 1}: normalized names, references or coordinates')
            except (KeyError, TypeError, ValueError) as exc:
                issues.append(ValidationIssue(index, current, str(exc)))
        return ActionReview(valid, issues, notes, notices, arguments)

    @staticmethod
    def _normalize_fields(value: dict, aliases: dict[str, str]) -> dict:
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ParameterError.format('object', 'string field names')
            name = aliases.get(symbol(key), key)
            if name in result:
                raise ParameterError.duplicate(name)
            result[name] = item
        return result

    def _validate_limits(self, entry: dict[str, Any], kwargs: dict[str, Any]) -> None:
        for param in entry['params']:
            if param['input'] != 'model' or param['name'] not in kwargs:
                continue
            value = kwargs[param['name']]
            if value is None or not ({'Unit', 'Units', 'Point2'} & type_names(param['type'])):
                continue
            if hasattr(value, 'tag') or hasattr(value, 'x') or hasattr(value, 'name'):
                continue
            if isinstance(value, (list, tuple, set)):
                limit = (self.game_config.max_action_units if param['name'] == entry['availability']['param']
                         else self.game_config.max_action_targets)
                if len(value) > limit:
                    raise ParameterError.invalid_value(param['name'], len(value), f'at most {limit} entries')
