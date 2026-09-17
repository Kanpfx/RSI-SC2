"""Offline SC2 discovery tools; no game or model calls."""
import inspect
import json

import pytest

from rsi.tools.sc2_api import TOOLS, api_query, entity_info, tech_tree


def test_tech_routes_and_reverse_unlocks():
    result = tech_tree("UnitTypeId.marauder", "prerequisites", 1)
    route = next(r for r in result["prerequisites"]["routes"] if r["producer"] == "BARRACKS")
    assert "BARRACKSTECHLAB" in route["requires"]
    assert "requires" in route["description"]
    reverse = tech_tree("BARRACKSTECHLAB", "unlocks", 1)["unlocks"]["routes"]
    assert any(r["product"] == "MARAUDER" for r in reverse)
    barracks = tech_tree("BARRACKS", "unlocks", 1)["unlocks"]["routes"]
    assert any(r["product"] == "FACTORY" and "BARRACKS" in r["requires"] for r in barracks)
    cyclic = tech_tree("SCV", depth=3)
    for way in ("prerequisites", "unlocks"):
        routes = cyclic[way]["routes"]
        assert len(routes) <= 30
        assert len({(r["producer"], r["product"]) for r in routes}) == len(routes)
    with pytest.raises(ValueError):
        tech_tree("MARINE", depth=0)
    with pytest.raises(ValueError, match="Unknown"):
        tech_tree("NO_SUCH_UNIT")


def test_entity_static_capabilities_and_missing_stats():
    info = entity_info("BARRACKS")
    assert "BARRACKSTRAIN_MARINE" in info["abilities"]
    assert any(r["product"] == "MARAUDER" for r in info["produces"])
    assert all(value is None for value in info["stats"].values())
    assert any(r["upgrade"] == "STIMPACK" for r in entity_info("BARRACKSTECHLAB")["research"])


def test_api_browse_search_members_and_pages():
    assert any(m["symbol"] == "sc2.bot_ai" for m in api_query()["members"])
    assert any(m["symbol"] == "sc2.bot_ai.BotAI" for m in api_query("sc2.bot_ai")["members"])
    assert "source" not in api_query("BotAI.build")
    build = api_query("BotAI.build", include_source=True)
    assert build["status"] == "found" and build["async"]
    assert build["symbol"] == "sc2.bot_ai.BotAI.build" and "async def build" in build["source"]
    found = api_query("sc2.bot_ai.BotAI", query="build")
    assert any(m["symbol"] == build["symbol"] for m in found["members"])
    enums = api_query("sc2.ids.unit_typeid.UnitTypeId")
    assert enums["next_offset"] is not None
    page = api_query("sc2.ids.unit_typeid.UnitTypeId", offset=enums["next_offset"])
    assert not {m["symbol"] for m in enums["members"]} & {m["symbol"] for m in page["members"]}
    marine = api_query("sc2.ids.unit_typeid.UnitTypeId", query="MARINE")
    assert any(m["symbol"].endswith(".MARINE") for m in marine["members"])
    assert "MARINE" in api_query("UnitTypeId.MARINE")["source"]
    assert api_query("sc2.missing")["status"] == "not_found"
    with pytest.raises(ValueError):
        api_query("../outside")


def test_tool_schemas_match_callables():
    functions = {"tech_tree": tech_tree, "entity_info": entity_info, "api_query": api_query}
    for tool in TOOLS:
        function = tool["function"]
        assert set(function["parameters"]["properties"]) == set(inspect.signature(functions[function["name"]]).parameters)
    json.dumps([tech_tree("MARINE"), entity_info("MARINE"), api_query("Unit.health_max")])


@pytest.fixture
def installed_source(tmp_path, monkeypatch):
    from rsi.tools import sc2_api

    root = tmp_path / "sc2"
    root.mkdir()
    (root / "bot_ai.py").write_text('''raise RuntimeError("Source must never execute")
class BotAI:
    async def build(self, unit: int, near=None) -> bool:
        """Build near the selected position."""
        return True
''', encoding="utf-8")
    (root / "unit.py").write_text("class Unit:\n    def build(self, unit):\n        return False\n", encoding="utf-8")

    class Distribution:
        version = "test-version"

        def locate_file(self, name):
            assert name == "sc2"
            return root

    sc2_api.library.cache_clear()
    sc2_api.api_index.cache_clear()
    monkeypatch.setattr(sc2_api.importlib.metadata, "distribution", lambda name: Distribution())
    try:
        yield root
    finally:
        sc2_api.library.cache_clear()
        sc2_api.api_index.cache_clear()


def test_lookup_definition_without_execution(installed_source):
    from pathlib import Path

    result = api_query("BotAI.build", include_source=True)
    assert result["status"] == "found" and result["version"] == "test-version"
    assert result["symbol"] == "sc2.bot_ai.BotAI.build" and result["async"]
    assert result["signature"] == "async def build(self, unit: int, near=None) -> bool:"
    assert result["line"] == 3 and Path(result["file"]).is_relative_to(installed_source)
    assert "Build near" in result["docstring"] and "return True" in result["source"]
    assert result["next_offset"] is None
    assert api_query("sc2.bot_ai.BotAI.build", include_source=True) == result


def test_ambiguous_missing_invalid_and_truncated(installed_source, monkeypatch):
    from rsi.tools import sc2_api

    result = api_query("build")
    assert result["status"] == "ambiguous"
    assert {item["symbol"] for item in result["members"]} == {"sc2.bot_ai.BotAI.build", "sc2.unit.Unit.build"}
    assert api_query("missing")["status"] == "not_found"
    for path in ("../secret.py", "/etc/passwd", "BotAI.build()", "", None):
        with pytest.raises(ValueError):
            api_query(path)
    expected = api_query("Unit.build", include_source=True)["source"]
    monkeypatch.setattr(sc2_api, "MAX_SOURCE", 12)
    chunks, offset = [], 0
    while offset is not None:
        result = api_query("Unit.build", offset=offset, include_source=True)
        assert len(result["source"]) <= 12
        chunks.append(result["source"])
        offset = result["next_offset"]
    assert "".join(chunks) == expected
