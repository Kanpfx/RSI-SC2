"""Recoverable model action review before Ares behavior construction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.config import GameConfig
from agent.game.actions.execution.adapter import AresActionAdapter
from agent.game.actions.errors import (
    ActionNameError,
    exception_reason,
    InstructionError,
    ParameterError,
)
from agent.game.actions.policy.exposure import ActionSurface
from agent.game.actions.policy.rules import (
    validate_resolved, actor_parameter, filter_group, actor_supported, ability_ready,
    ActionSurfaceError,
)
from agent.game.actions.resolution.types import normalize_fields, symbol, type_names
from agent.game.actions.resolution.resolver import EntityContext
from agent.game.actions.resolution.loader import ActionCatalog


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
    # Group actions have one argument set per execution cohort; [] means waiting.
    arguments: list[dict[str, Any] | list[dict[str, Any]]] = field(default_factory=list)
    indices: list[int] = field(default_factory=list)

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
            return ActionReview([], [ValidationIssue(0, actions, str(ParameterError.format('actions', 'a list')))], [])
        context.bot = bot
        context.max_point_nudge_tiles = self.game_config.max_point_nudge_tiles
        valid, issues, notes, notices, arguments = [], [], [], [], []
        seen, scopes, indices = {}, {}, []
        # Validate later instructions first; only legal actions claim control.
        for index in reversed(range(len(actions))):
            action = actions[index]
            current = action
            try:
                if index >= self.game_config.max_actions_per_decision:
                    raise InstructionError('action limit exceeded', f'at most {self.game_config.max_actions_per_decision} actions')
                if not isinstance(action, dict):
                    raise InstructionError('format error', 'each action must be an object')
                current = normalize_fields(action, {'id': 'id', 'args': 'args'})
                entry = self.catalog.get(current.get('id'))
                if not entry['enabled']:
                    raise ActionNameError.disabled(entry['name'])
                if isinstance(current.get('args'), dict):
                    names = {symbol(p['name']): p['name'] for p in entry['params']}
                    current['args'] = normalize_fields(current['args'], names)
                actor_param = actor_parameter(entry)
                if actor_param == 'group' and isinstance(current.get('args'), dict) and 'group' in current['args']:
                    members, removed = filter_group(entry, current['args']['group'], context, seen)
                    current['args']['group'] = members
                    notices.extend(ValidationIssue(index, current, reason) for reason in removed)
                    if not members:
                        continue
                    normalized, kwargs = self.adapter.prepare(current, context, resolve_runtime=False)
                    self._validate_limits(entry, kwargs)
                    # Check shared targets separately from individual actors.
                    validate_resolved(entry, {**kwargs, 'group': []}, context)
                    if surface is not None:
                        surface.validate(entry, {k: v for k, v in normalized['args'].items() if k != 'group'})
                    eligible = []
                    ongoing = entry['persistent']
                    for alias, unit in zip(members, kwargs['group']):
                        try:
                            if not actor_supported(entry, unit):
                                raise ActionSurfaceError('actor cannot execute', f'unit {alias} cannot execute {entry["name"]} in its current state')
                            ability = kwargs.get('ability', kwargs.get('ability_id'))
                            if ability is not None and not ability_ready(unit, ability.name):
                                raise ActionSurfaceError('ability unavailable', f'unit {alias}: {ability.name} unavailable')
                            if surface is not None:
                                surface.validate(entry, {'group': [alias]})
                            eligible.append((alias, unit))
                        except (TypeError, ValueError) as exc:
                            if not persistent:
                                notices.append(ValidationIssue(index, current,
                                    f'unit {alias}: {exception_reason(exc)}; ' +
                                    ('waiting' if ongoing else 'removed from group')))
                    if not ongoing:
                        normalized['args']['group'] = [alias for alias, unit in eligible]
                        if not eligible:
                            raise ParameterError.format('group', 'at least one executable member')
                    cohorts = {}
                    shared_grid = any(p['source'] == 'runtime' and p['value'] == 'group_grid' for p in entry['params'])
                    for alias, unit in eligible:
                        cohorts.setdefault(unit.is_flying if shared_grid else None, []).append(unit)
                    prepared = []
                    for group in cohorts.values():
                        group_args = {**kwargs, 'group': group}
                        self.adapter.resolve_runtime(entry, group_args, context)
                        prepared.append(group_args)
                    actor_tags = {str(context.own_entities[a].tag) for a in normalized['args']['group']}
                    seen.update({tag: (index + 1, entry['name']) for tag in actor_tags})
                    valid.append(normalized)
                    arguments.append(prepared)
                    indices.append(index)
                    if normalized != action:
                        notes.append(f'Action {index + 1}: filtered group members or normalized arguments')
                    continue
                normalized, kwargs = self.adapter.prepare(current, context)
                actor_tags = validate_resolved(entry, kwargs, context, ready=not persistent)
                if surface is not None:
                    surface.validate(entry, normalized['args'])
                overlap = seen.keys() & actor_tags
                if overlap:
                    tag = sorted(overlap)[0]
                    previous_index, previous_action = seen[tag]
                    notices.append(ValidationIssue(index, normalized,
                        f'unit {context.observation_id(tag)}: overridden by Action {previous_index}: {previous_action}'))
                    continue
                self._validate_limits(entry, kwargs)
                scope = self.catalog.override_key(normalized)
                if scope is not None and scope in scopes:
                    notices.append(ValidationIssue(index, normalized, f'configuration overridden by Action {scopes[scope]}'))
                    continue
                if scope is not None:
                    scopes[scope] = index + 1
                seen.update({tag: (index + 1, entry['name']) for tag in actor_tags})
                valid.append(normalized)
                arguments.append(kwargs)
                indices.append(index)
                if normalized != action:
                    notes.append(f'Action {index + 1}: normalized names, references or coordinates')
            except (KeyError, TypeError, ValueError) as exc:
                issues.append(ValidationIssue(index, current, exception_reason(exc)))
        return ActionReview(valid[::-1], sorted(issues, key=lambda item: item.index), notes,
                            sorted(notices, key=lambda item: item.index), arguments[::-1], indices[::-1])

    def _validate_limits(self, entry: dict[str, Any], kwargs: dict[str, Any]) -> None:
        for param in entry['params']:
            if param['source'] != 'model' or param['name'] not in kwargs:
                continue
            value = kwargs[param['name']]
            if value is None or not ({'Unit', 'Units', 'Point2'} & type_names(param['type'])):
                continue
            if hasattr(value, 'tag') or hasattr(value, 'x') or hasattr(value, 'name'):
                continue
            if isinstance(value, (list, tuple, set)):
                limit = (self.game_config.max_action_units if param['name'] == actor_parameter(entry)
                         else self.game_config.max_action_targets)
                if len(value) > limit:
                    raise ParameterError.invalid_value(param['name'], len(value), f'at most {limit} entries')
