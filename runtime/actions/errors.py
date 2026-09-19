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
        return f"{self.category}: {self.kind}; {self.detail}"


class ActionError(ValueError):
    """Base exception safe to include in next-turn model feedback."""

    category = "Action error"

    def __init__(
        self,
        kind: str,
        detail: str,
        *,
        parameter: str | None = None,
        expected: str | None = None,
        actual: Any = None,
    ) -> None:
        self.details = ErrorDetails(
            self.category,
            kind,
            detail,
            parameter,
            expected,
            actual,
        )
        super().__init__(self.details.render())


class OutputFormatError(ActionError):
    category = "Output format error"

    @classmethod
    def dsl_section(cls, name: str, count: int) -> "OutputFormatError":
        return cls(
            "invalid DSL output",
            f"exactly one '# {name}' section is required; found {count}",
            parameter=name,
            expected="one heading section",
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
            f"'{value}' is not currently listed in `<available_actions>`",
            actual=value,
        )


class ParameterError(InstructionError):
    category = "Parameter error"

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
        )


class AvailabilityError(InstructionError):
    category = "Availability error"

    @classmethod
    def unavailable(cls, action: Any, reason: str) -> "AvailabilityError":
        return cls(
            "action unavailable",
            f"'{action}': {reason}",
            actual=action,
        )


class ConflictError(InstructionError):
    category = "Conflict error"

    @classmethod
    def unit_reused(cls, unit_id: Any) -> "ConflictError":
        return cls(
            "unit assigned more than once",
            f"unit '{unit_id}' already has a higher-priority action in this decision",
            actual=unit_id,
        )


class ResourceError(InstructionError):
    category = "Resource error"

    @classmethod
    def insufficient(cls, action: Any) -> "ResourceError":
        return cls(
            "insufficient resources",
            f"action '{action}' is too far from being affordable this decision",
            actual=action,
        )


class ResolveError(ParameterError):
    """Compatibility name for failures while resolving model values."""
