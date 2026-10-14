"""Structured errors for model action parsing and validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ErrorDetails:
    """Machine-readable details behind one concise English error message."""

    category: str
    kind: str
    detail: str
    parameter: str | None = None
    expected: str | None = None
    actual: Any = None

    def render(self) -> str:
        return f"{self.category}: {self.detail}"


def exception_reason(exc: Exception) -> str:
    """Keep model errors concise and unexpected exceptions identifiable."""
    if isinstance(exc, ActionError):
        return str(exc)
    detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return f"execution_error: {detail}"


class ActionError(ValueError):
    """Base exception safe to include in next-turn model feedback."""

    category = "format_error"

    def __init__(
        self,
        kind: str,
        detail: str,
        *,
        parameter: str | None = None,
        expected: str | None = None,
        actual: Any = None,
        category: str | None = None,
    ) -> None:
        self.details = ErrorDetails(
            category or self.category,
            kind,
            detail,
            parameter,
            expected,
            actual,
        )
        super().__init__(self.details.render())


class OutputFormatError(ActionError):
    category = "format_error"

    @classmethod
    def dsl_section(cls, name: str, count: int) -> "OutputFormatError":
        return cls(
            "invalid DSL output",
            f"a '## {name}' section is required; found {count}",
            parameter=name,
            expected="a heading section; use the first occurrence",
            actual=count,
        )

    @classmethod
    def dsl_action(cls, source: str, detail: str) -> "OutputFormatError":
        return cls(
            "invalid action expression",
            detail,
            parameter="actions",
            expected="ActionName(argument=value, ...)",
            actual=source,
        )

    @classmethod
    def field_type(cls, name: str, expected: str) -> "OutputFormatError":
        return cls(
            "field format error",
            f"field '{name}' must be {expected}",
            parameter=name,
            expected=expected,
        )


class InstructionError(ActionError):
    """An action name, shape, or argument cannot be compiled safely."""


class ActionNameError(InstructionError):
    category = "action_unavailable"

    @classmethod
    def unknown(cls, value: Any) -> "ActionNameError":
        return cls(
            "unknown action",
            f"'{value}' is not listed in `<available_actions>`",
            actual=value,
        )

    @classmethod
    def disabled(cls, value: Any) -> "ActionNameError":
        return cls(
            "unavailable action",
            f"'{value}' is disabled; select an action from `<available_actions>`",
            actual=value,
        )


class ParameterError(InstructionError):
    category = "format_error"

    @classmethod
    def missing(cls, name: str) -> "ParameterError":
        return cls(
            "missing parameter",
            f"'{name}' is required",
            parameter=name,
        )

    @classmethod
    def unexpected(cls, names: list[str]) -> "ParameterError":
        rendered = ", ".join(f"'{name}'" for name in names)
        return cls(
            "unexpected parameter",
            f"not defined for this action: {rendered}",
            actual=names,
        )

    @classmethod
    def duplicate(cls, name: str) -> "ParameterError":
        return cls(
            "duplicate parameter",
            f"'{name}' was provided more than once",
            parameter=name,
        )

    @classmethod
    def format(cls, name: str, expected: str) -> "ParameterError":
        return cls(
            "format error",
            f"'{name}' must be {expected}",
            parameter=name,
            expected=expected,
        )

    @classmethod
    def invalid_value(cls, name: str, value: Any, expected: str) -> "ParameterError":
        return cls(
            "invalid value",
            f"'{name}' = {value!r}; expected {expected}",
            parameter=name,
            expected=expected,
            actual=value,
            category="execution_error" if name == "runtime" else None,
        )


class AvailabilityError(InstructionError):
    category = "action_unavailable"

    @classmethod
    def unavailable(cls, action: Any, reason: str) -> "AvailabilityError":
        return cls(
            "action unavailable",
            f"'{action}': {reason}",
            actual=action,
        )


class ConflictError(InstructionError):
    category = "action_conflict"

    @classmethod
    def unit_reused(cls, unit_id: Any, previous_action: str | None = None,
                    previous_index: int | None = None) -> "ConflictError":
        return cls(
            "unit assigned more than once",
            f"unit '{unit_id}' already assigned to "
            + (f"Action {previous_index}: {previous_action}" if previous_action else "another action in this decision")
            + "; assign each unit to one control action",
            actual=unit_id,
        )


class ResourceError(InstructionError):
    category = "resource_insufficient"

    @classmethod
    def insufficient(cls, action: Any, reason: str = "insufficient resources; no order submitted") -> "ResourceError":
        return cls(
            "insufficient resources",
            reason,
            actual=action,
        )

    @classmethod
    def for_cost(cls, bot: Any, target: Any) -> "ResourceError":
        cost = bot.calculate_cost(target)
        required = (int(cost.minerals), int(cost.vespene))
        available = (int(bot.minerals), int(bot.vespene))
        missing = tuple(max(0, need - have) for need, have in zip(required, available))
        return cls.insufficient(
            getattr(target, "name", str(target)),
            f"{getattr(target, 'name', str(target))} requires {required[0]} minerals, {required[1]} gas; "
            f"available {available[0]} minerals, {available[1]} gas; "
            f"missing {missing[0]} minerals, {missing[1]} gas; resubmit when affordable",
        )


class ResolveError(ParameterError):
    """Compatibility name for failures while resolving model values."""
