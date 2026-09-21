"""Parse the catalog's small type language and resolve values without eval."""

from __future__ import annotations

import ast
import math
import re
from functools import lru_cache
from typing import Any

from agent.runtime.actions.errors import ResolveError
from agent.runtime.actions.resolution.resolver import EntityContext


def symbol(value: str) -> str:
    return "".join(c for c in value.casefold() if c.isalnum())


@lru_cache(maxsize=128)
def parse_type(expression: str) -> tuple:
    def visit(node: ast.AST) -> tuple:
        if isinstance(node, ast.Name):
            return node.id, ()
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return "union", (visit(node.left), visit(node.right))
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            return node.value.id, tuple(visit(a) for a in args)
        raise ValueError(f"Unsupported catalog type: {expression}")
    return visit(ast.parse(expression, mode="eval").body)


def type_names(expression: str) -> set[str]:
    return set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", expression))


def resolve_enum(enum_type: Any, value: Any, name: str) -> Any:
    if not isinstance(value, str):
        raise ResolveError.format(name, f"a {enum_type.__name__} member name")
    normalized = symbol(value)
    if enum_type.__name__ == "UpgradeId":
        normalized = {"combatshield": "shieldwall", "combatshields": "shieldwall",
                      "concussiveshell": "punishergrenades",
                      "concussiveshells": "punishergrenades"}.get(normalized, normalized)
    for key, member in enum_type.__members__.items():
        if symbol(key) == normalized:
            return member
    raise ResolveError.invalid_value(name, value, f"a valid {enum_type.__name__} member")


class TypeResolver:
    """Return (canonical JSON-compatible value, native Ares value)."""

    ATOMS = {"bool", "int", "float", "str", "null", "Unit", "Units", "Point2",
             "Grid", "UnitTypeId", "AbilityId", "UpgradeId", "UnitRole", "Behavior"}

    def __init__(self, definitions: dict[str, Any]):
        missing = self.ATOMS - set(definitions)
        if missing:
            raise ValueError(
                f"ATOMS not declared in shared_types.json: {sorted(missing)}"
            )
        self.definitions = definitions

    def validate(self, expression: str, trail: frozenset[str] = frozenset()) -> None:
        def check(tree: tuple) -> None:
            name, args = tree
            if name in {"union", "list", "set", "tuple", "dict"}:
                size = 1 if name in {"list", "set"} else 2
                if len(args) != size:
                    raise ValueError(f"Invalid type arity: {expression}")
                for arg in args:
                    check(arg)
            elif args or name not in self.definitions:
                raise ValueError(f"Unknown type: {name}")
            elif name not in self.ATOMS:
                if name in trail:
                    raise ValueError(f"Recursive type: {name}")
                definition = self.definitions[name]
                children = ([definition['type']] if 'type' in definition
                            else list(definition.get('fields', {}).values()))
                if not children:
                    raise ValueError(f"Unresolvable type: {name}")
                for child in children:
                    self.validate(child, trail | {name})
        check(parse_type(expression))

    def resolve(self, value: Any, expression: str, context: EntityContext,
                name: str) -> tuple[Any, Any]:
        return self._resolve(value, parse_type(expression), context, name)

    def _resolve(self, value: Any, tree: tuple, context: EntityContext,
                 path: str) -> tuple[Any, Any]:
        kind, args = tree
        if kind == "union":
            # Units and list[Unit] both accept arrays but produce different native
            # types, so prefer the concrete SC2 collection regardless of spelling.
            branches = sorted(args, key=lambda branch: branch[0] != "Units")
            errors = []
            for branch in branches:
                try:
                    return self._resolve(value, branch, context, path)
                except ResolveError as exc:
                    errors.append(str(exc))
            raise ResolveError("union mismatch", " OR ".join(errors), parameter=path)
        if kind == "null":
            if value is None:
                return None, None
            raise ResolveError.format(path, "null")
        if value is None:
            raise ResolveError.format(path, f"{kind}, not null")
        if kind in {"list", "set", "tuple", "Units"}:
            if not isinstance(value, (list, tuple)):
                raise ResolveError.format(path, "an array")
            if kind == "tuple" and len(value) != len(args):
                raise ResolveError.format(path, f"an array of length {len(args)}")
            wire, native = [], []
            for i, item in enumerate(value):
                item_type = ("Unit", ()) if kind == "Units" else args[i] if kind == "tuple" else args[0]
                w, n = self._resolve(item, item_type, context, f"{path}[{i}]")
                wire.append(w)
                native.append(n)
            if kind == "set":
                try:
                    native = set(native)
                except TypeError as exc:
                    raise ResolveError.format(path, "hashable set elements") from exc
                wire = [v for i, v in enumerate(wire) if v not in wire[:i]]
            elif kind == "tuple":
                native = tuple(native)
            elif kind == "Units":
                if context.bot is None:
                    raise ResolveError.format(path, "Units with runtime BotAI context")
                from sc2.units import Units
                native = Units(native, context.bot)
            return wire, native
        if kind == "dict":
            if not isinstance(value, dict):
                raise ResolveError.format(path, "an object")
            wire, native = {}, {}
            for key, item in value.items():
                decoded_key = key
                if args[0] == ("int", ()) and isinstance(key, str) and re.fullmatch(r"-?\d+", key):
                    decoded_key = int(key)
                wk, nk = self._resolve(decoded_key, args[0], context, f"{path}.key")
                w, n = self._resolve(item, args[1], context, f"{path}.{wk}")
                try:
                    if nk in native:
                        raise ResolveError.invalid_value(path, key, "unique resolved keys")
                    native[nk] = n
                except TypeError as exc:
                    raise ResolveError.format(path, "hashable dictionary keys") from exc
                wire[str(wk)] = w
            return wire, native
        if kind == "bool":
            if type(value) is not bool:
                raise ResolveError.format(path, "a boolean")
            return value, value
        if kind in {"int", "float"}:
            valid = type(value) is int if kind == "int" else type(value) in (int, float)
            try:
                valid = valid and math.isfinite(value)
            except OverflowError:
                valid = False
            if not valid:
                raise ResolveError.format(path, f"a finite {kind}, excluding booleans")
            return value, value if kind == "int" else float(value)
        if kind == "str":
            if not isinstance(value, str):
                raise ResolveError.format(path, "a string")
            return value, value
        if kind == "Unit":
            alias = context.canonical_entity_alias(value)
            return alias, context.resolve_entity(alias)
        if kind == "Point2":
            point = context.resolve_point(value)
            return {"x": point.x, "y": point.y}, point
        if kind == "Grid":
            if not isinstance(value, str):
                raise ResolveError.format(path, "a grid name")
            key = value.strip().casefold().replace("-", "_").replace(" ", "_")
            if key not in context.grids or key not in self.definitions['Grid']['values']:
                raise ResolveError.invalid_value(path, value, f"one of {sorted(context.grids)}")
            return key, context.grids[key]
        if kind in {"UnitTypeId", "AbilityId", "UpgradeId", "UnitRole"}:
            from sc2.ids.unit_typeid import UnitTypeId
            from sc2.ids.ability_id import AbilityId
            from sc2.ids.upgrade_id import UpgradeId
            from ares.consts import UnitRole
            enum = {"UnitTypeId": UnitTypeId, "AbilityId": AbilityId,
                    "UpgradeId": UpgradeId, "UnitRole": UnitRole}[kind]
            member = resolve_enum(enum, value, path)
            return member.name, member
        if kind == "Behavior":
            raise ResolveError.format(path, "runtime-compiled child behaviors, not model expressions")
        definition = self.definitions[kind]
        if 'type' in definition:
            return self.resolve(value, definition['type'], context, path)
        fields = definition['fields']
        if not isinstance(value, dict) or set(value) != set(fields):
            raise ResolveError.format(path, f"an object with exactly {', '.join(fields)}")
        wire, native = {}, {}
        for key, expression in fields.items():
            wire[key], native[key] = self.resolve(value[key], expression, context, f"{path}.{key}")
        if kind == "CompositionEntry":
            if not 0 <= native['proportion'] <= 1 or not 0 <= native['priority'] <= 10:
                raise ResolveError.invalid_value(path, value, "proportion in [0,1] and priority in 0..10")
        return wire, native
