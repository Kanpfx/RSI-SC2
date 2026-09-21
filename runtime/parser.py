"""Small, non-executing parsers for LLM response formats."""

from __future__ import annotations

import ast
import io
import math
import re
import tokenize
from typing import Any

from agent.runtime.actions.errors import OutputFormatError


def _heading_content(text: str, heading: str) -> tuple[str, int]:
    pattern = re.compile(
        rf"^[ \t]*#[ \t]+{heading}[ \t]*$",
        re.IGNORECASE | re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise OutputFormatError.dsl_section(heading, len(matches))
    match = matches[0]
    return text[match.end() :].strip(), match.start()


def _dsl_value(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("numbers must be finite")
            return value
    elif isinstance(node, ast.Name):
        special = {"true": True, "false": False, "null": None, "none": None}
        return special.get(node.id.casefold(), node.id)
    elif isinstance(node, (ast.List, ast.Tuple)):
        return [_dsl_value(item) for item in node.elts]
    elif isinstance(node, ast.Dict):
        result: dict[str, Any] = {}
        for key_node, value_node in zip(node.keys, node.values):
            if key_node is None:
                raise ValueError("dictionary unpacking is not allowed")
            key = _dsl_value(key_node)
            if not isinstance(key, str):
                raise ValueError("object keys must be names or strings")
            if key in result:
                raise ValueError(f"duplicate object key {key!r}")
            result[key] = _dsl_value(value_node)
        return result
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _dsl_value(node.operand)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("a sign may only prefix a number")
        return value if isinstance(node.op, ast.UAdd) else -value
    raise ValueError("unsupported value expression")


def _normalize_dsl_symbols(source: str) -> str:
    """Join split bare names without touching quoted strings or numeric signs."""
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    normalized: list[tuple[int, str]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        value = token.string
        if token.type == tokenize.NAME:
            while index + 1 < len(tokens):
                following = tokens[index + 1]
                if following.type == tokenize.NAME:
                    value += "_" + following.string
                    index += 1
                elif (
                    following.string == "-"
                    and index + 2 < len(tokens)
                    and tokens[index + 2].type == tokenize.NAME
                ):
                    value += "_" + tokens[index + 2].string
                    index += 2
                else:
                    break
        normalized.append((token.type, value))
        index += 1
    return tokenize.untokenize(normalized)


def _parse_dsl_action(source: str) -> dict[str, Any]:
    try:
        expression = ast.parse(_normalize_dsl_symbols(source), mode="eval").body
    except (SyntaxError, tokenize.TokenError, IndentationError) as exc:
        raise OutputFormatError.dsl_action(source, "invalid function syntax") from exc

    if not isinstance(expression, ast.Call) or not isinstance(
        expression.func, ast.Name
    ):
        raise OutputFormatError.dsl_action(
            source, "expected ActionName(argument=value, ...)"
        )
    if expression.args:
        raise OutputFormatError.dsl_action(
            source, "positional arguments are not allowed"
        )

    args: dict[str, Any] = {}
    normalized_names: set[str] = set()
    for keyword in expression.keywords:
        if keyword.arg is None:
            raise OutputFormatError.dsl_action(
                source, "argument unpacking is not allowed"
            )
        normalized = "".join(
            character for character in keyword.arg.casefold() if character.isalnum()
        )
        if normalized in normalized_names:
            raise OutputFormatError.dsl_action(
                source, f"duplicate argument {keyword.arg!r}"
            )
        normalized_names.add(normalized)
        try:
            args[keyword.arg] = _dsl_value(keyword.value)
        except ValueError as exc:
            raise OutputFormatError.dsl_action(source, str(exc)) from exc
    return {"id": expression.func.id, "args": args}


def parse_model_payload(text: str) -> dict[str, Any]:
    """Parse the headed model DSL while retaining valid sibling actions."""
    candidate = text.strip().lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if "\n" not in candidate and "\\n" in candidate:
        raise OutputFormatError(
            "escaped line breaks",
            "use actual line breaks between DSL sections and actions, not literal backslash-n sequences",
        )
    lines = candidate.splitlines()
    if (
        len(lines) >= 2
        and lines[0].strip().startswith("```")
        and lines[-1].strip() == "```"
    ):
        candidate = "\n".join(lines[1:-1]).strip()
    actions_source, actions_position = _heading_content(candidate, "actions")
    headings = list(re.finditer(r"^[ \t]*#[ \t]+([^\n]+)$", candidate, re.MULTILINE))
    if any(match.group(1).strip().lower() != "actions" for match in headings):
        raise OutputFormatError("invalid DSL output", "only '# actions' is allowed")
    if candidate[:actions_position].strip():
        raise OutputFormatError("invalid DSL output", "output must start with '# actions'")

    actions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    action_lines = [
        line.strip()
        for line in actions_source.splitlines()
        if line.strip()
    ]
    for index, original in enumerate(action_lines):
        source = original
        if source.startswith("- "):
            source = source[2:].lstrip()
        if source.endswith((",", ";")):
            source = source[:-1].rstrip()
        try:
            actions.append(_parse_dsl_action(source))
            sources.append({"source_index": index + 1, "parsed_index": len(actions), "source": original})
        except OutputFormatError as exc:
            errors.append(
                {
                    "index": index,
                    "submitted_action": original,
                    "error": str(exc),
                }
            )
    return {"actions": actions, "errors": errors, "sources": sources}
