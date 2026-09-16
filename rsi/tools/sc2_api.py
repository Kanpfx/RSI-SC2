"""Small, offline queries over the installed python-sc2 package."""
import ast
import importlib.metadata
from functools import lru_cache

from rsi.tools import INTEGER, STRING, schema


TOOLS = [
    schema("tech_tree", "Explore static production and technology relationships. "
           "Prerequisites are AND conditions within each production route; routes are alternatives. "
           "Unlocks are potential, not currently available. Depth 1-3; cycles are visited once.",
           {"entity": STRING, "direction": {"type": "string", "enum": ["prerequisites", "unlocks", "both"]},
            "depth": {"type": "integer", "minimum": 1, "maximum": 3}}, ["entity"],
           [{"entity": "BARRACKS"}, {"entity": "MARAUDER", "direction": "prerequisites", "depth": 1}]),
    schema("entity_info", "Read static unit/building identity, production routes, potential abilities "
           "and research. Numeric stats unavailable offline are explicitly null. "
           "Accept a UnitTypeId name; abilities still require runtime checks.",
           {"entity": STRING}, ["entity"], [{"entity": "SIEGETANK"}]),
    schema("api_query", "Browse installed sc2 packages, modules, classes and members without executing source. "
           "Use path='sc2' to start; query filters names/docs within the selected scope. "
           "A member returns signature, docs and source. Use next_offset for more results. "
           "For inherited methods, follow listed bases or search the method name under sc2.",
           {"path": STRING, "query": STRING, "offset": INTEGER}, [],
           [{"path": "sc2"}, {"path": "sc2.bot_ai.BotAI", "query": "build"},
            {"path": "BotAI.build"}]),
]
MAX_RESULTS = 30
MAX_SOURCE = 12000


@lru_cache(maxsize=1)
def library():
    distribution = importlib.metadata.distribution("burnysc2")
    return distribution.version, distribution.locate_file("sc2").resolve()


@lru_cache(maxsize=1)
def static_data():
    from sc2.dicts.unit_abilities import UNIT_ABILITIES
    from sc2.dicts.unit_research_abilities import RESEARCH_INFO
    from sc2.dicts.unit_train_build_abilities import TRAIN_INFO
    from sc2.ids.unit_typeid import UnitTypeId

    routes = []
    for producer, products in TRAIN_INFO.items():
        for product, details in products.items():
            requirements = [details["required_building"].name] if details.get("required_building") else []
            if details.get("requires_techlab"):
                requirements.append(getattr(UnitTypeId, producer.name + "TECHLAB", UnitTypeId.TECHLAB).name)
            routes.append({"producer": producer.name, "product": product.name,
                           "requires": requirements, **{
                               key: getattr(value, "name", value) for key, value in details.items()}})
    return UnitTypeId, sorted(routes, key=lambda r: (r["producer"], r["product"])), UNIT_ABILITIES, RESEARCH_INFO


def resolve_entity(entity):
    ids = static_data()[0]
    name = entity.rsplit(".", 1)[-1].upper() if isinstance(entity, str) else ""
    if name not in ids.__members__:
        raise ValueError("Unknown UnitTypeId; use api_query('sc2.ids.unit_typeid.UnitTypeId', query=...) to find a name")
    return ids[name]


def route_text(route):
    conditions = ", and ".join(route["requires"])
    return (f'{route["producer"]} produces/builds/morphs into {route["product"]}'
            + (f'; also requires {conditions}' if conditions else "")
            + f' using {route["ability"]}.')


def tech_tree(entity, direction="both", depth=2):
    node = resolve_entity(entity).name
    if direction not in ("prerequisites", "unlocks", "both") or type(depth) is not int or not 1 <= depth <= 3:
        raise ValueError("Use direction prerequisites/unlocks/both and depth 1-3")
    routes = static_data()[1]
    result = {"status": "found", "entity": node, "version": library()[0],
              "source": "sc2.dicts.unit_train_build_abilities.TRAIN_INFO",
              "note": "Partial static graph, not a live build plan. Each route requires its producer AND "
                      "listed requirements; routes are alternatives. Placement/resource conditions still apply."}
    for way in ("prerequisites", "unlocks"):
        if direction not in (way, "both"):
            continue
        frontier, visited, selected, seen = {node}, set(), [], set()
        truncated = False
        for _ in range(depth):
            following = set()
            for current in sorted(frontier - visited):
                visited.add(current)
                for index, route in enumerate(routes):
                    dependencies = {route["producer"], *route["requires"]}
                    matches = route["product"] == current if way == "prerequisites" else current in dependencies
                    if not matches:
                        continue
                    if index not in seen:
                        if len(selected) == MAX_RESULTS:
                            truncated = True
                            continue
                        seen.add(index)
                        selected.append({**route, "description": route_text(route)})
                    following.update(dependencies if way == "prerequisites" else {route["product"]})
            frontier = following - visited
        result[way] = {"routes": selected, "truncated": truncated,
                       "depth_limited": bool(frontier),
                       "hint": "Query a returned entity to explore further."}
    return result


def entity_info(entity):
    unit = resolve_entity(entity)
    _, routes, abilities, research = static_data()
    return {"status": "found", "entity": unit.name, "id": unit.value, "version": library()[0],
            "source": "sc2.ids.unit_typeid; sc2.dicts",
            "production": [route for route in routes if route["product"] == unit.name],
            "produces": [route for route in routes if route["producer"] == unit.name],
            "abilities": sorted(ability.name for ability in abilities.get(unit, set())),
            "research": [{"upgrade": upgrade.name, **{key: getattr(value, "name", value)
                          for key, value in details.items()}}
                         for upgrade, details in sorted(research.get(unit, {}).items(), key=lambda pair: pair[0].name)],
            "stats": {"health_max": None, "shield_max": None, "armor": None,
                      "movement_speed": None, "mineral_cost": None, "vespene_cost": None},
            "note": "Numeric stats are unavailable offline: costs/armor/speed need game data; "
                    "health/shields need observed units. Listed abilities are potential, not currently usable. "
                    "Use api_query for Unit.train/build, BotAI.build and BotAI.get_available_abilities."}


def definitions(body, prefix):
    for node in body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = f"{prefix}.{node.name}"
            yield name, node
            if isinstance(node, ast.ClassDef):
                yield from definitions(node.body, name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    yield f"{prefix}.{target.id}", node


@lru_cache(maxsize=1)
def api_index():
    root = library()[1]
    if not root.is_dir():
        raise ValueError("Installed sc2 source directory was not found")
    entries, sources = {"sc2": {"symbol": "sc2", "kind": "package"}}, {}
    for file in sorted(root.rglob("*.py")):
        if not file.resolve().is_relative_to(root):
            continue
        parts = list(file.relative_to(root).with_suffix("").parts)
        package = parts[-1] == "__init__"
        if package:
            parts.pop()
        module = ".".join(["sc2", *parts])
        source = file.read_text(encoding="utf-8-sig")
        sources[module] = source
        entries[module] = {"symbol": module, "kind": "package" if package else "module",
                           "file": str(file), "module": module}
        for name, node in definitions(ast.parse(source).body, module):
            kind = "class" if isinstance(node, ast.ClassDef) else (
                "function" if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else "constant")
            entries[name] = {"symbol": name, "kind": kind, "module": module,
                             "file": str(file), "line": node.lineno, "node": node}
    return entries, sources


def api_query(path="sc2", query=None, offset=0):
    if (not isinstance(path, str) or len(path) > 200 or not all(part.isidentifier() for part in path.split("."))
            or type(offset) is not int or offset < 0
            or (query is not None and (not isinstance(query, str) or len(query) > 200))):
        raise ValueError("Use a dotted API path, an optional query of at most 200 characters and nonnegative offset")
    entries, sources = api_index()
    matches = [name for name in entries if name == path or name.endswith("." + path)]
    base = {"version": library()[0]}
    if path in entries:
        matches = [path]
    if not matches:
        return {**base, "status": "not_found", "hint": "Browse sc2 or search the method name with query; "
                "inherited methods are listed under their defining class."}
    if len(matches) > 1:
        items = [entries[name] for name in sorted(matches)]
        status = "ambiguous"
    else:
        path = matches[0]
        entry = entries[path]
        node = entry.get("node")
        base.update({key: value for key, value in entry.items() if key != "node"})
        base["status"] = "found"
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            docs = ast.get_docstring(node) or ""
            base.update(docstring=docs[:4000], docstring_truncated=len(docs) > 4000,
                        decorators=[ast.unparse(value) for value in node.decorator_list])
            if isinstance(node, ast.ClassDef):
                base["bases"] = [ast.unparse(value) for value in node.bases]
                base["signature"] = f"class {node.name}({', '.join(base['bases'])}):"
            else:
                base["async"] = isinstance(node, ast.AsyncFunctionDef)
                base["signature"] = (("async " if base["async"] else "") + f"def {node.name}({ast.unparse(node.args)})"
                                     + (f" -> {ast.unparse(node.returns)}" if node.returns else "") + ":")
        if entry["kind"] in ("function", "constant"):
            source = ast.get_source_segment(sources[entry["module"]], node) or ""
            base.update(source=source[offset:offset + MAX_SOURCE],
                        next_offset=offset + MAX_SOURCE if len(source) > offset + MAX_SOURCE else None)
            return base
        items = []
        for name, member in sorted(entries.items()):
            if not name.startswith(path + "."):
                continue
            if query:
                child = member.get("node")
                docs = ast.get_docstring(child) or "" if isinstance(
                    child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) else ""
                if query.casefold() not in (name + " " + docs).casefold():
                    continue
            elif name.rsplit(".", 1)[0] != path:
                continue
            items.append(member)
        status = "found"
    return {**base, "status": status,
            "members": [{key: value for key, value in item.items() if key != "node"}
                        for item in items[offset:offset + MAX_RESULTS]],
            "total": len(items),
            "next_offset": offset + MAX_RESULTS if len(items) > offset + MAX_RESULTS else None}
